"""Controller-owned, read-only evidence packets and bounded diagnosis receipts.

Engineering conclusions remain ordinary model reports. These actions only bind
the observed bytes, invocation and dispatch eligibility; they never run EDA.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import tempfile

from harness.runtime import rtl_readiness as readiness
from harness.runtime.rtl_diagnosis_report import parse_report

EVENTS = {'rtl_review_passed', 'stage_completed', 'observation_budget', 'job_failed'}
KINDS = {'source', 'log', 'status', 'report', 'metadata'}
ADVICE = {'continue', 'small_experiment', 'more_evidence'}
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,179}')
VOLATILE = {'elapsed', 'elapsedseconds', 'elapsedtime', 'elapsedtimeseconds',
            'cpuseconds', 'cputime', 'cputimeseconds', 'cputicks', 'cpupercent',
            'rss', 'rssbytes', 'rssmib', 'rsskb', 'residentbytes', 'residentmemory',
            'updatedutc', 'updatedat', 'captureutc', 'capturedutc', 'observedutc',
            'observedat', 'captureat', 'capturedat', 'mtime', 'mtimens', 'mtimeutc',
            'mtimeseconds', 'availablememory', 'availablememorybytes', 'availablememorymib',
            'memavailablebytes', 'swapfreebytes', 'load', 'loadaverage',
            'timestamp', 'lastupdated', 'lastupdatedutc'}


@contextmanager
def _lock(out):
    base = readiness.owned(out, 'control/diagnosis')
    base.mkdir(parents=True, exist_ok=True)
    with (base / '.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield base


def _binding(out):
    """Pin the actual current RTL, plus validated imported ancestry when present."""
    policy = readiness.regional_policy(out)
    result = {'source_sha256': readiness.digest(readiness.source_inventory(out / 'work', policy['build_files']))}
    if policy['region_roots'] or policy['build_files']:
        result['regional_policy'] = policy
    path = out / 'control/readiness-seed.json'
    if path.exists():
        from harness.runtime.rtl_readiness_seed import seed
        receipt = seed(out, {'validate_only': True})
        result.update(seed_receipt_sha256=readiness.sha(path),
                      selected_closure_sha256=receipt['selected_closure_sha256'],
                      seed_request_identity=receipt['request_identity'])
    manifest = json.loads((out / 'manifest.json').read_text())
    result['input_sha256'] = manifest.get('input_sha256', {})
    result['architecture_sha256'] = manifest.get('architecture_sha256', {})
    return result


def _stat(value):
    return {'device': value.st_dev, 'inode': value.st_ino, 'bytes': value.st_size,
            'mtime_ns': value.st_mtime_ns}


def _capture(row, destination):
    if not isinstance(row, dict) or row.get('kind') not in KINDS:
        raise ValueError('diagnosis file requires an explicit supported kind')
    name, raw_path = row.get('name'), row.get('path')
    if not isinstance(name, str) or NAME.fullmatch(name) is None:
        raise ValueError('diagnosis evidence name must be a safe single component')
    if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
        raise ValueError('diagnosis evidence requires an absolute existing file')
    path = Path(raw_path)
    if path.is_symlink() or not path.is_file() or path.resolve() != path:
        raise ValueError('diagnosis evidence must be a plain regular file without aliases')
    before = path.stat()
    digest, count, lines, last = hashlib.sha256(), 0, 0, b''
    target = destination / name
    # Read the initial-size prefix, so an append-only running log is a precise
    # finite observation rather than a stream that never reaches EOF.
    with path.open('rb') as source, target.open('xb') as dest:
        opened = os.fstat(source.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError('diagnosis evidence changed before capture')
        while count < before.st_size:
            chunk = source.read(min(1024 * 1024, before.st_size - count))
            if not chunk:
                raise ValueError('diagnosis evidence truncated during capture')
            dest.write(chunk); digest.update(chunk)
            count += len(chunk); lines += chunk.count(b'\n'); last = chunk[-1:]
    after = path.stat()
    if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
        raise ValueError('diagnosis evidence replaced during capture')
    captured_sha = digest.hexdigest()
    if row['kind'] == 'log':
        if after.st_size < before.st_size:
            raise ValueError('diagnosis log truncated during capture')
        # Recheck the prefix: appends are allowed, edits to captured bytes are not.
        current = hashlib.sha256()
        with path.open('rb') as stream:
            remaining = count
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError('diagnosis log prefix changed during capture')
                current.update(chunk); remaining -= len(chunk)
        if current.hexdigest() != captured_sha:
            raise ValueError('diagnosis log prefix changed during capture')
    elif _stat(before) != _stat(after) or readiness.sha(path) != captured_sha:
        raise ValueError('diagnosis evidence changed during capture')
    if row.get('sha256') is not None and row['sha256'] != captured_sha:
        raise ValueError('diagnosis declared evidence SHA-256 mismatch: ' + name)
    return {'name': name, 'kind': row['kind'], 'original_path': str(path),
            'sha256': captured_sha, 'observed_bytes': count,
            'lines': lines + int(bool(last) and last != b'\n'),
            'mime': mimetypes.guess_type(name)[0] or 'application/octet-stream',
            'source_identity_before': _stat(before), 'source_identity_after': _stat(after),
            'captured_utc': readiness.now()}


def _semantic(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = re.sub('[^a-z0-9]', '', key.lower())
            if normalized in VOLATILE:
                continue
            if normalized == 'state' and isinstance(item, str):
                if item in ('R', 'S', 'D', 'I', 'T', 't', 'W', 'P'):
                    item = 'active'
                elif item in ('Z', 'X', 'x'):
                    item = 'ended'
            result[key] = _semantic(item)
        return result
    if isinstance(value, list):
        return [_semantic(item) for item in value]
    return value


def _process(declared):
    if not isinstance(declared, dict) or type(declared.get('pid')) is not int or declared['pid'] <= 0:
        raise ValueError('diagnosis process requires a positive integer PID')
    if not str(declared.get('start_ticks', '')).isdigit() or not isinstance(declared.get('boot_id'), str) or not declared['boot_id']:
        raise ValueError('diagnosis process requires start_ticks and boot_id')
    result = {'declared': declared, 'available': False, 'identity_match': False}
    try:
        proc = Path('/proc') / str(declared['pid'])
        raw = (proc / 'stat').read_text()
        fields = raw[raw.rfind(')') + 2:].split()
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        result.update(available=True, pid=declared['pid'], start_ticks=int(fields[19]),
                      boot_id=boot, state=fields[0],
                      cpu_seconds=(int(fields[11]) + int(fields[12])) / os.sysconf('SC_CLK_TCK'),
                      rss_bytes=int(fields[21]) * os.sysconf('SC_PAGE_SIZE'),
                      command=(proc / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace').strip())
        result['identity_match'] = boot == declared['boot_id'] and str(result['start_ticks']) == str(declared['start_ticks'])
    except (OSError, ValueError, IndexError) as error:
        result['error'] = str(error)
    return result


def resources():
    value = {'cpu_count': os.cpu_count(), 'available_memory_bytes': None}
    try:
        value['load_average'] = list(os.getloadavg())
    except OSError:
        value['load_average'] = None
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'):
                value['available_memory_bytes'] = int(line.split()[1]) * 1024
                break
    except (OSError, ValueError, IndexError):
        pass
    return value


def _public(value):
    result = {key: value[key] for key in ('observation_id', 'fingerprint', 'index_path',
              'state', 'source_fingerprint', 'report_path', 'report') if key in value}
    if 'error' in value:
        result['diagnosis_error'] = value['error']
    return result


def prepare(out, request):
    event, inputs = request.get('event'), request.get('inputs')
    if event not in EVENTS:
        raise ValueError('unsupported diagnosis observation event')
    if not isinstance(inputs, dict) or inputs.get('version') != 1:
        raise ValueError('diagnosis inputs version must be 1')
    files = inputs.get('files')
    if not isinstance(files, list) or not files:
        raise ValueError('diagnosis requires an explicit evidence file list')
    if any(not isinstance(row, dict) or not isinstance(row.get('name'), str) for row in files):
        raise ValueError('diagnosis evidence names must be strings')
    if len({row.get('name') for row in files}) != len(files):
        raise ValueError('diagnosis evidence names must be unique')
    for key in ('source', 'metadata', 'observation'):
        if not isinstance(inputs.get(key, {}), dict):
            raise ValueError('diagnosis ' + key + ' must be an object')
    process_rows = inputs.get('processes', [])
    if not isinstance(process_rows, list):
        raise ValueError('diagnosis processes must be a list')
    with _lock(out) as base:
        before = _binding(out)
        staging = Path(tempfile.mkdtemp(prefix='.capture-', dir=base))
        try:
            evidence = staging / 'evidence'; evidence.mkdir()
            rows = [_capture(row, evidence) for row in files]
            processes = [_process(row) for row in process_rows]
            if _binding(out) != before:
                raise ValueError('diagnosis current source binding changed during preparation')
            keys = []
            for row in sorted(rows, key=lambda item: item['name']):
                item = {key: row[key] for key in ('name', 'kind', 'original_path')}
                if row['kind'] == 'status':
                    try:
                        item['semantic'] = _semantic(json.loads((evidence / row['name']).read_text()))
                    except (UnicodeError, ValueError):
                        item['sha256'] = row['sha256']
                else:
                    item['sha256'] = row['sha256']
                keys.append(item)
            meaningful = {'event': event, 'current_run_binding': before,
                          'source': inputs.get('source', {}), 'files': keys,
                          'observation': _semantic(inputs.get('observation', {})),
                          'metadata': _semantic(inputs.get('metadata', {})),
                          'processes': [{'declared': {key: p['declared'].get(key) for key in ('pid', 'start_ticks', 'boot_id', 'kind')},
                                         'lifecycle': ('ended' if p.get('state') in ('Z', 'X') else 'active') if p['available'] else 'unknown',
                                         **{key: p.get(key) for key in ('available', 'identity_match',
                                         'pid', 'start_ticks', 'boot_id')}} for p in processes]}
            fingerprint = readiness.digest(meaningful)
            observation_id = 'observation-' + fingerprint[:24]
            destination = base / observation_id
            if destination.exists():
                _, existing, _ = _load(out, observation_id)
                if existing['fingerprint'] != fingerprint:
                    raise ValueError('diagnosis observation ID collision')
                return dict(_public(existing), reused=True)
            relative = 'control/diagnosis/' + observation_id
            for row in rows:
                row['path'] = relative + '/evidence/' + row['name']
            index = {'version': 1, 'observation_id': observation_id, 'fingerprint': fingerprint,
                     'event': event, 'source': inputs.get('source', {}),
                     'current_run_binding': before, 'files': rows,
                     'processes': processes, 'resources': resources(),
                     'metadata': inputs.get('metadata', {}), 'observation': inputs.get('observation', {}),
                     'captured_utc': readiness.now(),
                     'note': 'Observed evidence and runtime facts; no RTL or physical acceptance is implied.'}
            readiness.save(staging / 'index.json', index)
            state = {'version': 1, 'observation_id': observation_id, 'fingerprint': fingerprint,
                     'state': 'active', 'index_path': relative + '/index.json',
                     'source_fingerprint': before['source_sha256'], 'current_run_binding': before,
                     'index_sha256': readiness.sha(staging / 'index.json'), 'created_utc': readiness.now()}
            readiness.save(staging / 'state.json', state)
            staging.replace(destination)
            return dict(_public(state), reused=False)
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def _load(out, observation_id):
    if not isinstance(observation_id, str) or not re.fullmatch(r'observation-[a-f0-9]{24}', observation_id):
        raise ValueError('invalid diagnosis observation ID')
    base = readiness.owned(out, 'control/diagnosis/' + observation_id)
    state = json.loads((base / 'state.json').read_text())
    if state.get('observation_id') != observation_id or readiness.sha(base / 'index.json') != state.get('index_sha256'):
        raise ValueError('diagnosis observation identity changed')
    index = json.loads((base / 'index.json').read_text())
    if index.get('fingerprint') != state.get('fingerprint'):
        raise ValueError('diagnosis observation fingerprint changed')
    for row in index['files']:
        path = readiness.owned(out, row['path'])
        if readiness.sha(path) != row['sha256']:
            raise ValueError('diagnosis captured evidence changed')
    if state.get('state') == 'completed':
        report_path = readiness.owned(out, state['report_path'])
        if readiness.sha(report_path) != state.get('report_sha256') or json.loads(report_path.read_text()) != state.get('report'):
            raise ValueError('diagnosis completed report changed')
    return base, state, index


def _actor_report(out, session_id):
    result = json.loads((out / 'control' / (session_id + '.result.json')).read_text())
    return parse_report(result.get('final_text'))


def complete(out, request):
    from harness.runtime import rtl_readback_store as store
    with _lock(out):
        base, state, _ = _load(out, request.get('observation_id'))
        if state['state'] != 'active':
            return dict(_public(state), reused=True)
        report = request.get('report')
        state['session_id'] = request.get('session_id')
        try:
            if _binding(out) != state['current_run_binding']:
                raise ValueError('diagnosis source binding changed before completion')
            actor = readiness._require_actor(out, request.get('session_id'), 'readiness_diagnosis', 'work')
            registered, _ = store._actor(out, request.get('session_id'))
            if (registered.get('diagnosis_observation_id') != state['observation_id']
                    or registered.get('diagnosis_fingerprint') != state['fingerprint']):
                raise ValueError('diagnosis actor invocation belongs to a different observation')
            if request.get('error'):
                raise ValueError(str(request['error']))
            if not isinstance(report, dict) or report.get('advice') not in ADVICE or not isinstance(report.get('summary'), str) or not report['summary'].strip():
                raise ValueError('diagnosis report requires advice and a nonempty summary')
            if not isinstance(report.get('findings'), list) or ('experiments' in report and not isinstance(report['experiments'], list)):
                raise ValueError('diagnosis findings and optional experiments must be lists')
            # Strict JSON means NaN/Infinity cannot enter a purported ordinary report.
            json.dumps(report, allow_nan=False)
            if _actor_report(out, request['session_id']) != report:
                raise ValueError('diagnosis report differs from its registered terminal actor output')
            readiness.save(base / 'report.json', report)
            state.update(state='completed', actor=actor, report=report,
                         report_path=str((base / 'report.json').relative_to(out)),
                         report_sha256=readiness.sha(base / 'report.json'))
        except (OSError, ValueError, KeyError, TypeError) as error:
            state.update(state='incomplete', error=str(error),
                         model_status=readiness._actor_status(out, request.get('session_id'), 'readiness_diagnosis', 'work'))
        state['ended_utc'] = readiness.now()
        readiness.save(base / 'state.json', state)
        return dict(_public(state), reused=False)


def admission(out, request):
    """Reserve one explicitly requested experiment dispatch; never launch it."""
    from harness.runtime.rtl_worker import active_jobs
    with _lock(out):
        base, state, index = _load(out, request.get('observation_id'))
        reasons = []
        current_resources = resources()
        budgets = {}
        if state['state'] != 'completed' or state.get('report', {}).get('advice') != 'small_experiment':
            reasons.append('completed_diagnosis_does_not_recommend_small_experiment')
        if (base / 'experiment-admission.json').exists():
            reasons.append('experiment_dispatch_already_reserved')
        if _binding(out) != state['current_run_binding']:
            reasons.append('diagnosed_source_binding_changed')
        try:
            budgets = readiness.settings(out)
            campaigns = readiness.campaigns(out)
            budgets['remaining_campaigns'] = max(0, budgets['max_campaigns'] - len(campaigns))
            if budgets['remaining_campaigns'] == 0:
                reasons.append('experiment_campaign_budget_exhausted')
            if any(c['state'] == 'active' or state['source_fingerprint'] in (c['source_sha256'], c.get('adopted_source_sha256')) for c in campaigns):
                reasons.append('source_has_existing_readiness_campaign')
        except (OSError, ValueError, KeyError, TypeError) as error:
            reasons.append('experiment_budgets_unavailable: ' + str(error))
        if active_jobs(out):
            reasons.append('current_run_has_pending_jobs')
        settings = json.loads((out / 'manifest.json').read_text()).get('settings', {}).get('readiness', {})
        minimum = settings.get('experiment_min_available_memory_mib')
        available = current_resources.get('available_memory_bytes')
        if type(minimum) is not int or minimum <= 0:
            reasons.append('experiment_memory_admission_threshold_unconfigured')
        elif not isinstance(available, int) or available < minimum * 1024 * 1024:
            reasons.append('experiment_available_memory_below_threshold_or_unknown')
        for row in index['processes']:
            current = _process(row['declared'])
            if not current['available'] or not current['identity_match']:
                reasons.append('external_process_identity_unknown_or_changed')
            elif row['declared'].get('kind') == 'experiment' and current['state'] not in ('Z', 'X'):
                reasons.append('external_experiment_active')
        metadata = index.get('metadata', {})
        for row in metadata.get('jobs', metadata.get('existing_jobs', [])):
            if isinstance(row, dict) and row.get('kind') == 'experiment' and row.get('status') not in ('completed', 'failed', 'cancelled', 'not_running'):
                reasons.append('external_experiment_active_or_unknown')
        result = {'observation_id': state['observation_id'], 'allowed': not reasons,
                  'reasons': sorted(set(reasons)), 'resources': current_resources,
                  'budgets': budgets, 'checked_utc': readiness.now()}
        if not reasons:
            result.update(reserved=True, source_fingerprint=state['source_fingerprint'])
            readiness.save(base / 'experiment-admission.json', result)
        return result
