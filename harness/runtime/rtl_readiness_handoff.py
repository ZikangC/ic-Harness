"""Controller-owned explicit readiness handoff, distinct from natural model stop.

The model may nominate one completed native finalization job. It cannot supply
the receipt, completion flag, bundle identity, or terminal witness. No historical
failed session is recovered through this API.
"""
import json

from harness.runtime import rtl_readiness as readiness
from harness.runtime import rtl_readback_store as store


def _binding(out, request, *, active):
    actor, actor_path = store._actor(out, request.get('session_id'))
    actor_sha = store._sha(store._raw(actor_path))
    if request.get('actor_sha256') != actor_sha:
        raise ValueError('readiness handoff actor fingerprint mismatch')
    campaign = readiness.load(out, request.get('campaign'))
    if actor.get('role') != 'rtl_readiness' or actor.get('scope') != campaign['work_scope']:
        raise ValueError('readiness handoff requires its exact implementation actor and campaign')
    if store._sha(store._raw(store._path(out, 'control/' + actor['session_id'] + '.prompt.txt'))) != actor['prompt_sha256']:
        raise ValueError('readiness handoff registered prompt changed')
    if active:
        if campaign.get('state') != 'active':
            raise ValueError('readiness handoff campaign is not active')
        if store._path(out, 'control/sessions/' + actor['session_id'] + '.end.json').exists():
            raise ValueError('readiness handoff session has already ended')
        readiness.check_baseline(out, campaign)
    job_id = request.get('job_id')
    resolved = readiness.resolve(out, {'campaign': campaign['campaign'], 'finalization_job_id': job_id},
                                 _include_verification=True)
    if not resolved.get('found') or resolved.get('finalization_job_id') != job_id:
        raise ValueError('readiness handoff finalization is not uniquely verified: ' + resolved.get('reason', 'missing job'))
    verified = resolved.pop('_verification')
    if resolved['report']['decision'] == 'candidate_for_integration':
        readiness.integration_updates(out, campaign, verified, check_live_baseline=active)
    job = readiness.owned(out, 'jobs/' + job_id)
    job_request = json.loads(store._raw(job / 'request.json'))
    if (not isinstance(job_request, dict) or job_request.get('session_id') != actor['session_id']
            or job_request.get('session_actor_sha256') != actor_sha):
        raise ValueError('readiness handoff finalization job belongs to a different session')
    report = readiness.owned(out / campaign['work_scope'], resolved['report_path'])
    comparison = readiness.owned(out / campaign['work_scope'], resolved['report']['comparison'])
    pins = {}
    for path in (job / 'request.json', job / 'result.json', report, comparison):
        raw = store._raw(path)
        pins[str(path.relative_to(out))] = {'sha256': store._sha(raw), 'bytes': len(raw)}
    return dict(version=1, run_id=out.name, session_id=actor['session_id'], actor_sha256=actor_sha,
                campaign=campaign['campaign'], campaign_source_sha256=campaign['source_sha256'],
                scope=actor['scope'], job_id=job_id, finalization=resolved, source_pins=pins)


def _response(out, receipt, path):
    return {'accepted': True, 'session_id': receipt['session_id'], 'job_id': receipt['job_id'],
            'receipt_path': str(path.relative_to(out)), 'receipt_sha256': store._sha(store._raw(path)),
            'finalization_identity': receipt['finalization']['finalization_identity']}


def commit(out, request, require_idle):
    """Commit once while active; repeat requests only read the same exact receipt."""
    binding = _binding(out, request, active=True)
    require_idle(out)
    path = store._path(out, 'control/sessions/' + binding['session_id'] + '.handoff.json')
    if path.exists():
        return status(out, request)
    receipt = dict(binding, committed_utc=readiness.now())
    store._once(path, receipt)
    return _response(out, receipt, path)


def status(out, request):
    """Fresh bundle/source check also works after adoption or session termination."""
    binding = _binding(out, request, active=False)
    path = store._path(out, 'control/sessions/' + binding['session_id'] + '.handoff.json')
    receipt = store._json(path)
    if (store._raw(path) != store._encoded(receipt)
            or {key: value for key, value in receipt.items() if key != 'committed_utc'} != binding
            or not isinstance(receipt.get('committed_utc'), str) or not receipt['committed_utc']):
        raise ValueError('readiness handoff receipt or source binding changed')
    return _response(out, receipt, path)


def terminal(out, actor, result, raw_events):
    """Validate the controller result against native receipt and actual tool pair."""
    if actor.get('role') != 'rtl_readiness':
        raise ValueError('only readiness implementation may complete by handoff')
    path = store._path(out, 'control/sessions/' + actor['session_id'] + '.handoff.json')
    receipt = store._json(path)
    response = status(out, {'session_id': actor['session_id'], 'actor_sha256': receipt.get('actor_sha256'),
                            'campaign': receipt.get('campaign'), 'job_id': result.get('handoff_job_id')})
    if (result.get('completed') is not False or result.get('handoff_complete') is not True
            or result.get('termination_reason') != 'verified_handoff'
            or result.get('handoff_receipt_sha256') != response['receipt_sha256']
            or result.get('budget_exhausted') is not False or result.get('budget_reason') is not None
            or type(result.get('exit_code')) is not int):
        raise ValueError('readiness controlled handoff result is invalid')
    limits = result.get('readiness_limits')
    campaign = readiness.load(out, receipt['campaign'])
    if (not isinstance(limits, dict) or any(type(limits.get(key)) is not int or limits[key] <= 0
            or limits[key] != campaign['budgets'][key] for key in ('assistant_turns', 'session_seconds'))):
        raise ValueError('readiness handoff limits differ from campaign')
    turns, elapsed = result.get('assistant_turns'), result.get('elapsed_seconds')
    if (type(turns) is not int or turns < 0 or turns >= limits['assistant_turns']
            or isinstance(elapsed, bool) or not isinstance(elapsed, (int, float))
            or not 0 <= elapsed < limits['session_seconds']):
        raise ValueError('readiness handoff was not within session budget')
    calls = {}; ended = set(); matched = 0
    for line in raw_events.decode('utf-8').split('\n'):
        if not line.strip():
            continue
        event = json.loads(line)
        if not isinstance(event, dict):
            raise ValueError('invalid readiness handoff event')
        kind = event.get('type')
        if kind not in ('tool_execution_start', 'tool_execution_end'):
            continue
        call_id = event.get('toolCallId')
        if not isinstance(call_id, str) or not call_id:
            raise ValueError('invalid readiness handoff tool identity')
        if 'session_id' in event and event['session_id'] != actor['session_id']:
            raise ValueError('readiness handoff event session mismatch')
        if kind == 'tool_execution_start':
            if call_id in calls or call_id in ended:
                raise ValueError('duplicate readiness handoff tool identity')
            calls[call_id] = event
            continue
        if call_id in ended:
            raise ValueError('duplicate readiness handoff tool result')
        ended.add(call_id)
        start = calls.pop(call_id, None)
        if start is None or start.get('toolName') != event.get('toolName'):
            raise ValueError('unpaired readiness handoff tool result')
        args = start.get('args')
        if start.get('toolName') != 'rtl_remote' or not isinstance(args, dict) or args.get('action') != 'handoff':
            continue
        public = event.get('result')
        if (args.get('job_id') != response['job_id'] or event.get('isError') is not False
                or not isinstance(public, dict) or public.get('isError', False) is not False):
            raise ValueError('readiness handoff tool failed or selected a different job')
        content = public.get('content')
        if (not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict)
                or content[0].get('type') != 'text'
                or not isinstance(content[0].get('text'), str)):
            raise ValueError('invalid readiness handoff public result')
        payload = json.loads(content[0]['text'])
        if not isinstance(payload, dict) or any(payload.get(key) != response[key]
                for key in ('accepted', 'session_id', 'job_id', 'receipt_sha256')):
            raise ValueError('readiness handoff public result binding mismatch')
        matched += 1
    if matched != 1:
        raise ValueError('readiness handoff requires one actual successful tool pair')
    return path
