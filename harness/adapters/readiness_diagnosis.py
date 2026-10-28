"""One bounded read-only readiness diagnosis over controller-indexed evidence."""
from __future__ import annotations

import json
import time
from pathlib import Path

from harness.adapters.omp_tools import run_session
from harness.runtime.rtl_diagnosis_report import parse_report


EVENTS = {'rtl_review_passed', 'stage_completed', 'observation_budget', 'job_failed'}
ADVICE = {'continue', 'small_experiment', 'more_evidence'}


def _report(text):
    """Decode ordinary model output; recommendations never grant tool permissions."""
    value = parse_report(text)
    if not isinstance(value, dict) or value.get('advice') not in ADVICE:
        raise ValueError('diagnosis advice must be continue, small_experiment or more_evidence')
    if not isinstance(value.get('summary'), str) or not value['summary'].strip():
        raise ValueError('diagnosis report requires a nonempty summary')
    if not isinstance(value.get('findings'), list):
        raise ValueError('diagnosis report requires a findings list')
    if 'experiments' in value and not isinstance(value['experiments'], list):
        raise ValueError('diagnosis experiments must be a list')
    return value


def _limits(context):
    settings = context.get('settings', {}).get('readiness', {})
    limits = {'session_seconds': settings.get('diagnosis_seconds', settings.get('session_seconds')),
              'assistant_turns': settings.get('diagnosis_turns', settings.get('assistant_turns'))}
    if any(type(value) is not int or value <= 0 for value in limits.values()):
        raise ValueError('readiness diagnosis requires positive diagnosis_seconds/diagnosis_turns or existing readiness session budgets')
    return limits


def _summary(observation, *, reused=False):
    completed = observation.get('state') == 'completed'
    report = observation.get('report', {})
    if not isinstance(report, dict):
        report = {}
    return {'status': 'rtl_diagnosis_reused' if reused else 'rtl_diagnosis_completed' if completed else 'rtl_diagnosis_incomplete',
            'completed': completed, 'adopted': False, 'rtl_stage_release': False,
            'observation_id': observation.get('observation_id'), 'fingerprint': observation.get('fingerprint'),
            'index_path': observation.get('index_path'), 'report_path': observation.get('report_path'),
            'advice': report.get('advice'), 'summary': report.get('summary'),
            'diagnosis_state': observation}


def run(context, remote, *, inputs, event):
    """An existing observation is retained even when its original actor failed."""
    if event not in EVENTS:
        raise ValueError('unsupported readiness diagnosis event')
    if not isinstance(inputs, dict):
        raise ValueError('diagnosis inputs must be a JSON object')
    limits = _limits(context)
    skill = Path(context['harness_dir']) / 'skills/rtl-implementation-readiness/SKILL.md'
    if not skill.is_file():
        raise ValueError('readiness diagnosis requires the readiness skill')
    observation = remote.call({'action': 'rtl_diagnosis_prepare', 'event': event, 'inputs': inputs})
    if observation.get('reused'):
        return _summary(observation, reused=True)
    if not observation.get('observation_id') or not observation.get('index_path'):
        raise ValueError('diagnosis preparation lacks its frozen evidence identity')

    label = 'readiness-diagnosis-' + str(time.time_ns())
    child = dict(context, role='readiness_diagnosis', scope='work',
                 diagnosis_observation_id=observation['observation_id'],
                 diagnosis_fingerprint=observation['fingerprint'], readiness_limits=limits)
    for key in ('session_id', 'session_actor_sha256', 'review', 'candidate_scope',
                'campaign', 'repair_skill', 'baseline_project_root'):
        child.pop(key, None)
    cache = Path(context['cache'])
    context_path = cache / (label + '.context.json')
    context_path.write_text(json.dumps(child))
    # Only entry points are supplied. The model chooses and paginates the sources,
    # reports and logs that can answer its engineering question.
    index_path = observation['index_path']
    if not index_path.startswith('@run/'):
        index_path = '@run/' + index_path
    entry = {'remote_run_dir': context['remote_run_dir'], 'observation_id': observation['observation_id'],
             'fingerprint': observation['fingerprint'], 'event': event,
             'index_path': index_path, 'session_budget': limits}
    prompt = (skill.read_text() + '\nMODE: read-only diagnosis of existing synthesis-cost evidence.\n'
              'This session has only list, read, status and jobs. Do not write files, submit or cancel jobs, '
              'modify RTL, or request another skill. Read the frozen evidence index first, then select '
              'the source, log, report and objective observations that address your questions. '
              'Use the ordinary @run/ snapshot paths returned by that index for historical evidence; all large '
              'source/data/log files remain on the server. A read response is one page: use total_lines, '
              'returned_range, eof and next_cursor for targeted reads rather than treating a clipped page '
              'as the end of its source log. The event only requests an observation; '
              'it does not establish an RTL defect, a performance limit or a need to modify RTL. '
              'Judge source/top/parameter/tool/constraint applicability before reusing historical evidence. '
              'Separate directly observed facts, engineering inferences and missing evidence. '
              'Use existing evidence before recommending a new probe. A probe is an option when evidence '
              'is insufficient; this diagnosis cannot execute it. Resource admission and any later '
              'experiment session belong to the controller. A local cost finding is not mapped area, '
              'STA, full-design synthesis acceptance or permission to modify the frozen candidate. '
              'Do not invent an implemented synthesis stage or infer completion from CPU growth or '
              'unchanged logs. Stop when the supplied budget calls for closing. '
              'Return one ordinary JSON object as final output, without write, handoff or other publishing tool calls: '
              '{"advice":"continue|small_experiment|more_evidence","summary":"...",'
              '"findings":[{"observation":"...","evidence":["indexed paths and locations"],'
              '"applicability":"...","limitations":["..."]}],'
              '"experiments":[{"question":"...","scope":"...","comparison":"...",'
              '"functional_check":"..."}],"limitations":["..."]}. '
              'The experiments list may be empty. Recommend only bounded, useful questions supported '
              'by what you actually inspected; no candidate RTL or fixed diagnosis is prescribed. '
              'The controller stores your ordinary report and preserves the actor outcome.\n'
              'EVIDENCE ENTRY POINTS:\n' + json.dumps(entry, ensure_ascii=False, indent=2)
              + '\nFROZEN TASK:\n' + context.get('task', ''))
    answer = run_session(prompt, 'implementation', context['models'], Path(context['project_root']),
                         context_path, cache, remote, label)
    report, error = None, None
    if (answer.get('completed') is True and answer.get('tool_isolation_ok') is True
            and not answer.get('budget_exhausted')):
        try:
            report = _report(answer.get('final_text'))
        except (ValueError, TypeError) as exc:
            error = type(exc).__name__ + ': ' + str(exc)
    else:
        error = 'diagnosis actor did not finish naturally within its isolated budget'
    request = {'action': 'rtl_diagnosis_complete', 'observation_id': observation['observation_id'],
               'session_id': label, 'report': report}
    if error:
        request['error'] = error
    result = remote.call(request)
    return _summary(result)
