"""Controller-owned session and architecture readback records.

Only the runner registers/ends actors. Model tools do not expose these actions.
All terminal evidence is read from its registered control-file names; caller
completion flags and caller event fingerprints are never accepted.
"""
import hashlib
import json
import re
from pathlib import Path

from harness.runtime.rtl_readback import verify_readback


REQUIRED_FILES = ('architecture.md', 'design.json', 'CHANGES.md', 'budgets.json',
                  'review.json', 'decision.json', 'SOURCE.md', 'budget_binding.json',
                  'budget.execution.json')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _path(out, relative):
    path = out / relative
    resolved = path.resolve()
    resolved.relative_to(out.resolve())
    if resolved != path or any(p.is_symlink() for p in [path] + list(path.parents) if p != out.parent):
        raise ValueError('readback control path is aliased')
    return path


def _label(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,199}', value):
        raise ValueError('invalid session label')
    return value


def _raw(path):
    if not path.is_file() or path.is_symlink():
        raise ValueError('session control source missing or aliased')
    return path.read_bytes()


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(path):
    value = json.loads(_raw(path))
    if not isinstance(value, dict):
        raise ValueError('session control record must be an object')
    return value


def _encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()


def _once(path, value, same=False):
    raw = _encoded(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open('xb') as stream:
            stream.write(raw)
    except FileExistsError:
        if not same or _raw(path) != raw:
            raise ValueError('immutable session record already exists')
    return _sha(raw)


def _actor(out, session_id):
    session_id = _label(session_id)
    path = _path(out, 'control/sessions/' + session_id + '.json')
    actor = _json(path)
    if (actor.get('version') != 1 or actor.get('session_id') != session_id
            or actor.get('label') != session_id or actor.get('run_id') != out.name):
        raise ValueError('registered session identity mismatch')
    if _raw(path) != _encoded(actor):
        raise ValueError('registered session original bytes changed')
    return actor, path


def begin_session(out, request):
    session_id = _label(request.get('session_id'))
    if request.get('label', session_id) != session_id:
        raise ValueError('session label differs from session identity')
    role = request.get('role'); scope = request.get('scope')
    if role not in ('implementation', 'architecture', 'review', 'rtl_audit', 'rtl_readiness', 'readiness_review', 'readiness_diagnosis'):
        raise ValueError('unsupported context session role')
    if scope != 'work' and (not isinstance(scope, str) or not re.fullmatch(r'(architecture/revision-[0-9]+/(candidate|audit)|audit/review-[1-9][0-9]*|readiness/campaign-[1-9][0-9]*/(work|audit))', scope)):
        raise ValueError('unsupported context session scope')
    if role == 'readiness_diagnosis' and scope != 'work':
        raise ValueError('read-only diagnosis requires work scope')
    if role in ('rtl_readiness', 'readiness_review'):
        from harness.runtime import rtl_readiness
        rtl_readiness.from_scope(out, scope, writable=True)
        expected = 'work' if role == 'rtl_readiness' else 'audit'
        if not scope.endswith('/' + expected):
            raise ValueError('readiness actor role differs from exact scope')
    elif isinstance(scope, str) and scope.startswith('readiness/'):
        raise ValueError('readiness scope requires a readiness actor role')
    model_role = request.get('model_role')
    if model_role is not None and (not isinstance(model_role, str) or not model_role):
        raise ValueError('invalid session model role')
    if role in ('rtl_readiness', 'readiness_review') and model_role != ('implementation' if role == 'rtl_readiness' else 'review'):
        raise ValueError('readiness actor must retain its configured model role')
    if role == 'readiness_diagnosis' and model_role != 'implementation':
        raise ValueError('diagnosis actor must retain its configured implementation model role')
    diagnosis_pins = {}
    if role == 'readiness_diagnosis':
        from harness.runtime import rtl_diagnosis
        _, observation, _ = rtl_diagnosis._load(out, request.get('diagnosis_observation_id'))
        if observation['state'] != 'active' or request.get('diagnosis_fingerprint') != observation['fingerprint']:
            raise ValueError('diagnosis invocation must bind its active observation')
        diagnosis_pins = {key: request[key] for key in ('diagnosis_observation_id', 'diagnosis_fingerprint')}
    prompt = _path(out, 'control/' + session_id + '.prompt.txt')
    actor = {'version': 1, 'run_id': out.name, 'session_id': session_id, 'label': session_id,
             'role': role, 'scope': scope, 'model_role': model_role, 'prompt_sha256': _sha(_raw(prompt)), **diagnosis_pins}
    path = _path(out, 'control/sessions/' + session_id + '.json')
    return {'registered': True, 'actor': actor, 'actor_path': str(path.relative_to(out)), 'actor_sha256': _once(path, actor)}


def require_active_parent(out, session_id, actor_sha256):
    actor, path = _actor(out, session_id)
    if actor_sha256 != _sha(_raw(path)):
        raise ValueError('registered parent actor fingerprint changed')
    if actor.get('role') != 'implementation' or actor.get('scope') != 'work':
        raise ValueError('architecture revision needs its actual implementation parent')
    if _path(out, 'control/sessions/' + actor['session_id'] + '.end.json').exists():
        raise ValueError('architecture revision parent session has ended')
    if _sha(_raw(_path(out, 'control/' + actor['session_id'] + '.prompt.txt'))) != actor['prompt_sha256']:
        raise ValueError('registered parent prompt changed')
    return actor


def required_files(out, baseline, published_root=None):
    """Index the complete generic native main packet, never infer engineering fields."""
    rows = []; seen = set()
    for name in REQUIRED_FILES:
        logical = baseline / name
        if not logical.is_file():
            raise ValueError('architecture readback packet missing required file: ' + name)
        actual = logical.resolve(); actual.relative_to(baseline.resolve())
        canonical = str(((published_root or baseline) / actual.relative_to(baseline.resolve())).relative_to(out.resolve()))
        if canonical in seen:
            raise ValueError('architecture readback main packet has aliased required files')
        seen.add(canonical); raw = actual.read_bytes()
        rows.append({'resolved_run_path': canonical, 'sha256': _sha(raw), 'bytes': len(raw)})
    return rows


def _packet(out, current, closure):
    if _json(_path(out, 'architecture-current.json')) != current:
        raise ValueError('architecture current changed during readback check')
    if _digest(closure) != current.get('baseline_sha256'):
        raise ValueError('architecture readback baseline identity changed')
    baseline = out / current['path']; baseline.resolve().relative_to(out.resolve())
    handoff_path = _path(out, current['handoff_path']); handoff_raw = _raw(handoff_path)
    if _sha(handoff_raw) != current.get('handoff_sha256'):
        raise ValueError('architecture readback handoff changed')
    handoff = json.loads(handoff_raw)
    for key in ('path', 'baseline_sha256', 'candidate_path', 'candidate_sha256', 'parent_session_id', 'parent_actor_sha256', 'readback_required_files'):
        if handoff.get(key) != current.get(key):
            raise ValueError('architecture readback current/handoff binding mismatch')
    actual = required_files(out, baseline)
    if actual != current.get('readback_required_files'):
        raise ValueError('architecture readback required packet changed')
    return {'run_id': out.name, 'parent_session_id': current['parent_session_id'],
            'baseline_path': current['path'], 'baseline_sha256': current['baseline_sha256'],
            'handoff_path': current['handoff_path'], 'handoff_sha256': current['handoff_sha256'],
            'required_files': actual}


def _terminal(out, actor):
    sources = {}; content = {}
    for suffix in ('prompt.txt', 'result.json', 'isolation.json', 'events.jsonl'):
        name = 'control/' + actor['session_id'] + '.' + suffix
        raw = _raw(_path(out, name)); content[suffix] = raw
        sources[name] = {'sha256': _sha(raw), 'bytes': len(raw)}
    if sources['control/' + actor['session_id'] + '.prompt.txt']['sha256'] != actor['prompt_sha256']:
        raise ValueError('registered session prompt changed')
    result = json.loads(content['result.json']); isolation = json.loads(content['isolation.json'])
    if not isinstance(result, dict) or not isinstance(isolation, dict):
        raise ValueError('invalid terminal session evidence')
    if actor.get('model_role') is not None and result.get('role') != actor['model_role']:
        raise ValueError('terminal model role differs from registered invocation')
    expected = ({'rtl_remote'} if actor['role'] in ('rtl_readiness', 'readiness_review', 'readiness_diagnosis') else
                {'rtl_remote', 'rtl_request_revision', 'rtl_finish_review'} if actor['role'] == 'rtl_audit' else {'rtl_remote', 'rtl_revise_architecture'})
    checks = isolation.get('checks'); isolated = isinstance(checks, list) and bool(checks) and not isolation.get('blocked')
    isolated = isolated and all(isinstance(c, dict) and isinstance(c.get('active_tools'), list)
                                and all(isinstance(name, str) for name in c['active_tools'])
                                and set(c['active_tools']) == expected for c in checks)
    session = {'run_id': out.name, 'session_id': actor['session_id'], 'role': actor['role'], 'scope': actor['scope'],
               'completed': result.get('completed') is True and type(result.get('exit_code')) is int and result['exit_code'] == 0 and result.get('stop_reason') == 'stop',
               'tool_isolation_ok': result.get('tool_isolation_ok') is True and isolated,
               'events_sha256': _sha(content['events.jsonl'])}
    session['handed_off'] = False
    handoff = _path(out, 'control/sessions/' + actor['session_id'] + '.handoff.json')
    if handoff.exists():
        raw = _raw(handoff)
        sources[str(handoff.relative_to(out))] = {'sha256': _sha(raw), 'bytes': len(raw)}
    if result.get('handoff_complete') is True:
        from harness.runtime import rtl_readiness_handoff
        rtl_readiness_handoff.terminal(out, actor, result, content['events.jsonl'])
        session['handed_off'] = True
    return sources, session, content['events.jsonl']


def _receipt(out, actor, actor_path, current, closure):
    packet = _packet(out, current, closure); sources, session, events = _terminal(out, actor)
    return {'version': 1, 'run_id': out.name, 'session_id': actor['session_id'],
            'actor_sha256': _sha(_raw(actor_path)), 'current_sha256': _sha(_raw(_path(out, 'architecture-current.json'))),
            'source_pins': sources, 'packet': packet, 'witness': verify_readback(events, session, packet)}


def end_session(out, request, current, closure):
    actor, actor_path = _actor(out, request.get('session_id'))
    end_path = _path(out, 'control/sessions/' + actor['session_id'] + '.end.json')
    sources, session, _ = _terminal(out, actor)
    if end_path.exists():
        ended = _json(end_path)
        expected_end = {'version': 1, 'run_id': out.name, 'session_id': actor['session_id'],
                        'actor_sha256': _sha(_raw(actor_path)), 'source_pins': sources,
                        'completed': session['completed'], 'tool_isolation_ok': session['tool_isolation_ok'],
                        'readback_path': ended.get('readback_path'), 'readback_sha256': ended.get('readback_sha256')}
        if 'handed_off' in ended or session['handed_off']:
            expected_end['handed_off'] = session['handed_off']
        if _raw(end_path) != _encoded(expected_end):
            raise ValueError('ended session source evidence changed')
        if ended.get('readback_path') not in (None, 'control/architecture-readback/' + actor['session_id'] + '.json'):
            raise ValueError('ended session readback path changed')
        if ended.get('readback_path') and _sha(_raw(_path(out, ended['readback_path']))) != ended.get('readback_sha256'):
            raise ValueError('ended session readback receipt changed')
        return ended
    ended = {'version': 1, 'run_id': out.name, 'session_id': actor['session_id'],
             'actor_sha256': _sha(_raw(actor_path)), 'source_pins': sources,
             'completed': session['completed'], 'tool_isolation_ok': session['tool_isolation_ok'],
             'readback_path': None, 'readback_sha256': None}
    if actor['role'] == 'rtl_readiness' and _path(out, 'control/sessions/' + actor['session_id'] + '.handoff.json').exists():
        ended['handed_off'] = session['handed_off']
    if current.get('parent_session_id') == actor['session_id']:
        if current.get('parent_actor_sha256') != _sha(_raw(actor_path)):
            raise ValueError('adopting parent actor fingerprint changed')
        receipt = _receipt(out, actor, actor_path, current, closure)
        relative = 'control/architecture-readback/' + actor['session_id'] + '.json'
        ended.update(readback_path=relative, readback_sha256=_once(_path(out, relative), receipt, same=True))
    _once(end_path, ended)
    return ended


def readback_status(out, current, closure):
    packet = _packet(out, current, closure)
    actor, actor_path = _actor(out, packet['parent_session_id'])
    if current.get('parent_actor_sha256') != _sha(_raw(actor_path)):
        raise ValueError('adopting parent actor fingerprint changed')
    if actor.get('role') != 'implementation' or actor.get('scope') != 'work':
        raise ValueError('architecture readback actor is not its implementation parent')
    end_path = _path(out, 'control/sessions/' + actor['session_id'] + '.end.json')
    if not end_path.exists():
        return {'required': True, 'complete': False, 'status': 'pending_parent_session_end', 'session_ended': False, 'parent_session_id': actor['session_id'], 'baseline_sha256': packet['baseline_sha256'], 'receipt_path': None, 'receipt_sha256': None}
    ended = _json(end_path); relative = 'control/architecture-readback/' + actor['session_id'] + '.json'
    path = _path(out, relative)
    if ended.get('readback_path') != relative or ended.get('readback_sha256') != _sha(_raw(path)):
        raise ValueError('architecture readback receipt missing or changed')
    receipt = _json(path); expected = _receipt(out, actor, actor_path, current, closure)
    _, terminal_session, _ = _terminal(out, actor)
    expected_end = {'version': 1, 'run_id': out.name, 'session_id': actor['session_id'],
                    'actor_sha256': expected['actor_sha256'], 'source_pins': expected['source_pins'],
                    'completed': terminal_session['completed'], 'tool_isolation_ok': terminal_session['tool_isolation_ok'],
                    'readback_path': relative, 'readback_sha256': _sha(_raw(path))}
    if (receipt != expected or ended.get('source_pins') != expected['source_pins']
            or ended.get('actor_sha256') != expected['actor_sha256'] or _raw(end_path) != _encoded(expected_end)):
        raise ValueError('architecture readback receipt or original source evidence changed')
    return {'required': True, 'complete': receipt['witness']['complete'] is True,
            'status': 'complete' if receipt['witness']['complete'] else 'incomplete',
            'session_ended': True,
            'parent_session_id': actor['session_id'], 'baseline_sha256': packet['baseline_sha256'],
            'receipt_path': relative, 'receipt_sha256': _sha(_raw(path)),
            'reasons': receipt['witness']['reasons']}


def protect_control_path(out, path, action):
    relative = str(path.relative_to(out))
    if relative.startswith(('control/sessions/', 'control/architecture-readback/')):
        raise ValueError('session/readback records are controller-owned immutable records')
    for suffix in ('.prompt.txt', '.events.jsonl', '.isolation.json', '.result.json'):
        if relative.startswith('control/') and relative.endswith(suffix):
            label = relative[len('control/'):-len(suffix)]
            if '/' in label:
                continue
            label = _label(label)
            if _path(out, 'control/sessions/' + label + '.end.json').exists():
                raise ValueError('ended session control evidence is immutable')
            if not _path(out, 'control/sessions/' + label + '.json').exists():
                continue
            if suffix == '.prompt.txt':
                raise ValueError('registered session prompt is immutable')
            if action == 'append_evidence' and suffix != '.events.jsonl':
                raise ValueError('only active session events may be appended')
            if action == 'put_evidence' and path.exists():
                raise ValueError('registered session source cannot be overwritten')
