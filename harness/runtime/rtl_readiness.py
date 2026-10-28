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
import tempfile
import shutil

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


def regional_policy(out):
    """Explicit project opt-in; budgets and engineering decisions remain separate."""
    configured = json.loads((out / 'manifest.json').read_text()).get('settings', {}).get('readiness', {})
    result = {}
    for key in ('region_roots', 'build_files'):
        values = configured.get(key, [])
        if not isinstance(values, list) or any(not isinstance(v, str) for v in values) or len(set(values)) != len(values):
            raise ValueError('invalid readiness ' + key)
        for name in values:
            path = owned(out / 'work', name)
            if str(path.relative_to(out / 'work')) != name:
                raise ValueError('readiness policy paths must be canonical')
            if key == 'region_roots':
                if Path(name).parts[0] != 'rtl':
                    raise ValueError('readiness region roots must stay under rtl')
                if path.exists() and not path.is_dir():
                    raise ValueError('readiness region root must be a directory')
            elif (Path(name).parts[0] in {'input', 'snapshot', 'tests', 'audit', 'readiness', 'output', 'control'}
                  or path.suffix.lower() in RTL_SUFFIXES or not path.is_file()):
                raise ValueError('readiness build file must be an existing project filelist outside frozen inputs/tests')
        result[key] = list(values)
    if result['build_files'] and not result['region_roots']:
        raise ValueError('readiness build files require an explicit regional opt-in')
    if (result['region_roots'] and (out / 'work/RTL_STRUCTURE.json').exists()
            and 'RTL_STRUCTURE.json' not in result['build_files']):
        raise ValueError('regional readiness with live RTL_STRUCTURE.json requires it in build_files; '
                         'the compile declaration must travel with the complete candidate')
    return result


def source_inventory(base, build_files=()):
    """Hash RTL/header inputs plus explicitly declared project build file bytes."""
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
    for name in build_files:
        path = owned(base, name)
        if not path.is_file() or path.suffix.lower() in RTL_SUFFIXES:
            raise ValueError('readiness declared build file is unavailable: ' + name)
        result[name] = dict(sha256=sha(path), size=path.stat().st_size, mode=path.stat().st_mode & 0o777)
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
    policy = regional_policy(out)
    source = source_inventory(out / 'work', policy['build_files'])
    fingerprint = digest(source)
    previous = campaigns(out)
    for value in reversed(previous):
        if fingerprint in (value['source_sha256'], value.get('adopted_source_sha256')):
            if any(value.get(key, []) != policy[key] for key in ('region_roots', 'build_files')):
                raise ValueError('readiness campaign regional policy changed')
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
    value = dict(version=1, **policy, campaign=name, work_scope=scope + '/work', audit_scope=scope + '/audit',
                 baseline_path='work', state='active', source_sha256=fingerprint, source_files=source,
                 budgets=limits, created_utc=now(), result_path=scope + '/work/result.json',
                 review_path=scope + '/audit/review.json')
    save(metadata_path(out, name), value)
    return status(out, {'campaign': name})


def check_baseline(out, value):
    if source_inventory(out / 'work', value.get('build_files', [])) != value['source_files']:
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
    known = repeat | {'--variant', '--metric', '--min-ratio', '--min-repeats', '--health', '--out'}
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
    health = path(options['--health'][0]) if '--health' in options else None
    if health is not None:
        health.relative_to(workspace)
        if not health.is_file():
            raise ValueError('finalize health report must be an existing regular workspace file')
    return workspace, delivery, health


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
    if comparison.get('health_report') != verified.get('health_report'):
        raise ValueError('freshly read comparison differs from verified health report')
    sources = {key: verified[key] for key in ('workspace_id', 'baseline_identity', 'variant_identity',
                                             'files', 'baseline_files', 'allowed_paths', 'changes')}
    for key in ('region_roots', 'build_files', 'build', 'baseline_build', 'operations'):
        if key in verified:
            sources[key] = verified[key]
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
        workspace, delivery, health = _finalize_paths(out, campaign, request, manifest, script)
        health_reference = {'path': str(health), 'sha256': sha(health)} if health is not None else None
        verified = verify_finalization(out, workspace, delivery)
        # The frozen helper validates the report's source/checker/evidence
        # bindings. The controller also binds that fresh proof to the exact
        # report selected by this native job, rather than ignoring --health.
        if verified.get('health_report') != health_reference:
            raise ValueError('verified finalization health report differs from its native command')
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


def _regional_compile_declaration(out, campaign, verified, tree):
    """Bind the project's native declaration to the complete regional build.

    This validates declaration inputs without running EDA or accepting structure
    evidence. All defines/parameters stay in the native normalized configuration
    and in the unchanged publication bytes; independent RTL audit still owns the
    complete compile contract and structural release decision.
    """
    name = 'RTL_STRUCTURE.json'
    if name not in campaign['source_files'] and not (out / 'work' / name).exists():
        return
    if name not in campaign.get('build_files', []):
        raise ValueError('regional readiness with live RTL_STRUCTURE.json requires it in build_files; '
                         'the compile declaration must travel with the complete candidate')
    path = owned(tree, name)
    entry = verified['files'].get(name, {})
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise ValueError('regional candidate is missing RTL_STRUCTURE.json compile declaration') from None
    if hashlib.sha256(raw).hexdigest() != entry.get('sha256') or len(raw) != entry.get('size'):
        raise ValueError('regional candidate RTL_STRUCTURE.json identity changed')
    declaration = json.loads(raw)
    if (not isinstance(declaration, dict) or set(declaration) != {'version', 'configurations', 'blackboxes'}
            or type(declaration.get('version')) is not int or declaration['version'] != 1
            or not isinstance(declaration['configurations'], list) or not declaration['configurations']
            or not isinstance(declaration['blackboxes'], list)):
        raise ValueError('invalid regional RTL_STRUCTURE.json compile declaration schema')
    from harness.runtime import rtl_structure, rtl_structure_gate
    tools = json.loads((out / 'manifest.json').read_text())['settings']['tools']
    identifiers, configurations = set(), set()
    matching = False
    for item in declaration['configurations']:
        if (not isinstance(item, dict) or set(item) - (rtl_structure_gate.CONFIGURATION_FIELDS | {'id'})
                or not isinstance(item.get('id'), str) or not item['id'].strip()
                or len(item['id']) > 128 or item['id'] in identifiers):
            raise ValueError('invalid or duplicate regional compile declaration configuration')
        try:
            normalized = rtl_structure.normalize_config(tree.resolve(), {
                **{key: value for key, value in item.items() if key != 'id'},
                'version': 2, 'tool_path': tools['yosys'], 'lint_tool_path': tools.get('verilator'),
                'timeout_seconds': 600, 'binding': {}})
        except (OSError, ValueError, TypeError) as error:
            raise ValueError('regional RTL_STRUCTURE.json compile configuration is invalid: ' + str(error)) from error
        key = digest({name: normalized[name] for name in rtl_structure_gate.CONFIGURATION_FIELDS})
        if key in configurations:
            raise ValueError('duplicate semantic regional compile declaration configuration')
        identifiers.add(item['id']); configurations.add(key)
        matching = matching or all(normalized[key] == verified['build'][key]
                                   for key in ('top', 'sources', 'include_dirs'))
    if not matching:
        raise ValueError('regional RTL_STRUCTURE.json has no configuration matching the verified '
                         'candidate top, ordered sources and include_dirs')


def integration_updates(out, campaign, verified, *, check_live_baseline=True):
    """Validate an entire source/build proposal before any live implementation edit."""
    allowed = verified['allowed_paths']; changes = verified['changes']
    regional = 'operations' in verified
    if (not isinstance(allowed, list) or not isinstance(changes, list) or not changes
            or any(not isinstance(name, str) for name in allowed + changes)
            or len(set(allowed)) != len(allowed) or len(set(changes)) != len(changes)):
        raise ValueError('verified readiness change set is invalid')
    tree = Path(verified['candidate_tree'])
    tree.resolve().relative_to((out / campaign['work_scope']).resolve())
    baseline_files, files = verified['baseline_files'], verified['files']
    for name in allowed:
        baseline = campaign['source_files'].get(name)
        original = owned(out / 'work', name)
        if (not baseline or original.suffix.lower() not in RTL_SUFFIXES
                or baseline_files.get(name, {}).get('sha256') != baseline['sha256']
                or (check_live_baseline and (not original.is_file() or sha(original) != baseline['sha256']))):
            raise ValueError('allowed RTL has no matching original baseline: ' + str(name)
                             + '; probe-only evidence cannot be integrated. Compare and verify a candidate '
                             'bound to original RTL paths and source hashes, or report an honest probe-only outcome')
    operations = dict(add=[], modify=list(changes), delete=[])
    if regional:
        roots, build_files = campaign.get('region_roots', []), campaign.get('build_files', [])
        if (not roots or verified.get('region_roots') != roots or verified.get('build_files') != build_files):
            raise ValueError('regional readiness proposal differs from explicit campaign policy')
        operations = verified['operations']
        if (not isinstance(operations, dict) or set(operations) != {'add', 'modify', 'delete'}
                or any(not isinstance(v, list) or any(not isinstance(n, str) for n in v)
                       or v != sorted(set(v)) for v in operations.values())):
            raise ValueError('invalid regional file operations')
        actual = {'add': sorted(set(files) - set(baseline_files)),
                  'delete': sorted(set(baseline_files) - set(files)),
                  'modify': sorted(n for n in set(files) & set(baseline_files)
                                   if files[n] != baseline_files[n])}
        if operations != actual or changes != sorted(n for values in operations.values() for n in values):
            raise ValueError('regional file operations differ from verified manifests')
        for name in build_files:
            baseline = campaign['source_files'].get(name)
            original = owned(out / 'work', name)
            if (not baseline or baseline_files.get(name, {}).get('sha256') != baseline['sha256']
                    or (check_live_baseline and (not original.is_file() or sha(original) != baseline['sha256']))):
                raise ValueError('regional build file has no matching original baseline: ' + name)
        if any(name not in allowed + build_files for name in operations['modify']):
            raise ValueError('regional modifications exceed original RTL/build policy')
        if any(name not in allowed or Path(name).suffix.lower() not in RTL_SUFFIXES
               for name in operations['delete']):
            raise ValueError('regional deletion requires owned original RTL')
        for name in operations['add']:
            path = owned(out / 'work', name)
            if (path.suffix.lower() not in RTL_SUFFIXES
                    or not any(Path(name).is_relative_to(Path(root)) for root in roots)
                    or (check_live_baseline and path.exists())):
                raise ValueError('regional addition exceeds authorized RTL roots: ' + name)
        for key in ('build', 'baseline_build'):
            build = verified.get(key)
            if (not isinstance(build, dict) or set(build) != {'top', 'sources', 'include_dirs'}
                    or not isinstance(build['top'], str) or not build['top']
                    or any(not isinstance(build[k], list) or any(not isinstance(n, str) for n in build[k])
                           for k in ('sources', 'include_dirs'))):
                raise ValueError('regional proposal lacks verified ' + key)
        if verified['build']['top'] != verified['baseline_build']['top']:
            raise ValueError('regional proposal changed frozen top')
        if verified['build']['include_dirs'] != verified['baseline_build']['include_dirs']:
            raise ValueError('changed regional include_dirs requires an actual project build configuration adapter for adoption')
        if verified['build'] != verified['baseline_build'] and not build_files:
            raise ValueError('changed regional build requires a declared project filelist for adoption')
        _regional_compile_declaration(out, campaign, verified, tree)
    elif not set(changes).issubset(allowed):
        raise ValueError('verified readiness change set is invalid')
    updates = {name: None for name in operations['delete']}
    for name in operations['add'] + operations['modify']:
        variant = owned(tree, name)
        raw = variant.read_bytes()
        entry = files.get(name, {})
        if hashlib.sha256(raw).hexdigest() != entry.get('sha256') or len(raw) != entry.get('size'):
            raise ValueError('verified candidate RTL changed: ' + name)
        updates[name] = raw
    return updates


def _publish_updates(out, campaign, updates):
    """Stage all bytes, journal intent, then publish or roll back under controller idle ownership.

    This is a multi-file transaction, not a filesystem-wide atomic replacement.
    A process crash leaves its durable journal for explicit recovery; it never
    triggers an automatic model retry or synthesis launch.
    """
    base = out / 'work'
    journal = metadata_path(out, campaign['campaign']).with_suffix('.publication.json')
    if journal.exists():
        raise ValueError('readiness publication journal already exists; inspect recovery state')
    with journal.open('x') as claim:
        json.dump({'state': 'staging', 'campaign': campaign['campaign']}, claim)
    transaction = Path(tempfile.mkdtemp(prefix='.publication-', dir=out / campaign['work_scope']))
    originals = {}
    created_dirs = []
    try:
        for index, (name, raw) in enumerate(updates.items()):
            target = owned(base, name)
            originals[name] = {'exists': target.is_file(), 'mode': target.stat().st_mode & 0o777 if target.exists() else None}
            if target.exists():
                shutil.copyfile(target, transaction / ('original-' + str(index)))
            if raw is not None:
                (transaction / ('update-' + str(index))).write_bytes(raw)
        save(journal, {'state': 'prepared', 'transaction': str(transaction.relative_to(out)),
                       'files': originals, 'operations': {n: 'delete' if v is None else 'write' for n, v in updates.items()}})
        try:
            for index, (name, raw) in enumerate(updates.items()):
                target = owned(base, name)
                if raw is None:
                    target.unlink()
                else:
                    missing = []
                    parent = target.parent
                    while not parent.exists():
                        missing.append(parent); parent = parent.parent
                    for parent in reversed(missing):
                        parent.mkdir(); created_dirs.append(parent)
                    staged = transaction / ('update-' + str(index))
                    staged.chmod(originals[name]['mode'] if originals[name]['exists'] else 0o644)
                    staged.replace(target)
            save(journal, {'state': 'committed', 'transaction': str(transaction.relative_to(out)), 'files': originals})
        except BaseException:
            errors = []
            for index, (name, original) in reversed(list(enumerate(originals.items()))):
                try:
                    target = owned(base, name)
                    if original['exists']:
                        backup = transaction / ('original-' + str(index))
                        backup.chmod(original['mode']); backup.replace(target)
                    elif target.exists():
                        target.unlink()
                except OSError as error:
                    errors.append(str(error))
            for path in reversed(created_dirs):
                try:
                    path.rmdir()
                except OSError:
                    pass
            save(journal, {'state': 'rollback_failed' if errors else 'rolled_back',
                           'transaction': str(transaction.relative_to(out)), 'files': originals,
                           'rollback_errors': errors})
            raise
    finally:
        # Keep the small journal and backups for explicit recovery/provenance.
        # Never delete backups after an interrupted or unsuccessful publication.
        pass


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
            _publish_updates(out, value, updates)
            receipt.update(adopted=True, changed_files={name: sha(out / 'work' / name)
                                                      for name, raw in updates.items() if raw is not None},
                           deleted_files=sorted(name for name, raw in updates.items() if raw is None),
                           scope='provisional implementation RTL/build; independent downstream RTL audit required')
            if 'operations' in verified:
                receipt.update(operations=verified['operations'], build=verified['build'], baseline_build=verified['baseline_build'],
                               region_roots=verified['region_roots'], build_files=verified['build_files'])
                value['adopted_build_sha256'] = digest(verified['build'])
            value['adopted_source_sha256'] = digest(source_inventory(out / 'work', value.get('build_files', [])))
            if 'operations' in verified:
                value['adopted_source_build_sha256'] = digest({'files': source_inventory(out / 'work', value.get('build_files', [])),
                                                               'build': verified['build']})
                receipt.update(adopted_build_sha256=value['adopted_build_sha256'],
                               adopted_source_build_sha256=value['adopted_source_build_sha256'])
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
