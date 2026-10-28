"""Harness-owned RTL review and optional repair orchestration.

The audit skill supplies engineering judgments. Callers explicitly select the
repair executor; an independent review never chooses one implicitly.
"""
from dataclasses import dataclass
from typing import Callable
import json
import time
from hashlib import sha256
from pathlib import Path

from harness.adapters.omp_tools import run_session
from harness.adapters.rtl_remote import wait_for_jobs


@dataclass(frozen=True)
class RepairSession:
    """A caller-selected executor's prompt and extra context, not its verdict."""
    prompt: str
    role: str = 'implementation'
    context_updates: dict | None = None


@dataclass(frozen=True)
class RepairHandler:
    """Named repair policy; name is persisted to prevent cross-executor reuse."""
    name: str
    prepare: Callable

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip() or not callable(self.prepare):
            raise ValueError('repair handler requires a stable name and callable prepare')


def _review_scope(review):
    # Legacy records remain readable but cannot grant a new release.
    scope = review.get('review_scope')
    if 'review_scope' in review and scope not in ('candidate', 'change'):
        raise ValueError('unknown RTL review_scope')
    decision = review.get('decision')
    if decision is not None and decision.get('review_scope') != scope:
        raise ValueError('RTL decision review_scope differs from frozen review')
    return scope


def _completed(answer):
    return (answer.get('completed') is True and answer.get('tool_isolation_ok') is True
            and not answer.get('budget_exhausted'))


def _ended(receipt):
    # Initial resume placeholders have no integer terminal exit code.
    # A failed ended actor may be replaced, but never supplies acceptance.
    return bool(receipt) and (_completed(receipt) or type(receipt.get('exit_code')) is int)


def _receipt(remote, review, kind):
    try:
        response = remote.call({'action': 'read', 'scope': 'work',
                                'path': f'@run/control/rtl-audit/{review["review"]}.{kind}.json',
                                'lines': 300})
    except RuntimeError as error:
        if 'not found' not in str(error) and 'No such file' not in str(error):
            raise
        return None
    # Read returns numbered lines for the model; receipts need exact JSON bytes.
    if not response.get('eof', True) or response.get('truncated', False):
        raise ValueError('session receipt must fit one exact read')
    text = response.get('content')
    if text is None:
        text = '\n'.join(line.split(': ', 1)[1] for line in response['text'].splitlines())
    receipt = json.loads(text)
    if (receipt.get('review_scope') != _review_scope(review) or
            receipt.get('candidate_sha256') != review['candidate_sha256'] or
            receipt.get('decision_report_sha256') !=
            (review.get('decision') or {}).get('report_sha256')):
        return None
    return receipt


def _save_receipt(remote, review, kind, answer, label, repair_actor=None, started_evidence_sha256=None):
    # Final prose stays in the full session result, not in this resume marker.
    remote.evidence(f'rtl-audit/{review["review"]}.{kind}.json', json.dumps({
        k: answer.get(k) for k in ('completed', 'tool_isolation_ok', 'exit_code',
                                  'stop_reason', 'model', 'role', 'budget_exhausted')} | {
            'session_label': label, 'candidate_sha256': review['candidate_sha256'],
            'review_scope': _review_scope(review),
            'decision_report_sha256': (review.get('decision') or {}).get('report_sha256'),
            'evidence_sha256': review.get('evidence_sha256'),
            'started_evidence_sha256': started_evidence_sha256,
            **({'repair_actor': repair_actor} if repair_actor is not None else {})},
            indent=2))


def _session(context, remote, review, kind, prompt, scope, role, *, repair_actor=None, context_updates=None):
    cache = Path(context['cache'])
    label = f'rtl-{kind}-{review["review"]}-{time.time_ns()}'
    child = dict(context, role=role, scope=scope, review=review['review'],
                 review_scope=_review_scope(review))
    # Each actor is fresh; no parent actor or repair capability leaks to audit.
    for key in ('session_id', 'session_actor_sha256', 'candidate_scope', 'repair_skill', 'repair_actor'):
        child.pop(key, None)
    updates = {} if context_updates is None else context_updates
    if not isinstance(updates, dict) or any(key in context or key in
            ('role', 'scope', 'review', 'candidate_scope', 'session_id',
             'session_actor_sha256', 'repair_actor', 'review_scope') for key in updates):
        raise ValueError('repair context cannot override frozen context or isolation bindings')
    child.update(updates)
    if repair_actor is not None:
        child['repair_actor'] = repair_actor
    # Model selection reuses existing configured roles; no project/model defaults.
    model_role = 'review' if role == 'rtl_audit' else 'implementation'
    path = cache / (label + '.context.json')
    path.write_text(json.dumps(child))
    started_evidence = review.get('evidence_sha256')
    _save_receipt(remote, review, kind, {'completed': False,
                  'tool_isolation_ok': False}, label, repair_actor, started_evidence)
    answer = run_session(prompt, model_role, context['models'],
                         Path(context['project_root']), path, cache, remote, label)
    latest = remote.call({'action': 'rtl_review_status', 'review': review['review']})
    if _review_scope(latest) != _review_scope(review):
        raise ValueError('RTL review_scope changed during model session')
    _save_receipt(remote, latest, kind, answer, label, repair_actor, started_evidence)
    return answer


def _context_text(context, review):
    return json.dumps({k: context[k] for k in ('remote_run_dir', 'tools',
                       'capabilities', 'reference_path', 'input_source_paths')}
                      | {'review_scope': _review_scope(review)}
                      | {k: review[k] for k in ('review', 'audit_scope',
                         'candidate_scope', 'candidate_sha256', 'architecture_scope',
                         'architecture_identity_sha256', 'decision') if k in review},
                      ensure_ascii=False)


def _delivery_task(context):
    # The same caller task used to initialize the frozen run must reach both
    # independent roles. A source optimization target and a required delivery
    # objective may have different dispositions; the controller does not judge it.
    task = context.get('task')
    expected = context.get('settings', {}).get('task_sha256')
    if not isinstance(task, str) or sha256(task.encode()).hexdigest() != expected:
        raise ValueError('frozen delivery task missing or fingerprint mismatch')
    return '\nFROZEN DELIVERY TASK (SHA-256 ' + expected + '):\n' + task + '\n'


def _frozen_architecture(remote, review):
    # This opt-in action is controller-only, not a registered model tool.
    # Transport/worker errors propagate; absence is never an empty baseline.
    current = remote.call({'action': 'rtl_review_status', 'review': review['review'],
                           'include_architecture_context': True, 'review_scope': _review_scope(review)})
    if _review_scope(current) != _review_scope(review):
        raise ValueError('frozen architecture context has stale review_scope binding')
    for key in ('review', 'candidate_sha256', 'architecture_scope',
                'architecture_identity_sha256', 'decision'):
        if key not in review or current.get(key) != review[key]:
            raise ValueError('frozen architecture context has stale review binding')
    value = current.get('architecture_context')
    if not isinstance(value, dict) or value.get('version') != 1:
        raise ValueError('frozen architecture context missing or incomplete')
    identity = value.get('identity')
    if (not isinstance(identity, dict) or not isinstance(identity.get('current'), dict)
            or not isinstance(identity.get('inventory'), dict)):
        raise ValueError('frozen architecture identity missing or incomplete')
    identity_sha = sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    scope = review['architecture_scope']
    if (value.get('review') != review['review'] or value.get('candidate_sha256') != review['candidate_sha256']
            or value.get('scope') != scope or value.get('identity_sha256') != identity_sha
            or identity_sha != review['architecture_identity_sha256']
            or type(value.get('matches_run_current')) is not bool
            or value['matches_run_current'] != current.get('architecture_current')
            or not isinstance(value.get('run_current'), dict)
            or (value['matches_run_current'] and value['run_current'] != identity['current'])):
        raise ValueError('frozen architecture context identity mismatch')
    inventory = identity['inventory']
    files = value.get('files')
    if not isinstance(files, dict):
        raise ValueError('frozen architecture file index missing')
    expected_files = {}
    for name, entry in inventory.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            raise ValueError('invalid frozen architecture inventory')
        target = inventory.get(entry.get('target'), {}) if entry.get('type') == 'symlink' else entry
        if target.get('type') == 'file':
            expected_files[name] = target.get('sha256')
    if set(files) != set(expected_files):
        raise ValueError('frozen architecture file index incomplete')
    for name, expected_sha in expected_files.items():
        item = files[name]
        if (not isinstance(item, dict) or item.get('path') != '@run/' + scope + '/' + name
                or item.get('sha256') != expected_sha or not isinstance(expected_sha, str)
                or len(expected_sha) != 64 or type(item.get('bytes')) is not int or item['bytes'] < 0):
            raise ValueError('frozen architecture file index mismatch')
    primary = value.get('primary')
    names = ('architecture.md', 'SOURCE.md', 'decision.json')
    if not isinstance(primary, dict) or set(primary) != set(names) or 'architecture.md' not in files:
        raise ValueError('frozen architecture primary sources incomplete')
    sections = []
    for name in names:
        item = primary[name]
        path = '@run/' + scope + '/' + name
        if name not in inventory:
            if item != {'status': 'missing', 'path': path}:
                raise ValueError('missing architecture source must be explicit')
            sections.append('\nMISSING IN FROZEN PACKET: ' + path + '\n')
            continue
        if name not in files or not isinstance(item, dict):
            raise ValueError('frozen architecture primary source is not a file')
        content = item.get('content')
        if (item.get('status') != 'present' or not isinstance(content, str)
                or any(item.get(key) != files[name][key] for key in ('path', 'sha256', 'bytes'))
                or len(content.encode('utf-8')) != item['bytes']
                or sha256(content.encode('utf-8')).hexdigest() != item['sha256']):
            raise ValueError('frozen architecture primary source delivery incomplete or changed')
        sections.append('\nRAW FROZEN FILE ' + path + ' (SHA-256 ' + item['sha256']
                        + '; UTF-8 bytes ' + str(item['bytes']) + '):\n' + content
                        + '\nEND RAW FROZEN FILE ' + path + '\n')
    metadata = {key: value[key] for key in ('version', 'review', 'candidate_sha256', 'scope',
                'identity', 'identity_sha256', 'run_current', 'matches_run_current', 'files')}
    return ('\nFROZEN CANDIDATE ARCHITECTURE PRIMARY SOURCES:\n'
            'These are the packet frozen with this candidate, not a claim of current RTL validity. '
            'run_current is reported separately; it may differ. Read these source files and their '
            'native provenance/decision before attributing claims to this baseline. Other complete '
            'packet files are indexed below for source-bound paginated reads. A required '
            'parent_readback witness in identity is distinct from adoption: it must be complete '
            'and bound to this baseline and original parent. Your reads cannot replace it. '
            'An ended parent with incomplete evidence is a concrete capability/evidence block, '
            'not a reason for indefinite equivalent repair sessions.\n'
            + json.dumps(metadata, ensure_ascii=False, indent=2) + '\n' + ''.join(sections))



def _decision_result(review):
    decision = review['decision']
    return {'status': 'rtl_review_finished', 'review': review['review'],
            'review_verdict': decision['verdict'],
            'review_scope': _review_scope(review),
            'rtl_stage_release': decision['verdict'] == 'pass' and _review_scope(review) == 'candidate',
            'candidate_sha256': review['candidate_sha256'],
            'candidate_scope': review['candidate_scope'],
            'report_path': decision['report_path'], 'report_sha256': decision['report_sha256']}


def _reserve_repair(remote, review, actor, limit):
    """Reserve a round before mutable work begins; the server record survives resume."""
    if limit is None:
        return True
    name = 'rtl-audit/repair-budget-' + sha256(actor.encode()).hexdigest() + '.json'
    created = False
    try:
        response = remote.call({'action': 'read', 'scope': 'work',
                                'path': '@run/control/' + name, 'lines': 300})
    except RuntimeError as error:
        # Match the worker's concrete missing-file response, not arbitrary transport,
        # integrity or permission errors, which must remain visible to the caller.
        if 'not found' not in str(error) and 'No such file' not in str(error):
            raise
        ledger = {'version': 1, 'repair_actor': actor, 'limit': limit, 'rounds': {}}
        created = True
    else:
        if not response.get('eof', True) or response.get('truncated', False):
            raise ValueError('repair budget ledger must fit one exact read')
        text = response.get('content')
        if text is None:
            text = '\n'.join(line.split(': ', 1)[1] for line in response['text'].splitlines())
        ledger = json.loads(text)
    if (not isinstance(ledger, dict) or type(ledger.get('version')) is not int or ledger.get('version') != 1
            or ledger.get('repair_actor') != actor or type(ledger.get('limit')) is not int or ledger.get('limit') != limit
            or not isinstance(ledger.get('rounds'), dict)):
        raise ValueError('persistent repair budget binding mismatch')
    rounds = ledger['rounds']
    if any(not isinstance(key, str) or not isinstance(value, dict) or
           set(value) != {'candidate_sha256', 'decision_report_sha256'} or
           any(not isinstance(item, str) or not item for item in value.values())
           for key, value in rounds.items()):
        raise ValueError('persistent repair budget rounds are invalid')
    if created:
        remote.evidence(name, json.dumps(ledger, indent=2))
    binding = {'candidate_sha256': review['candidate_sha256'],
               'decision_report_sha256': review['decision']['report_sha256']}
    if review['review'] in rounds:
        if rounds[review['review']] != binding:
            raise ValueError('persistent repair round has stale candidate/report binding')
        return True
    if len(rounds) >= limit:
        return False
    rounds[review['review']] = binding
    remote.evidence(name, json.dumps(ledger, indent=2))
    return True

def review_once(context, remote, *, review_scope='candidate'):
    """Review a frozen candidate without selecting or invoking a repair executor.

    Source/session guards may require a fresh scope; no RTL repair is started.
    """
    return run(context, remote, review_scope=review_scope)


def _repair_limit(context, limit):
    if limit is None:
        limit = context.get('settings', {}).get('rtl_audit', {}).get('max_repairs', 3)
    if type(limit) is not int or limit < 0:
        raise ValueError('max_repairs must be a finite non-negative integer')
    return limit


def _new_progress(review, receipt, key='evidence_sha256'):
    """A new native record permits another judgment; it never establishes pass."""
    if review.get('work_current') is False or review.get('architecture_current') is False:
        return True
    before, after = receipt.get(key), review.get('evidence_sha256')
    return isinstance(before, str) and isinstance(after, str) and before != after


def run(context, remote, before_freeze=None, *, repair_handler=None, max_repairs=None,
        review_scope='candidate'):
    """Follow independent review decisions using only an explicitly supplied repair policy.

    max_repairs is a persistent per-executor round budget, not a model retry budget.
    A failed/interrupted model stops this invocation; resume preserves reservations.
    review_scope is caller-owned; only an explicit candidate pass releases RTL.
    """
    if review_scope not in ('candidate', 'change'):
        raise ValueError('unknown RTL review_scope')
    if repair_handler is not None and not isinstance(repair_handler, RepairHandler):
        raise TypeError('repair_handler must be a named RepairHandler')
    max_repairs = _repair_limit(context, max_repairs)
    harness = Path(context['harness_dir'])
    skill_path = harness / 'skills/rtl-audit/SKILL.md'
    if not skill_path.is_file():
        return {'status': 'rtl_audit_not_run', 'review_verdict': 'not_run',
                'rtl_stage_release': False, 'reason': 'optional RTL audit skill unavailable'}

    # Generation or a prior interrupted client may have left detached tools running.
    wait_for_jobs(remote)
    current = remote.call({'action': 'rtl_review_status'})
    if current.get('review') and _review_scope(current) != review_scope:
        # A narrower result and untyped legacy records cannot satisfy this
        # request. Preserve active actors until their controller receipt ends.
        if (current.get('repair') or {}).get('status') == 'active':
            raise ValueError('existing RTL review_scope differs; active repair cannot be migrated')
        receipt = _receipt(remote, current, 'audit')
        if not _ended(receipt):
            raise ValueError('existing RTL review_scope differs; unfinished review cannot be replaced')
        current = remote.call({'action': 'rtl_review_begin', 'review_scope': review_scope})
    if not current.get('review'):
        current = remote.call({'action': 'rtl_review_begin', 'review_scope': review_scope})
    while True:
        if _review_scope(current) != review_scope:
            raise ValueError('RTL review_scope differs from caller request')
        review = current['review']
        decision = current.get('decision')
        if decision is None:
            prompt = (skill_path.read_text() + '\nMODE: independent RTL audit.\n'
                      + _delivery_task(context)
                      + _frozen_architecture(remote, current)
                      + 'SERVER CONTEXT:\n' + _context_text(context, current) + '\n'
                      'REVIEW SCOPE: ' + review_scope + '. Copy this exact value into '
                      'the report review_scope field. candidate requests full candidate '
                      'acceptance; change requests acceptance of the delivered modification '
                      'and its relevant effects, with inherited baseline limitations kept '
                      'explicit. A change pass never releases the whole RTL stage.\n'
                      'The candidate is immutable. All relative writes and jobs belong '
                      'to the supplied audit scope. Read the original sources directly. '
                      'Prior test files/results may have been copied for continuity; '
                      'they are historical until verified against this candidate. '
                      'Inspect persistent jobs and existing results before submitting '
                      'commands; reuse valid execution evidence instead of duplicating jobs. '
                      'Publish your report with rtl_request_revision or rtl_finish_review. '
                      'End this session after publishing; the caller handles the next role.')
            answer = _session(context, remote, current, 'audit', prompt,
                              current['audit_scope'], 'rtl_audit')
            if not _completed(answer):
                return {'status': 'rtl_audit_session_failure', 'review': review,
                        'rtl_stage_release': False}
            drained = wait_for_jobs(remote)
            current = remote.call({'action': 'rtl_review_status', 'review': review})
            decision = current.get('decision')
            if decision is None:
                if drained['waited']:
                    # New terminal evidence needs an auditor's judgment, never an
                    # automatic pass. No-pending incomplete sessions still stop.
                    continue
                return {'status': 'rtl_audit_incomplete', 'review': review,
                        'rtl_stage_release': False,
                        'reason': 'audit session ended without a bound decision'}
        else:
            receipt = _receipt(remote, current, 'audit')
            if not receipt or not _completed(receipt):
                # A crash may fall between publication and the session receipt.
                # Preserve that decision, but obtain a fresh independent review.
                current = remote.call({'action': 'rtl_review_begin', 'review_scope': review_scope})
                continue
            if decision['verdict'] in ('blocked', 'insufficient_evidence'):
                # An explicit resume is not itself new engineering evidence.
                # Preserve the cause/report until source, architecture or native
                # job metadata changes; neither outcome selects an RTL repair.
                if not _new_progress(current, receipt):
                    return _decision_result(current)
                current = remote.call({'action': 'rtl_review_begin', 'review_scope': review_scope})
                continue

        if decision['verdict'] != 'revise':
            if not current.get('architecture_current', True) or not current.get('work_current', True):
                current = remote.call({'action': 'rtl_review_begin', 'review_scope': review_scope})
                continue
            return _decision_result(current)

        readback = current.get('parent_readback')
        if (isinstance(readback, dict) and readback.get('required') is True
                and readback.get('complete') is False and readback.get('session_ended') is True):
            # Preserve the independent revise decision. Another implementation
            # session cannot add exposure to this already-ended original parent.
            return {'status': 'rtl_parent_readback_incomplete', 'review': review,
                    'review_verdict': decision['verdict'], 'rtl_stage_release': False,
                    'candidate_sha256': current['candidate_sha256'],
                    'report_path': decision['report_path'], 'report_sha256': decision['report_sha256'],
                    'parent_readback': readback,
                    'reason': 'Required original-parent readback is incomplete after that session ended; equivalent repair sessions cannot replace it.'}

        if repair_handler is None:
            return _decision_result(current) | {'repair_required': True}

        repair = current.get('repair') or {}
        prior = _receipt(remote, current, 'repair')
        if (repair.get('status') != 'completed' and prior is not None and prior.get('repair_actor') != repair_handler.name
                or repair.get('status') == 'completed' and
                   (not prior or not _completed(prior))):
            return _decision_result(current) | {
                'status': 'rtl_repair_actor_mismatch', 'repair_required': True,
                'reason': 'Recorded repair lacks a completed receipt for the caller-selected executor.'}
        # A completed old repair may only need its next freeze after a crash.
        # Its source-bound receipt proves the old executor; the caller's policy
        # applies to the next candidate. Do not charge or run that repair twice.
        if (repair.get('status') != 'completed' and
                not _reserve_repair(remote, current, repair_handler.name, max_repairs)):
            return _decision_result(current) | {
                'status': 'rtl_repair_budget_exhausted', 'repair_required': True,
                'reason': 'Persistent repair-round budget exhausted; independent revise decision retained.'}
        if repair.get('status') != 'completed':
            current = remote.call({'action': 'rtl_repair_begin', 'review': review})
            if not prior or not _completed(prior):
                request = repair_handler.prepare(context, remote, current)
                if not isinstance(request, RepairSession) or request.role != 'implementation':
                    raise ValueError('repair executor must prepare an implementation RepairSession')
                if not isinstance(request.prompt, str) or not request.prompt.strip():
                    raise ValueError('repair executor prompt missing')
                answer = _session(context, remote, current, 'repair', request.prompt,
                                  'work', request.role, repair_actor=repair_handler.name,
                                  context_updates=request.context_updates)
                if not _completed(answer):
                    return {'status': 'rtl_repair_session_failure', 'review': review,
                            'rtl_stage_release': False}
            wait_for_jobs(remote)
            current = remote.call({'action': 'rtl_repair_end', 'review': review})
        repair_receipt = _receipt(remote, current, 'repair')
        if (current.get('work_current') is True and current.get('architecture_current') is True
                and repair_receipt and not _new_progress(current, repair_receipt, 'started_evidence_sha256')):
            return _decision_result(current) | {
                'status': 'rtl_repair_no_progress', 'repair_required': True,
                'reason': 'Repair ended without a source/architecture change or new native job evidence; original issues remain open.'}
        if before_freeze is not None:
            selected = before_freeze()
            if selected is not None:
                if (not isinstance(selected, dict) or set(selected) != {'repair_handler', 'max_repairs'}
                        or not isinstance(selected['repair_handler'], RepairHandler)):
                    raise ValueError('before_freeze must return an explicit repair handler and finite budget')
                repair_handler = selected['repair_handler']
                max_repairs = _repair_limit(context, selected['max_repairs'])
        # A callback may have submitted jobs. Freeze only after all jobs drain.
        wait_for_jobs(remote)
        current = remote.call({'action': 'rtl_review_begin', 'seed_review': review,
                               'review_scope': review_scope})
