"""Optional readiness orchestration; standalone evidence owns engineering decisions."""
from __future__ import annotations

import json
import time
from pathlib import Path, PurePosixPath

from harness.adapters.omp_tools import run_session
from harness.adapters.rtl_remote import wait_for_jobs


def _json_file(remote, scope, name):
    """Read exact, paginated UTF-8; recommendations remain untrusted input."""
    parts = []
    source_sha = None
    seen_cursors = set()
    size = 0
    request = {'action': 'read', 'scope': scope, 'path': name, 'lines': 300}
    while True:
        page = remote.call(request)
        if source_sha is None:
            source_sha = page.get('sha256')
        elif page.get('sha256') != source_sha:
            raise ValueError('readiness report changed during pagination')
        if 'content' in page:
            content = page['content']
        else:
            if not page.get('eof', True):
                raise ValueError('paginated readiness report requires exact content bytes')
            content = '\n'.join(line.split(': ', 1)[1] for line in page['text'].splitlines())
        size += len(content.encode('utf-8'))
        if size > 131072:
            raise ValueError('readiness decision report exceeds 128 KiB')
        parts.append(content)
        if page.get('eof', True):
            break
        cursor = page.get('next_cursor')
        if not cursor or cursor in seen_cursors:
            raise ValueError('readiness report page lacks continuation cursor')
        seen_cursors.add(cursor)
        request = dict(request, cursor=cursor)
    value = json.loads(''.join(parts))
    if not isinstance(value, dict):
        raise ValueError('readiness report must be an object')
    return value


def _finished(answer):
    return ((answer.get('completed') is True or
             (answer.get('handoff_complete') is True and answer.get('termination_reason') == 'verified_handoff'))
            and answer.get('tool_isolation_ok') is True
            and not answer.get('budget_exhausted'))


def _baseline_project_root(context, campaign):
    return str(PurePosixPath(context['remote_run_dir']) / campaign['baseline_path'])


def _session(context, remote, campaign, role, prompt):
    review = role == 'readiness_review'
    settings = context['settings']['readiness'] | campaign.get('budgets', {})
    label = f'readiness-{"review" if review else "implementation"}-{campaign["campaign"]}-{time.time_ns()}'
    child = dict(context, role=role, scope=campaign['audit_scope' if review else 'work_scope'],
                 campaign=campaign['campaign'], baseline_project_root=_baseline_project_root(context, campaign),
                 readiness_limits={
                     'session_seconds': settings['review_seconds' if review else 'session_seconds'],
                     'assistant_turns': settings['review_turns' if review else 'assistant_turns']})
    # A prior implementation/review identity must never leak into this actor.
    for key in ('session_id', 'session_actor_sha256', 'review', 'candidate_scope'):
        child.pop(key, None)
    cache = Path(context['cache'])
    path = cache / (label + '.context.json')
    path.write_text(json.dumps(child))
    answer = run_session(prompt, 'review' if review else 'implementation', context['models'],
                         Path(context['project_root']), path, cache, remote, label)
    return answer, label


def _context_text(context, campaign):
    keys = ('remote_run_dir', 'tools', 'reference_path', 'input_source_paths', 'diagnosis_report_path')
    values = {k: context[k] for k in keys if k in context}
    if values.get('diagnosis_report_path') and not values['diagnosis_report_path'].startswith('@run/'):
        values['diagnosis_report_path'] = '@run/' + values['diagnosis_report_path']
    return json.dumps(values
                      | {'campaign': campaign, 'baseline_project_root': _baseline_project_root(context, campaign),
                         'source_path_base': 'original RTL paths are rtl/... without a work/ prefix; direct baseline root or a campaign staging root preserving original paths/SHA; explicitly restrict --allow-edit to original RTL'},
                      ensure_ascii=False, indent=2)


def run(context, remote, *, reason='initial'):
    """Run at most one fresh attempt; resume never starts an equivalent model turn."""
    settings = context.get('settings', {}).get('readiness', {})
    if settings.get('enabled') is not True:
        return {'status': 'rtl_readiness_skipped', 'reason': 'readiness disabled', 'adopted': False}
    if reason == 'repair' and settings.get('after_repair') is not True:
        return {'status': 'rtl_readiness_skipped', 'reason': 'readiness after repair disabled', 'adopted': False}
    skill = Path(context['harness_dir']) / 'skills/rtl-implementation-readiness/SKILL.md'
    if not skill.is_file():
        return {'status': 'rtl_readiness_skipped', 'reason': 'optional readiness skill unavailable', 'adopted': False}

    wait_for_jobs(remote)
    campaign = remote.call({'action': 'rtl_readiness_begin'})
    if not campaign.get('campaign'):
        return {'status': 'rtl_readiness_skipped', 'reason': campaign.get('reason', campaign.get('state', 'campaign budget exhausted')),
                'adopted': False, 'campaign_state': campaign}
    if campaign.get('reused'):
        return {'status': 'rtl_readiness_reused', 'campaign': campaign['campaign'],
                'campaign_state': campaign, 'adopted': campaign.get('adopted') is True,
                'reason': 'existing attempt retained; no automatic model retry'}

    prompt = (skill.read_text() + '\nMODE: bounded implementation readiness before independent RTL review.\n'
              'The implementation baseline at @run/work is read-only. Create overlays, probes, configurations '
              'and the standalone workspace only in your supplied campaign work scope. '
              'SOURCE PATH CONTRACT: the default readiness.py init --project is the absolute '
              'baseline_project_root from SERVER CONTEXT. A staging project within campaign work is '
              'also allowed: copy original RTL with the same relative rtl/... paths and identical '
              'baseline SHA values, and add the necessary frozen probe wrappers or test assets there. '
              'Use that staging root as --project when added sources are needed. Declare --source, '
              '--include, --asset and --allow-edit paths relative to the selected project root. '
              'Always explicitly repeat --allow-edit only for actual original RTL files that may change; '
              'the default editable set also includes source wrappers and is unsuitable for integration. '
              'New wrappers may be frozen --source inputs; tests may be --asset inputs. Neither may '
              'appear in integration allowed edit paths. Original RTL paths and overlay paths must retain rtl/...; '
              'do not prefix them with work/ or root the integration workspace at the parent run directory. '
              'This keeps exported original-file identities compatible with guarded integration. '
              'A separately extracted probe that does not preserve original file paths/bytes remains '
              'probe-only evidence until a corresponding original-source candidate is created and verified. '
              'Inspect original requirements at @input/ and the accepted packet at @architecture/. '
              'Do not run full-design synthesis, edit project scripts, or change the baseline. '
              'Use the actual top and ordered source closure; unsupported dependencies require an honest '
              'smaller scope or evidence_incomplete. You may recommend keeping the baseline.\n'
              'SCHEDULER: use rtl_remote submit for persistent outer Docker jobs. Invoke '
              '@skill/rtl-implementation-readiness/scripts/readiness.py through the configured python. '
              'The @skill alias must be an entire argv argument. For single experiments use run --foreground; '
              'batch runs its planned experiments in the foreground internally. The outer native job owns '
              'their lifecycle. Poll the OUTER job until terminal. Readiness status may wait up to five seconds '
              'for that same outer job; a pending reply means observe it again, never resubmit it. '
              'Do not infer success from outer exit zero, '
              'query live inner PIDs across containers, or resubmit a lost worker as a resume. '
              'Use explicit job IDs and preserve failures. Keep outer timeout within remaining budgets '
              'and above the inner timeout with cleanup margin. Every submit, including inspection and '
              'small helper commands, consumes a native job and timeout reservation. Prefer remote read/list '
              'for inspection; batch independent cheap inspections into one small helper command when '
              'execution is necessary. Reserve remaining jobs, tool time and assistant turns for paired '
              'functional checks, repeated measurements, atomic finalize and independent review. '
              'PROGRESSIVE PILOT: reserve a functional check, three '
              'baseline measurements and three candidate measurements, plus finalize/review/delivery '
              'capacity before allocating a pilot. Choose a short pilot using a small fraction of remaining '
              'campaign tool time after those reserves; max_job_seconds is a ceiling, not the pilot target. '
              'If that pilot times out and a representative retry plus the reserved completion work still '
              'fits, narrow independent multiplicity and/or the source/dependency closure once, preserving '
              'actual widths, modes, clock/reset/control, output observability and the suspected mechanism. '
              'Confirm an affordable representative baseline pilot before sealing an optimization variant '
              'or starting alternative search. A successful pilot can count as a repeat only when its exact '
              'measurement identity matches the final comparison. A first timeout alone with sufficient '
              'budget is not a reason to abandon that one useful narrower attempt. If narrowing would '
              'remove the mechanism or the reserved completion work cannot fit, preserve evidence and '
              'write an honest keep_baseline or evidence_incomplete report while turns remain to deliver it. '
              'GROUPED MEASUREMENTS: after the affordable pilot, prefer finite sequential batches over '
              'one native submit and model inspection per repeat. Write a plan P inside W with jobs entries '
              '{job_id, variant, config:<inline normal experiment config>}; optional finalize contains '
              '{variant, baseline_jobs:[IDs], candidate_jobs:[IDs], verification_jobs:[IDs], out:<relative to W>}. '
              'Submit readiness.py batch W --plan P --batch-id ID --tool-seconds S. The sum of the new '
              'jobs\' configured timeouts must fit S; set the native timeout at least S+5 and within '
              'max_job_seconds and remaining native budgets. Split longer groups across bounded batches, '
              'using unchanged measurement configurations. Only explicit reuse:true can reuse an existing '
              'successful exact request; a repeated batch ID observes its original attempt and never resumes '
              'or restarts skipped work. The helper preserves failed evidence and stops on failure. '
              'Preplan the final group to include the functional check and optional finalize referring to '
              'the actual prior/planned receipt IDs, so a successful batch leaves the delivery bundle in '
              'the same job. Read its compact result first; inspect individual receipts/logs only for a '
              'failure, inconsistency or engineering question. A verified finalization needs no per-inner '
              'status/read loop. For single foreground experiments, receipts, tool.log and execution.json '
              'are under W/jobs/<id>; worker.log belongs to detached launch and may be absent.\n'
              'DELIVERY: batch optional finalize uses the normal comparison thresholds and evidence limits. '
              'A separate standalone finalize command remains available with the chosen variant and actual repeated job IDs: '
              'readiness.py finalize W --variant V --baseline-job B (repeat) --candidate-job C (repeat) '
              '--verification-job Q (repeat) --out D. It compares and atomically exports an ordinary '
              'bundle containing comparison.json, candidate.patch, delivery.json and finalization.json. '
              'The controller discovers a successful cleaned native batch or finalize job and freshly verifies '
              'its bundle; no separate result.json, identity copying or model-written path report is '
              'needed. Keep the normal comparison thresholds and evidence limits. End this session '
              'after the finalizing job finishes. Prefer rtl_remote action=handoff with that actual finalization job_id: '
              'the controller freshly verifies, commits an actor/source-bound receipt, and ends this actor '
              'without a further model turn. This is an explicit controlled handoff, distinct from a natural stop. '
              'A rejected handoff leaves the actor active; inspect the error and retain baseline if it cannot be resolved. '
              'Tool responses expose session_budget; start closing when it says phase=closing and avoid new exploration. '
              'For natural-stop delivery, no extra file-reading or JSON-transcription turn is '
              'required for handoff. A local win remains provisional for independent review. '
              'If evidence is insufficient before a comparable candidate exists, the legacy manual '
              'negative report is still available: write result.json in your campaign work scope: '
              '{"decision":"keep_baseline|evidence_incomplete|candidate_for_integration", '
              '"reason":"...", "workspace":"relative workspace directory", '
              '"comparison":"relative comparison JSON path", "delivery":"relative export directory"}. '
              'Manual candidate recommendations need all three paths and actual compare/export artifacts. '
              'Manual report paths are relative to campaign work. '
              'End this session after writing the report.\nSERVER CONTEXT:\n' + _context_text(context, campaign)
              + '\nFROZEN DELIVERY TASK:\n' + context.get('task', ''))
    answer, implementation_label = _session(context, remote, campaign, 'rtl_readiness', prompt)
    wait_for_jobs(remote)
    complete = {'action': 'rtl_readiness_complete', 'campaign': campaign['campaign'],
                'report_path': 'result.json', 'implementation_session_id': implementation_label}
    resolve_request = {'action': 'rtl_readiness_resolve', 'campaign': campaign['campaign']}
    if answer.get('handoff_complete') is True:
        resolve_request['finalization_job_id'] = answer['handoff_job_id']
    resolved = remote.call(resolve_request)
    recommendation = {}
    if resolved.get('found'):
        recommendation = resolved['report']
        complete['finalization_job_id'] = resolved['finalization_job_id']
    elif _finished(answer) and not resolved.get('ambiguous'):
        try:
            recommendation = _json_file(remote, campaign['work_scope'], 'result.json')
        except (RuntimeError, ValueError, KeyError):
            recommendation = {}
    if _finished(answer):
        if recommendation.get('decision') == 'candidate_for_integration':
            if resolved.get('found'):
                evidence_instruction = ('Read @run/' + campaign['work_scope'] + '/' + resolved['report_path']
                                        + ' and the ordinary bundle paths returned by the controller: '
                                        + json.dumps(resolved['report'], ensure_ascii=False) + '. '
                                        'The controller has freshly verified this exact native finalization and ordinary bundle. '
                                        'Independently read its source, generated tests/scripts, actual raw evidence and comparison; '
                                        'mechanical verification is not engineering acceptance. Re-execute standalone validation '
                                        'only if it resolves a concrete question and the remaining job/time budget permits: '
                                        'python @skill/rtl-implementation-readiness/scripts/readiness.py '
                                        'verify-finalization W --delivery D, with W and D rooted in @run/'
                                        + campaign['work_scope'] + '/. ')
            else:
                evidence_instruction = ('Read @run/' + campaign['work_scope'] + '/result.json and its '
                                        'actual source, comparison, export and raw job evidence. '
                                        'Inspect fresh standalone verify output through an outer persistent job: '
                                        'python @skill/rtl-implementation-readiness/scripts/readiness.py verify W '
                                        '--comparison C --delivery D, with report paths rooted in @run/'
                                        + campaign['work_scope'] + '/. ')
            review_prompt = (skill.read_text() + '\nMODE: independent engineering review of a readiness proposal.\n'
                             'Only your campaign audit scope is writable. Baseline, implementation workspace, '
                             'candidate and historical evidence are read-only. ' + evidence_instruction
                             + 'Aliases must be entire argv arguments. '
                             'do not trust caller-written pass flags. Do not run full synthesis. '
                             'Check functional coverage, widths/cycles/reset/memory semantics, representativeness '
                             'and unmeasured full-design costs; a local speedup is not full-chip acceptance. '
                             'Write audit-scope review.json: {"verdict":"accept|revise|insufficient_evidence", '
                             '"comparison_identity":"actual verified identity", "baseline_identity":"...", '
                             '"variant_identity":"...", "reason":"...", "limitations":[]}. '
                             'End after publishing; the controller validates and integrates only changed '
                             'original RTL files after an accept.\nSERVER CONTEXT:\n' + _context_text(context, campaign)
                             + '\nFROZEN DELIVERY TASK:\n' + context.get('task', ''))
            _, review_label = _session(context, remote, campaign, 'readiness_review', review_prompt)
            wait_for_jobs(remote)
            complete.update(review_path='review.json', review_session_id=review_label)
    result = remote.call(complete)
    return {'status': 'rtl_readiness_completed', 'campaign': campaign['campaign'],
            'campaign_state': result, 'adopted': result.get('adopted') is True,
            'decision': result.get('decision', result.get('state')),
            'implementation_session_status': {key: answer.get(key) for key in
                                               ('completed', 'handoff_complete', 'termination_reason', 'handoff_job_id',
                                                'tool_isolation_ok', 'budget_exhausted', 'budget_reason')}}
