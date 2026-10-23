"""Native readiness campaigns: small source pins, persistent budgets and guarded adoption.

This controller module is deliberately outside the model tool action surface.
The standalone skill owns engineering evidence; this adapter owns paths and provenance.
"""
from contextlib import contextmanager
import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys

SCOPE = re.compile(r'^readiness/(campaign-[1-9][0-9]*)/(work|audit)$')
RTL_SUFFIXES = {'.v', '.sv', '.vh', '.svh'}
DEFAULTS = dict(max_campaigns=1, max_jobs=20, max_job_seconds=60, tool_seconds=240,
                session_seconds=900, assistant_turns=80, review_seconds=600,
                review_turns=50, max_variants=2)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + '\n')
    temporary.replace(path)


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def owned(base, name):
    if not isinstance(name, str) or not name or '\0' in name or Path(name).is_absolute() or '..' in Path(name).parts:
        raise ValueError('readiness path must stay within its exact scope')
    path = base / name
    resolved = path.resolve()
    resolved.relative_to(base.resolve())
    for part in [path] + list(path.parents):
        if part == base.parent:
            break
        if part.is_symlink():
            raise ValueError('readiness paths cannot be aliased')
    return path


def settings(out):
    value = json.loads((out / 'manifest.json').read_text()).get('settings', {}).get('readiness', {})
    if value.get('enabled') is not True:
        raise ValueError('RTL readiness is disabled')
    result = dict(DEFAULTS)
    result.update({key: value[key] for key in DEFAULTS if key in value})
    for key, limit in result.items():
        if type(limit) is not int or limit <= 0:
            raise ValueError('invalid readiness budget: ' + key)
    return result


def source_inventory(base):
    """Hash only existing RTL/header inputs, never historical build/results trees."""
    rtl = base / 'rtl'
    if not rtl.is_dir() or rtl.is_symlink():
        raise ValueError('readiness requires an existing plain work/rtl source closure')
    result = {}
    for path in sorted(rtl.rglob('*')):
        if path.is_symlink():
            raise ValueError('readiness RTL source links are unavailable')
        if path.is_file() and path.suffix.lower() in RTL_SUFFIXES:
            name = str(path.relative_to(base))
            result[name] = dict(sha256=sha(path), size=path.stat().st_size, mode=path.stat().st_mode & 0o777)
    if not result:
        raise ValueError('readiness RTL source closure is empty')
    return result


def metadata_path(out, campaign):
    if not isinstance(campaign, str) or not re.fullmatch(r'campaign-[1-9][0-9]*', campaign):
        raise ValueError('invalid readiness campaign')
    return owned(out, 'readiness/' + campaign + '.json')


def campaigns(out):
    paths = [path for path in (out / 'readiness').glob('campaign-*.json')
             if re.fullmatch(r'campaign-[1-9][0-9]*', path.stem)]
    return [json.loads(path.read_text()) for path in sorted(paths, key=lambda p: int(p.stem.split('-')[-1]))]


def load(out, campaign=None):
    if campaign is None:
        values = campaigns(out)
        if not values:
            raise ValueError('no readiness campaign exists')
        return values[-1]
    return json.loads(metadata_path(out, campaign).read_text())


def from_scope(out, scope, writable=False):
    match = SCOPE.fullmatch(scope or '')
    if not match:
        raise ValueError('invalid readiness scope')
    value = load(out, match.group(1))
    if value[match.group(2) + '_scope'] != scope:
        raise ValueError('readiness scope identity changed')
    if writable and value['state'] != 'active':
        raise ValueError('terminal readiness campaign is read-only')
    return value


def budget_usage(out, value):
    jobs = []
    used = reserved = 0.0
    for path in (out / 'jobs').glob('*/request.json'):
        request = json.loads(path.read_text())
        if request.get('scope') not in (value['work_scope'], value['audit_scope']):
            continue
        jobs.append(path.parent.name)
        result_path = path.with_name('result.json')
        result = json.loads(result_path.read_text()) if result_path.is_file() else None
        pending = result is None or result.get('isolation', {}).get('cleanup_complete') is False
        elapsed = result.get('elapsed_seconds') if result else None
        if pending or not isinstance(elapsed, (int, float)) or isinstance(elapsed, bool) or not math.isfinite(elapsed) or elapsed < 0:
            reserved += request['timeout_seconds']
        else:
            used += elapsed
    limits = value['budgets']
    return {'jobs_submitted': len(jobs), 'elapsed_seconds': used, 'reserved_seconds': reserved,
            'max_jobs': max(0, limits['max_jobs'] - len(jobs)),
            'tool_seconds': max(0.0, limits['tool_seconds'] - used - reserved),
            'max_job_seconds': limits['max_job_seconds']}


def status(out, request, reused=False):
    value = load(out, request.get('campaign'))
    result = {key: item for key, item in value.items() if key != 'source_files'}
    result.update(reused=reused, source_fingerprint=value['source_sha256'], remaining_budgets=budget_usage(out, value))
    return result


def begin(out, request, require_idle):
    require_idle(out)
    limits = settings(out)
    if not (out / 'snapshot/harness/skills/rtl-implementation-readiness/scripts/readiness.py').is_file():
        return {'state': 'skill_unavailable', 'reused': True, 'campaign': None}
    source = source_inventory(out / 'work')
    fingerprint = digest(source)
    previous = campaigns(out)
    for value in reversed(previous):
        if fingerprint in (value['source_sha256'], value.get('adopted_source_sha256')):
            return status(out, {'campaign': value['campaign']}, reused=True)
        if value['state'] == 'active':
            raise ValueError('readiness baseline changed during active campaign')
    if len(previous) >= limits['max_campaigns']:
        return {'state': 'campaign_budget_exhausted', 'reused': True, 'campaign': None,
                'source_sha256': fingerprint, 'remaining_budgets': {'max_campaigns': 0}}
    number = max([int(v['campaign'].split('-')[-1]) for v in previous], default=0) + 1
    name = 'campaign-' + str(number)
    scope = 'readiness/' + name
    (out / scope / 'work').mkdir(parents=True, exist_ok=False)
    (out / scope / 'audit').mkdir()
    value = dict(version=1, campaign=name, work_scope=scope + '/work', audit_scope=scope + '/audit',
                 baseline_path='work', state='active', source_sha256=fingerprint, source_files=source,
                 budgets=limits, created_utc=now(), result_path=scope + '/work/result.json',
                 review_path=scope + '/audit/review.json')
    save(metadata_path(out, name), value)
    return status(out, {'campaign': name})


def check_baseline(out, value):
    if source_inventory(out / 'work') != value['source_files']:
        raise ValueError('readiness original RTL baseline changed')


@contextmanager
def submit_reservation(out, scope, timeout, argv=None, cwd=None):
    value = from_scope(out, scope, writable=True)
    lock = metadata_path(out, value['campaign']).with_suffix('.lock')
    with lock.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        value = from_scope(out, scope, writable=True)
        remaining = budget_usage(out, value)
        if type(timeout) is not int or timeout <= 0 or timeout > value['budgets']['max_job_seconds']:
            raise ValueError('readiness per-job timeout budget exceeded')
        if remaining['max_jobs'] < 1 or remaining['tool_seconds'] < timeout:
            reason = 'job_count' if remaining['max_jobs'] < 1 else 'tool_seconds'
            raise ValueError('readiness native submit budget exhausted (' + reason + '): '
                             'requested_timeout_seconds=' + str(timeout)
                             + '; remaining_budgets=' + json.dumps(remaining, sort_keys=True))
        # A direct standalone variant command can be limited before submission.
        # Arbitrary helper programs are checked against the same limit at adoption.
        script = str(out / 'snapshot/harness/skills/rtl-implementation-readiness/scripts/readiness.py')
        if argv and script in argv:
            index = argv.index(script)
            if len(argv) > index + 1 and argv[index + 1] == 'batch':
                manifest, helper = _frozen_helper(out)
                _batch_paths(out, value, {'argv': argv, 'cwd': str(cwd or out / scope),
                    'run_dir': str(out), 'source_dir': str(out / scope), 'timeout_seconds': timeout},
                    manifest, helper)
            if len(argv) > index + 3 and argv[index + 1] == 'variant':
                workspace = Path(argv[index + 2])
                workspace = (workspace if workspace.is_absolute() else (Path(cwd) if cwd else out / scope) / workspace).resolve()
                workspace.relative_to((out / scope).resolve())
                variants = workspace / 'variants'
                count = sum(p.is_dir() and p.name != 'baseline' for p in variants.iterdir()) if variants.is_dir() else 0
                if count >= value['budgets']['max_variants']:
                    raise ValueError('readiness variant budget exhausted')
        check_baseline(out, value)
        yield {'campaign': value['campaign'], 'source_sha256': value['source_sha256'],
               'reserved_seconds': timeout, 'budgets': value['budgets']}


def _scope_file(out, scope, name):
    if name.startswith(scope + '/'):
        name = name[len(scope) + 1:]
    return owned(out / scope, name)


def verify_core(out, workspace, comparison, delivery):
    manifest = json.loads((out / 'manifest.json').read_text())
    python = manifest['settings'].get('tools', {}).get('python', sys.executable)
    script = out / 'snapshot/harness/skills/rtl-implementation-readiness/scripts/readiness.py'
    result = subprocess.run([python, '-B', str(script), 'verify', str(workspace), '--comparison', str(comparison), '--delivery', str(delivery)],
                            cwd=str(workspace), env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'),
                            capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise ValueError('standalone readiness verify failed: ' + result.stderr[-1500:])
    value = json.loads(result.stdout)
    if not isinstance(value, dict) or value.get('error'):
        raise ValueError('standalone readiness verify returned no receipt')
    return value


def _frozen_helper(out):
    manifest = json.loads((out / 'manifest.json').read_text())
    relative = 'skills/rtl-implementation-readiness/scripts/readiness.py'
    script = owned(out, 'snapshot/harness/' + relative)
    if sha(script) != manifest.get('harness_sha256', {}).get(relative):
        raise ValueError('frozen readiness helper identity changed')
    return manifest, script


def verify_finalization(out, workspace, delivery):
    manifest, script = _frozen_helper(out)
    result = subprocess.run([manifest['settings']['tools']['python'], '-B', str(script),
                             'verify-finalization', str(workspace), '--delivery', str(delivery)],
                            cwd=str(workspace), env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'),
                            capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise ValueError('standalone finalization verify failed: ' + result.stderr[-1500:])
    value = json.loads(result.stdout)
    if not isinstance(value, dict) or value.get('error'):
        raise ValueError('standalone finalization verifier returned no receipt')
    return value


def verify_batch(out, workspace, plan, batch_id, tool_seconds):
    manifest, script = _frozen_helper(out)
    result = subprocess.run([manifest['settings']['tools']['python'], '-B', str(script),
                             'verify-batch', str(workspace), '--plan', str(plan),
                             '--batch-id', batch_id, '--tool-seconds', str(tool_seconds)],
                            cwd=str(workspace), env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'),
                            capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise ValueError('standalone batch verify failed: ' + result.stderr[-1500:])
    value = json.loads(result.stdout)
    if not isinstance(value, dict) or value.get('error'):
        raise ValueError('standalone batch verifier returned no receipt')
    return value


def _batch_paths(out, campaign, request, manifest, script):
    """Recognize a finite batch invocation, including its native cleanup margin."""
    argv = request.get('argv')
    if not isinstance(argv, list) or not argv or any(not isinstance(v, str) or '\0' in v for v in argv):
        raise ValueError('invalid batch argv')
    prefix = [manifest['settings']['tools']['python']]
    index = 2 if argv[:2] == prefix + ['-B'] else 1
    if argv[:1] != prefix or argv[index:index + 2] != [str(script), 'batch']:
        raise ValueError('not an exact frozen readiness batch command')
    arguments = argv[index + 2:]
    if not arguments or arguments[0].startswith('-'):
        raise ValueError('batch requires an explicit workspace')
    options = {}
    position = 1
    while position < len(arguments):
        key, separator, value = arguments[position].partition('=')
        if key not in {'--plan', '--batch-id', '--tool-seconds'} or key in options:
            raise ValueError('unsupported or duplicate batch option')
        if not separator:
            position += 1
            if position == len(arguments):
                raise ValueError('missing batch option value')
            value = arguments[position]
        if not value or value.startswith('--'):
            raise ValueError('missing batch option value')
        options[key] = value
        position += 1
    if set(options) != {'--plan', '--batch-id', '--tool-seconds'}:
        raise ValueError('incomplete batch command')
    batch_id = options['--batch-id']
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', batch_id):
        raise ValueError('invalid readiness batch ID')
    try:
        seconds = int(options['--tool-seconds'])
    except ValueError as error:
        raise ValueError('invalid batch tool seconds') from error
    timeout = request.get('timeout_seconds')
    if seconds <= 0 or type(timeout) is not int or seconds > timeout - 5:
        raise ValueError('batch requires at least five seconds native cleanup/finalize margin')
    base = out / campaign['work_scope']
    if request.get('run_dir') != str(out) or request.get('source_dir') != str(base):
        raise ValueError('batch native job has different scope paths')
    cwd = Path(request.get('cwd', ''))
    if not cwd.is_absolute():
        raise ValueError('batch native cwd must be absolute')
    cwd = owned(base, str(cwd.relative_to(base)))

    def path(raw):
        item = Path(raw)
        if '..' in item.parts:
            raise ValueError('batch path traversal is unavailable')
        item = item if item.is_absolute() else cwd / item
        return owned(base, str(item.relative_to(base)))
    workspace, plan = path(arguments[0]), path(options['--plan'])
    plan.relative_to(workspace)
    if not plan.is_file():
        raise ValueError('batch plan must be an existing regular workspace file')
    return workspace, plan, batch_id, seconds


def _finalize_paths(out, campaign, request, manifest, script):
    """Recognize the exact frozen CLI, not a caller's claimed output JSON."""
    argv = request.get('argv')
    if not isinstance(argv, list) or not argv or any(not isinstance(v, str) or '\0' in v for v in argv):
        raise ValueError('invalid finalize argv')
    prefix = [manifest['settings']['tools']['python']]
    index = 2 if argv[:2] == prefix + ['-B'] else 1
    if argv[:1] != prefix or argv[index:index + 2] != [str(script), 'finalize']:
        raise ValueError('not an exact frozen readiness finalize command')
    arguments = argv[index + 2:]
    if not arguments or arguments[0].startswith('-'):
        raise ValueError('finalize requires an explicit workspace')
    options = {}
    repeat = {'--baseline-job', '--candidate-job', '--verification-job'}
    known = repeat | {'--variant', '--metric', '--min-ratio', '--min-repeats', '--out'}
    position = 1
    while position < len(arguments):
        key, separator, value = arguments[position].partition('=')
        if key not in known or key in options and key not in repeat:
            raise ValueError('unsupported or duplicate finalize option')
        if not separator:
            position += 1
            if position == len(arguments):
                raise ValueError('missing finalize option value')
            value = arguments[position]
        if not value or value.startswith('--'):
            raise ValueError('missing finalize option value')
        options.setdefault(key, []).append(value)
        position += 1
    if not {'--variant', '--out', '--baseline-job', '--candidate-job'}.issubset(options):
        raise ValueError('incomplete finalize command')
    base = out / campaign['work_scope']
    if request.get('run_dir') != str(out) or request.get('source_dir') != str(base):
        raise ValueError('finalize native job has different scope paths')
    cwd = Path(request.get('cwd', ''))
    if not cwd.is_absolute():
        raise ValueError('finalize native cwd must be absolute')
    cwd = owned(base, str(cwd.relative_to(base)))

    def path(raw):
        item = Path(raw)
        if '..' in item.parts:
            raise ValueError('finalize path traversal is unavailable')
        item = item if item.is_absolute() else cwd / item
        return owned(base, str(item.relative_to(base)))
    workspace, delivery = path(arguments[0]), path(options['--out'][0])
    delivery.relative_to(workspace)
    return workspace, delivery


def _evidence_identity(workspace, delivery, verified):
    """Group repeated exports of exactly the same freshly verified evidence.

    Export paths and creation timestamps are publication details. Every other
    comparison field and the verified source/change manifests remain part of
    this key; ordinary comparison/finalization identities are never rewritten.
    """
    comparison = json.loads(owned(delivery, 'comparison.json').read_text())
    if not isinstance(comparison, dict):
        raise ValueError('verified comparison must be an object')
    unsigned = {key: value for key, value in comparison.items() if key != 'identity'}
    # Standalone core.digest uses UTF-8 JSON rather than native ASCII escaping.
    identity = hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(',', ':'),
                                        ensure_ascii=False).encode()).hexdigest()
    if comparison.get('identity') != identity or verified['comparison_identity'] != identity:
        raise ValueError('freshly read comparison identity differs from the verifier')
    for key in ('workspace_id', 'baseline_identity', 'variant_identity', 'decision', 'reason'):
        if comparison.get(key) != verified[key]:
            raise ValueError('freshly read comparison differs from verified ' + key)
    sources = {key: verified[key] for key in ('workspace_id', 'baseline_identity', 'variant_identity',
                                             'files', 'baseline_files', 'allowed_paths', 'changes')}
    stable = {key: value for key, value in comparison.items() if key not in ('created_utc', 'identity')}
    return digest({'workspace': str(workspace), 'comparison': stable, 'sources': sources})


def _resolve_job(out, campaign, job_id, manifest, script):
    if not isinstance(job_id, str) or not re.fullmatch(r'[0-9]+', job_id):
        raise ValueError('invalid finalization native job ID')
    job = owned(out, 'jobs/' + job_id)
    request = json.loads(owned(job, 'request.json').read_text())
    result = json.loads(owned(job, 'result.json').read_text())
    isolation = result.get('isolation', {})
    expected = manifest.get('job_isolation')
    if (request.get('scope') != campaign['work_scope'] or not isinstance(expected, dict) or not expected
            or request.get('job_isolation') != expected or result.get('job_isolation') != expected
            or not isinstance(isolation, dict)
            or any(isolation.get(key) != value for key, value in expected.items())
            or result.get('cleanup_complete') is not True or result.get('container_running') is not False
            or isolation.get('cleanup_complete') is not True or isolation.get('container_running') is not False
            or type(result.get('exit_code')) is not int or result['exit_code'] != 0
            or any(result.get(key) for key in ('error', 'timed_out', 'cancelled', 'sandbox_setup_failed', 'integrity_error'))):
        raise ValueError('finalization job is not successful and cleaned in the campaign work scope')
    if (request.get('candidate_sha256') != campaign['source_sha256']
            or result.get('candidate_sha256') != campaign['source_sha256']
            or request.get('readiness_budget', {}).get('source_sha256') != campaign['source_sha256']):
        raise ValueError('finalization native source identity differs from the campaign')
    argv = request.get('argv', [])
    index = 2 if argv[:2] == [manifest['settings']['tools']['python'], '-B'] else 1
    if argv[index:index + 2] == [str(script), 'batch']:
        workspace, plan, batch_id, seconds = _batch_paths(out, campaign, request, manifest, script)
        batch = verify_batch(out, workspace, plan, batch_id, seconds)
        for key, expected_value in (('workspace', str(workspace)), ('plan', str(plan)),
                                    ('batch_id', batch_id), ('tool_seconds', seconds)):
            if type(batch.get(key)) is not type(expected_value) or batch[key] != expected_value:
                raise ValueError('verified batch has different ' + key)
        if not isinstance(batch.get('batch_identity'), str) or not batch['batch_identity']:
            raise ValueError('verified batch lacks its identity')
        verified = batch.get('finalization')
        if not isinstance(verified, dict):
            raise ValueError('verified batch has no finalized ordinary bundle')
        raw_delivery = Path(verified.get('delivery', ''))
        if not raw_delivery.is_absolute():
            raise ValueError('verified batch finalization delivery must be absolute')
        delivery = owned(workspace, str(raw_delivery.relative_to(workspace)))
    else:
        workspace, delivery = _finalize_paths(out, campaign, request, manifest, script)
        verified = verify_finalization(out, workspace, delivery)
    comparison, finalization = delivery / 'comparison.json', delivery / 'finalization.json'
    for key, expected_path in (('workspace', workspace), ('delivery', delivery),
                               ('comparison', comparison), ('finalization_path', finalization)):
        if verified.get(key) != str(expected_path):
            raise ValueError('verified finalization has different ' + key + ' path')
    for key in ('workspace_id', 'baseline_identity', 'variant_identity', 'comparison_identity', 'finalization_identity'):
        if not isinstance(verified.get(key), str) or not verified[key]:
            raise ValueError('verified finalization lacks ' + key)
    if (verified.get('decision') not in ('keep_baseline', 'evidence_incomplete', 'candidate_for_integration')
            or not isinstance(verified.get('reason'), str) or not verified['reason'].strip()):
        raise ValueError('verified finalization has no decision or reason')
    evidence_identity = _evidence_identity(workspace, delivery, verified)
    base = out / campaign['work_scope']
    report = {key: verified[key] for key in ('decision', 'reason')}
    report.update(workspace=str(workspace.relative_to(base)), comparison=str(comparison.relative_to(base)),
                  delivery=str(delivery.relative_to(base)))
    return {'found': True, 'report': report, 'report_path': str(finalization.relative_to(base)),
            'finalization_job_id': job_id,
            'evidence_identity': evidence_identity,
            **{key: verified[key] for key in ('workspace_id', 'baseline_identity', 'variant_identity',
                                             'comparison_identity', 'finalization_identity')},
            '_verification': verified}


def resolve(out, request, *, _include_verification=False):
    """Controller-only discovery. Never creates a report or executes experiments."""
    campaign = load(out, request.get('campaign'))
    manifest, script = _frozen_helper(out)
    selected = request.get('finalization_job_id')
    if selected is not None and (not isinstance(selected, str) or not re.fullmatch(r'[0-9]+', selected)):
        return {'found': False, 'reason': 'invalid finalization native job ID'}
    candidates = []
    for path in sorted((out / 'jobs').glob('*/request.json')):
        try:
            item = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(item, dict):
            continue
        if item.get('scope') in (campaign['work_scope'], campaign['audit_scope']):
            candidates.append(path.parent.name)
            if len(candidates) > campaign['budgets']['max_jobs']:
                return {'found': False, 'reason': 'campaign native job count exceeds its budget'}
    found, selected_result = {}, None
    selected_error = 'selected finalization job is not in this campaign'
    for job_id in candidates:
        try:
            result = _resolve_job(out, campaign, job_id, manifest, script)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            if job_id == selected:
                selected_error = str(error)
            continue
        found.setdefault(result['evidence_identity'], result)
        if job_id == selected:
            selected_result = result
    if len(found) > 1:
        return {'found': False, 'ambiguous': True, 'reason': 'multiple distinct verified finalizations'}
    if selected is not None and selected_result is None:
        return {'found': False, 'reason': selected_error}
    if not found:
        return {'found': False, 'reason': 'no trusted completed finalization job'}
    result = selected_result or next(iter(found.values()))
    if not _include_verification:
        result.pop('_verification')
    return result


def _actor_status(out, session_id, role, scope):
    """Record actual model termination separately from a verified artifact outcome."""
    from harness.runtime import rtl_readback_store as store
    try:
        actor, path = store._actor(out, session_id)
        if actor['role'] != role or actor['scope'] != scope:
            raise ValueError('readiness model session role or scope mismatch')
        sources, terminal, _ = store._terminal(out, actor)
        ended_path = path.with_name(path.stem + '.end.json')
        ended = json.loads(ended_path.read_text())
        if (ended.get('actor_sha256') != sha(path) or ended.get('source_pins') != sources
                or ended.get('completed') != terminal['completed']
                or ended.get('handed_off', False) != terminal['handed_off']
                or ended.get('tool_isolation_ok') != terminal['tool_isolation_ok']):
            raise ValueError('readiness ended actor source binding changed')
        result = json.loads((out / 'control' / (session_id + '.result.json')).read_text())
        return {'available': True, 'session_id': session_id, 'actor_sha256': sha(path),
                'end_sha256': sha(ended_path), **{key: terminal[key] for key in ('completed', 'handed_off', 'tool_isolation_ok')},
                **{key: result.get(key) for key in ('budget_exhausted', 'budget_reason', 'exit_code',
                                                  'assistant_turns', 'elapsed_seconds')}}
    except (OSError, ValueError, KeyError, TypeError) as error:
        return {'available': False, 'session_id': session_id, 'reason': str(error)}


def _require_actor(out, session_id, role, scope):
    from harness.runtime import rtl_readback_store as store
    actor, path = store._actor(out, session_id)
    if actor['role'] != role or actor['scope'] != scope:
        raise ValueError('readiness model session role or scope mismatch')
    ended_path = path.with_name(path.stem + '.end.json')
    if not ended_path.is_file():
        raise ValueError('readiness model session has not ended')
    ended = json.loads(ended_path.read_text())
    sources, session, _ = store._terminal(out, actor)
    model_result = json.loads((out / 'control' / (session_id + '.result.json')).read_text())
    naturally_completed = ended.get('completed') is True and session['completed'] is True
    handed_off = (role == 'rtl_readiness' and ended.get('handed_off') is True and session['handed_off'] is True)
    if (ended.get('actor_sha256') != sha(path) or ended.get('source_pins') != sources
            or ended.get('completed') != session['completed']
            or ended.get('handed_off', False) != session['handed_off']
            or not (naturally_completed or handed_off)
            or ended.get('tool_isolation_ok') is not True or session['tool_isolation_ok'] is not True
            or model_result.get('budget_exhausted') is True):
        raise ValueError('readiness model session is incomplete or not isolated')
    result = {'session_id': session_id, 'actor_sha256': sha(path), 'end_sha256': sha(ended_path)}
    if handed_off:
        result.update(handed_off=True, handoff_job_id=model_result['handoff_job_id'],
                      handoff_receipt_sha256=model_result['handoff_receipt_sha256'])
    return result


def integration_updates(out, campaign, verified, *, check_live_baseline=True):
    """Bind a verified positive proposal to original campaign RTL before delivery.

    Completed handoff receipts remain verifiable after adoption: their immutable
    source metadata still binds to the campaign, while live work may be the
    adopted variant. The adoption path retains its original live-byte check.
    """
    allowed = verified['allowed_paths']; changes = verified['changes']
    if (not isinstance(allowed, list) or not isinstance(changes, list) or not changes
            or any(not isinstance(name, str) for name in allowed + changes)
            or len(set(changes)) != len(changes) or not set(changes).issubset(allowed)):
        raise ValueError('verified readiness change set is invalid')
    tree = Path(verified['candidate_tree'])
    tree.resolve().relative_to((out / campaign['work_scope']).resolve())
    for name in allowed:
        baseline = campaign['source_files'].get(name)
        original = owned(out / 'work', name)
        if (not baseline or original.suffix.lower() not in RTL_SUFFIXES or not original.is_file()
                or verified['baseline_files'].get(name, {}).get('sha256') != baseline['sha256']
                or (check_live_baseline and sha(original) != baseline['sha256'])):
            raise ValueError('allowed RTL has no matching original baseline: ' + str(name)
                             + '; probe-only evidence cannot be integrated. Compare and verify a candidate '
                             'bound to original RTL paths and source hashes, or report an honest probe-only outcome')
    updates = {}
    for name in changes:
        variant = owned(tree, name)
        raw = variant.read_bytes()
        entry = verified['files'].get(name, {})
        if hashlib.sha256(raw).hexdigest() != entry.get('sha256') or len(raw) != entry.get('size'):
            raise ValueError('verified candidate RTL changed: ' + name)
        updates[name] = raw
    return updates


def complete(out, request, require_idle):
    require_idle(out)
    value = load(out, request.get('campaign'))
    if value['state'] != 'active':
        return status(out, {'campaign': value['campaign']}, reused=True)
    receipt = {'campaign': value['campaign'], 'source_sha256': value['source_sha256'],
               'recorded_utc': now(), 'adopted': False, 'changed_files': {}}
    receipt['implementation_model_status'] = _actor_status(out, request.get('implementation_session_id'),
                                                          'rtl_readiness', value['work_scope'])
    try:
        check_baseline(out, value)
        resolved = resolve(out, request, _include_verification=True)
        verified = None
        if resolved.get('found'):
            document = resolved['report']
            report = _scope_file(out, value['work_scope'], resolved['report_path'])
            verified = resolved['_verification']
            receipt.update(finalization_job_id=resolved['finalization_job_id'],
                           finalization_identity=resolved['finalization_identity'])
        else:
            if request.get('finalization_job_id') is not None or resolved.get('ambiguous'):
                raise ValueError(resolved.get('reason', 'finalization could not be resolved'))
            report = _scope_file(out, value['work_scope'], request.get('report_path', 'result.json'))
            document = json.loads(report.read_text())
        receipt.update(report_path=str(report.relative_to(out)), report_sha256=sha(report))
        decision = document.get('decision')
        if decision not in ('keep_baseline', 'evidence_incomplete', 'candidate_for_integration') or not isinstance(document.get('reason'), str) or not document['reason'].strip():
            raise ValueError('invalid readiness report decision or reason')
        receipt.update(decision=decision, reason=document['reason'])
        if decision == 'candidate_for_integration':
            receipt['implementation_session'] = _require_actor(out, request.get('implementation_session_id'), 'rtl_readiness', value['work_scope'])
            if (receipt['implementation_session'].get('handed_off')
                    and receipt['implementation_session']['handoff_job_id'] != receipt.get('finalization_job_id')):
                raise ValueError('readiness adoption finalization differs from explicit handoff job')
            receipt['review_session'] = _require_actor(out, request.get('review_session_id'), 'readiness_review', value['audit_scope'])
            workspace = owned(out / value['work_scope'], document['workspace'])
            variants = workspace / 'variants'
            if variants.is_dir() and sum(p.is_dir() and p.name != 'baseline' for p in variants.iterdir()) > value['budgets']['max_variants']:
                raise ValueError('readiness variant budget exceeded')
            comparison = owned(out / value['work_scope'], document['comparison'])
            delivery = owned(out / value['work_scope'], document['delivery'])
            verified = verified or verify_core(out, workspace, comparison, delivery)
            if verified['decision'] != 'candidate_for_integration' or Path(verified['workspace']).resolve() != workspace.resolve():
                raise ValueError('standalone verifier did not recommend this workspace')
            review_path = _scope_file(out, value['audit_scope'], request.get('review_path', 'review.json'))
            review = json.loads(review_path.read_text())
            if review.get('verdict') != 'accept' or not isinstance(review.get('reason'), str) or not review['reason'].strip():
                raise ValueError('readiness engineering review did not accept')
            for key in ('comparison_identity', 'baseline_identity', 'variant_identity'):
                if not isinstance(verified.get(key), str) or review.get(key) != verified[key]:
                    raise ValueError('readiness review has stale ' + key)
            updates = integration_updates(out, value, verified)
            check_baseline(out, value)
            receipt.update(verification=verified, review_path=str(review_path.relative_to(out)), review_sha256=sha(review_path))
            originals = {name: (out / 'work' / name).read_bytes() for name in updates}
            try:
                for name, raw in updates.items():
                    target = out / 'work' / name
                    temporary = target.with_name(target.name + '.readiness.tmp')
                    temporary.write_bytes(raw); temporary.chmod(value['source_files'][name]['mode']); temporary.replace(target)
            except BaseException:
                for name, raw in originals.items():
                    (out / 'work' / name).write_bytes(raw)
                raise
            receipt.update(adopted=True, changed_files={name: sha(out / 'work' / name) for name in updates},
                           scope='provisional implementation RTL; independent downstream RTL audit required')
            value['adopted_source_sha256'] = digest(source_inventory(out / 'work'))
            value['state'] = 'adopted'
        else:
            if verified is not None:
                receipt['verification'] = verified
            value['state'] = decision
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        receipt.update(decision='evidence_incomplete', reason=str(error))
        value['state'] = 'evidence_incomplete'
    relative = 'readiness/' + value['campaign'] + '.adoption.json'
    save(out / relative, receipt)
    value.update(completed_utc=now(), adoption_path=relative, decision=receipt['decision'], adopted=receipt['adopted'],
                 implementation_model_status=receipt['implementation_model_status'])
    save(metadata_path(out, value['campaign']), value)
    return status(out, {'campaign': value['campaign']})
