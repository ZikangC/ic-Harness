#!/usr/bin/env python3
"""Host-only Docker job supervisor (Python 3.6 stdlib; never imported by targets).

Each job receives the existing control container's volumes read-only, its exact
editing scope read-write, and private disk-backed temporary directories. Failure
never falls back to execution in the control container.
"""
import hashlib
import json
import os
import posixpath
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BACKEND = 'docker_readonly_job_v1'
LABEL = 'harness.rtl.job.'
ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]*\Z')
CID = re.compile(r'^[0-9a-f]{64}\Z')
SCOPE = re.compile(r'^(work|audit/review-[1-9][0-9]*|architecture/revision-[1-9][0-9]*/(candidate|audit)|readiness/campaign-[1-9][0-9]*/(work|audit))\Z')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text())


def save(path, value, exclusive=False):
    path = Path(path)
    raw = (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()
    if exclusive:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
        return
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def absolute(value):
    if not isinstance(value, str) or not value.startswith('/') or '\x00' in value:
        raise ValueError('expected absolute path')
    if posixpath.normpath(value) != value or value == '/':
        raise ValueError('non-canonical or root path forbidden')
    return value


def inside(path, root):
    return path == root or path.startswith(root.rstrip('/') + '/')


def plain_host(path, directory=None):
    """No symlink is allowed in trusted host metadata or mount paths."""
    path = Path(absolute(str(path)))
    for part in [path] + list(path.parents):
        if part.is_symlink():
            raise ValueError('symlink in host path: ' + str(part))
    if not path.exists():
        raise ValueError('missing host path: ' + str(path))
    if directory is True and not path.is_dir():
        raise ValueError('expected host directory')
    if directory is False and not path.is_file():
        raise ValueError('expected host file')
    return path


def docker(args, input_data=None, timeout=30, allow_failure=False):
    result = subprocess.run(['docker'] + list(args), input=input_data,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=timeout)
    if result.returncode and not allow_failure:
        raise RuntimeError('docker command failed ({0}): {1}'.format(
            args[0], result.stderr.decode('utf-8', 'replace')[-2000:]))
    return result.stdout.decode('utf-8')


def inspect_container(identifier):
    objects = json.loads(docker(['inspect', '--type', 'container', identifier]))
    if len(objects) != 1 or not CID.match(objects[0].get('Id', '')):
        raise ValueError('invalid Docker inspection')
    return objects[0]


def unescape_mount(value):
    return re.sub(r'\\([0-7]{3})', lambda match: chr(int(match.group(1), 8)), value)


def reject_nested_mounts(sources):
    # Linux <5.12 cannot recursively force read-only bind submounts. Do not
    # assume a top-level :ro covers a host filesystem mounted beneath it.
    mountinfo = Path('/proc/self/mountinfo')
    if not mountinfo.is_file():
        raise ValueError('host mount inventory unavailable')
    mounts = [unescape_mount(line.split()[4]) for line in mountinfo.read_text().splitlines()]
    for source in sources:
        for mount in mounts:
            if mount != source and inside(mount, source):
                raise ValueError('nested host mount cannot be proved read-only: ' + mount)


def validate_payload(payload):
    for key in ('control_container', 'run_id'):
        if not isinstance(payload.get(key), str) or not ID.match(payload[key]):
            raise ValueError('invalid ' + key)
    for key in ('host_project_root', 'container_project_root', 'host_workspace_root',
                'container_workspace_root', 'worker', 'eda_python'):
        absolute(payload.get(key))
    if not inside(payload['host_project_root'], payload['host_workspace_root']):
        raise ValueError('host project outside workspace')
    if not inside(payload['container_project_root'], payload['container_workspace_root']):
        raise ValueError('container project outside workspace')
    if (payload['host_project_root'][len(payload['host_workspace_root']):] !=
            payload['container_project_root'][len(payload['container_workspace_root']):]):
        raise ValueError('project mapping mismatch')


def mapped(payload, container_path):
    absolute(container_path)
    if not inside(container_path, payload['container_workspace_root']):
        raise ValueError('container path outside workspace')
    return Path(payload['host_workspace_root'] + container_path[len(payload['container_workspace_root']):])


def control_spec(payload):
    validate_payload(payload)
    control = inspect_container(payload['control_container'])
    if not control.get('State', {}).get('Running'):
        raise ValueError('control container not running')
    if not re.match(r'^sha256:[0-9a-f]{64}$', control.get('Image', '')):
        raise ValueError('control image identity unavailable')
    sources = []
    mapping = False
    for mount in control.get('Mounts', []):
        destination = absolute(mount.get('Destination'))
        source = mount.get('Source', '')
        if ('docker.sock' in source or 'docker.sock' in destination or
                destination.startswith('/run/docker') or destination.startswith('/var/run/docker')):
            raise ValueError('control container exposes Docker control socket')
        if mount.get('Type') not in ('bind', 'volume'):
            raise ValueError('unsupported inherited mount type')
        source = str(plain_host(source, directory=None))
        if ',' in source or ',' in destination or ':' in source or ':' in destination:
            raise ValueError('unsupported mount path separator')
        sources.append(source)
        if destination == payload['container_workspace_root']:
            if source != payload['host_workspace_root'] or mount.get('Type') != 'bind':
                raise ValueError('workspace bind differs from configured mapping')
            mapping = True
    if not mapping:
        raise ValueError('workspace bind not found')
    reject_nested_mounts(sources)
    plain_host(payload['host_project_root'], directory=True)
    identity = json.loads(docker(['exec', control['Id'], payload['eda_python'], '-B', '-c',
                                 'import json,os;print(json.dumps({"uid":os.getuid(),"gid":os.getgid()}))']))
    if (type(identity.get('uid')) is not int or type(identity.get('gid')) is not int or
            identity['uid'] <= 0 or identity['gid'] < 0):
        raise ValueError('jobs require a non-root numeric control user')
    return {'backend': BACKEND, 'control_container_id': control['Id'],
            'image_id': control['Image'], 'uid': identity['uid'], 'gid': identity['gid'],
            'host_workspace_root': payload['host_workspace_root'],
            'container_workspace_root': payload['container_workspace_root']}


def load_context(payload):
    validate_payload(payload)
    job_id = payload.get('job_id')
    if not isinstance(job_id, str) or not ID.match(job_id):
        raise ValueError('invalid job id')
    run = plain_host(Path(payload['host_project_root']) / 'output' / payload['run_id'], True)
    job = plain_host(run / 'jobs' / job_id, True)
    manifest = read_json(plain_host(run / 'manifest.json', False))
    request = read_json(plain_host(job / 'request.json', False))
    spec = manifest.get('job_isolation')
    if not isinstance(spec, dict) or spec.get('backend') != BACKEND or request.get('job_isolation') != spec:
        raise ValueError('job isolation manifest mismatch')
    if control_spec(payload) != spec:
        raise ValueError('control container or isolation identity changed')
    container_run = payload['container_project_root'] + '/output/' + payload['run_id']
    worker = container_run + '/snapshot/harness/runtime/rtl_worker.py'
    if payload['worker'] != worker:
        raise ValueError('worker is not this run frozen worker')
    helper = plain_host(mapped(payload, worker).with_name('rtl_job_host.py'), False)
    if helper.resolve() != Path(__file__).resolve():
        raise ValueError('host helper is not this run frozen helper')
    if sha(helper) != manifest.get('harness_sha256', {}).get('runtime/rtl_job_host.py'):
        raise ValueError('frozen host helper changed')
    if sha(plain_host(mapped(payload, worker), False)) != manifest.get('harness_sha256', {}).get('runtime/rtl_worker.py'):
        raise ValueError('frozen worker changed')
    scope = request.get('scope')
    if not isinstance(scope, str) or not SCOPE.match(scope):
        raise ValueError('invalid writable job scope')
    source = container_run + '/' + scope
    if request.get('run_dir') != container_run or request.get('source_dir') != source:
        raise ValueError('job source path mismatch')
    host_source = plain_host(mapped(payload, source), True)
    cwd = absolute(request.get('cwd'))
    if not inside(cwd, source):
        raise ValueError('job cwd outside writable scope')
    plain_host(mapped(payload, cwd), True)
    argv = request.get('argv')
    if (not isinstance(argv, list) or not argv or any(not isinstance(v, str) or '\x00' in v for v in argv)
            or not argv[0].startswith('/')):
        raise ValueError('invalid explicit command argv')
    allowed = manifest.get('settings', {}).get('tools', {})
    if argv[0] not in set(allowed.values()):
        raise ValueError('job command is not a configured tool')
    timeout = request.get('timeout_seconds')
    if type(timeout) is not int or timeout <= 0:
        raise ValueError('invalid command timeout')
    return {'payload': payload, 'run': run, 'job': job, 'manifest': manifest,
            'request_sha256': sha(job / 'request.json'),
            'request': request, 'spec': spec, 'host_source': host_source}


def labels(context):
    payload = context['payload']
    return {LABEL + 'backend': BACKEND, LABEL + 'run': payload['run_id'],
            LABEL + 'job': payload['job_id'],
            LABEL + 'nonce': read_json(context['job'] / 'host-launch.intent.json')['nonce']}


def own_container(context, cid):
    if not CID.match(cid):
        raise ValueError('invalid own container ID')
    info = inspect_container(cid)
    if info['Id'] != cid or info.get('Image') != context['spec']['image_id']:
        raise ValueError('job container identity mismatch')
    actual = info.get('Config', {}).get('Labels') or {}
    if any(actual.get(key) != value for key, value in labels(context).items()):
        raise ValueError('refusing to control a container with different ownership labels')
    return info


def assert_mounts(context, info):
    request = context['request']
    expected = {request['source_dir']: str(context['host_source']),
                '/tmp': str(context['job'] / 'private-tmp'),
                '/var/tmp': str(context['job'] / 'private-tmp')}
    found = {}
    for mount in info.get('Mounts', []):
        destination = mount.get('Destination')
        if mount.get('RW'):
            if destination not in expected or mount.get('Source') != expected[destination]:
                raise ValueError('unexpected writable job mount')
            found[destination] = mount['Source']
    if found != expected or not info.get('HostConfig', {}).get('ReadonlyRootfs'):
        raise ValueError('job mounts are not read-only outside its scope and private tmp')
    config = info.get('Config', {})
    if config.get('User') != '{0}:{1}'.format(context['spec']['uid'], context['spec']['gid']):
        raise ValueError('job Docker user mismatch')
    host = info.get('HostConfig', {})
    if host.get('Privileged') or host.get('CapAdd'):
        raise ValueError('unexpected job capabilities')
    if 'ALL' not in (host.get('CapDrop') or []):
        raise ValueError('job capabilities were not dropped')
    if not any(str(v).startswith('no-new-privileges') for v in host.get('SecurityOpt') or []):
        raise ValueError('job no-new-privileges missing')


def create_argv(context):
    request, spec = context['request'], context['spec']
    temporary = str(context['job'] / 'private-tmp')
    args = ['create', '--read-only', '--cap-drop', 'ALL', '--security-opt',
            'no-new-privileges:true', '--user', '{0}:{1}'.format(spec['uid'], spec['gid']),
            '--init', '--no-healthcheck', '--network', 'container:' + spec['control_container_id'],
            '--volumes-from', spec['control_container_id'] + ':ro']
    for key, value in sorted(labels(context).items()):
        args += ['--label', key + '=' + value]
    for source, destination in ((str(context['host_source']), request['source_dir']),
                                (temporary, '/tmp'), (temporary, '/var/tmp')):
        if any(token in source + destination for token in (',', ':', '\x00')):
            raise ValueError('unsupported bind path')
        args += ['--mount', 'type=bind,src=' + source + ',dst=' + destination + ',bind-propagation=rprivate']
    for value in ('TMPDIR=/tmp', 'TMP=/tmp', 'TEMP=/tmp', 'XDG_CACHE_HOME=/tmp/cache',
                  'PYTHONDONTWRITEBYTECODE=1'):
        args += ['--env', value]
    args += ['--workdir', request['cwd'], '--entrypoint', request['argv'][0], spec['image_id']]
    return args + request['argv'][1:]


class JobIntegrityError(ValueError):
    pass


class JobSourceCheckError(RuntimeError):
    def __init__(self, message, failure):
        RuntimeError.__init__(self, message)
        self.failure = failure


def ordinary_work_inventory_failure(context, phase, response):
    """Accept only the frozen worker's narrow, bound incomplete-read report.

    Unknown classifications preserve the existing fail-closed integrity path.
    This exception never supplies a successful before/after source inventory.
    """
    if 'job_check_failure' not in response:
        return None
    failure = response['job_check_failure']
    request = context['request']
    fields = {'version', 'kind', 'job_id', 'phase', 'stage', 'scope', 'source_dir',
              'request_sha256', 'guard_set_sha256', 'protected_guards_checked', 'source_containment_checked',
              'native_job', 'exception_type', 'message', 'errno', 'filename'}
    if (set(response) != {'ok', 'error', 'job_check_failure'} or
            not isinstance(failure, dict) or set(failure) != fields or
            type(failure.get('version')) is not int or failure['version'] != 2 or
            failure.get('kind') != 'ordinary_work_inventory_incomplete' or
            request.get('scope') != 'work' or request.get('structure_check') is not None or
            failure.get('job_id') != context['payload']['job_id'] or failure.get('phase') != phase or
            failure.get('stage') not in ('writable_scope_links', 'source_inventory') or
            failure.get('scope') != 'work' or failure.get('source_dir') != request['source_dir'] or
            failure.get('request_sha256') != context['request_sha256'] or
            failure.get('guard_set_sha256') != digest(request.get('integrity_guards', [])) or
            failure.get('protected_guards_checked') is not True or failure.get('source_containment_checked') is not True or
            failure.get('native_job') is not False or
            failure.get('exception_type') != 'FileNotFoundError' or
            not isinstance(failure.get('message'), str) or
            (failure.get('errno') is not None and (type(failure['errno']) is not int or failure['errno'] != 2)) or
            (failure.get('filename') is not None and not isinstance(failure['filename'], str)) or
            response.get('ok') is not False or response.get('error') != 'FileNotFoundError: ' + failure['message']):
        raise JobIntegrityError('incomplete or mismatched ordinary-work inventory failure record')
    return failure


def control_check(context, phase):
    payload = context['payload']
    body = json.dumps({'action': 'job_check', 'job_id': payload['job_id'], 'phase': phase}).encode()
    # Full guarded trees can take minutes; no transport-wide 60s deadline.
    raw = docker(['exec', '-i', context['spec']['control_container_id'], payload['eda_python'],
                  '-B', payload['worker'], '--root', payload['container_project_root'],
                  '--run', payload['run_id']], body, timeout=None, allow_failure=True)
    try:
        response = json.loads(raw)
    except (ValueError, TypeError):
        raise RuntimeError('control job check returned no valid JSON response')
    failure = ordinary_work_inventory_failure(context, phase, response)
    if failure is not None:
        raise JobSourceCheckError(response['error'], failure)
    if response.get('error') or response.get('ok') is not True:
        raise JobIntegrityError('job integrity check failed: ' + str(response.get('error', response)))
    return response


def cleanup(context, cid, kill=False):
    info = own_container(context, cid)
    if info.get('State', {}).get('Running'):
        if not kill:
            return {'cleanup_complete': False, 'container_running': True}
        docker(['kill', cid])
        info = own_container(context, cid)
        if info.get('State', {}).get('Running'):
            return {'cleanup_complete': False, 'container_running': True}
    docker(['rm', cid])
    return {'cleanup_complete': True, 'container_running': False,
            'container_exit_code': info.get('State', {}).get('ExitCode')}


def result_base(context):
    request = context['request']
    return {'exit_code': None, 'timed_out': False, 'source_sha256': request['source_sha256'],
            'source_closure_sha256': digest(request['source_sha256']),
            'candidate_sha256': request.get('candidate_sha256'),
            'job_isolation': context['spec'], 'cleanup_complete': True, 'container_running': False}


def record_setup_failure(payload, error, supervised=False):
    """Finish an already queued request only when no target could have started.

    A duplicate launch never gets to manufacture a result for an active job.
    """
    validate_payload(payload)
    job_id = payload.get('job_id')
    if not isinstance(job_id, str) or not ID.match(job_id):
        return
    run = plain_host(Path(payload['host_project_root']) / 'output' / payload['run_id'], True)
    job = plain_host(run / 'jobs' / job_id, True)
    manifest = read_json(plain_host(run / 'manifest.json', False))
    request = read_json(plain_host(job / 'request.json', False))
    if manifest.get('job_isolation', {}).get('backend') != BACKEND or (job / 'result.json').exists():
        return
    if (job / 'container.json').exists():
        return
    intent = job / 'host-launch.intent.json'
    if supervised:
        if (job / 'host-supervise.intent.json').exists():
            return
        descriptor = read_json(plain_host(job / 'host-launch.json', False))
        if (descriptor.get('payload') != payload or
                descriptor.get('request_sha256') != sha(job / 'request.json') or
                descriptor.get('helper_sha256') != sha(__file__)):
            return
    else:
        if intent.exists():
            return
        try:
            save(intent, {'nonce': hashlib.sha256(os.urandom(32)).hexdigest(),
                          'job_id': job_id, 'setup_failed': True}, exclusive=True)
        except FileExistsError:
            return
    result = {'exit_code': None, 'timed_out': False, 'error': type(error).__name__ + ': ' + str(error),
              'sandbox_setup_failed': True, 'elapsed_seconds': 0,
              'source_sha256': request.get('source_sha256'), 'candidate_sha256': request.get('candidate_sha256'),
              'job_isolation': manifest.get('job_isolation'),
              'isolation': dict(manifest.get('job_isolation', {}), cleanup_complete=True, container_running=False),
              'cleanup_complete': True, 'container_running': False}
    save(job / 'result.json', result, exclusive=True)


def supervise(payload):
    try:
        context = load_context(payload)
    except Exception as error:
        record_setup_failure(payload, error, supervised=True)
        raise
    job, request = context['job'], context['request']
    # Exclusive ownership precedes descriptor validation, so malformed launch
    # metadata can finish as a no-target setup failure without racing an active
    # supervisor. Duplicate supervisors never write another job's result.
    save(job / 'host-supervise.intent.json', {'pid': os.getpid()}, exclusive=True)
    result, started, cid, target_started = result_base(context), time.monotonic(), None, False
    try:
        descriptor = read_json(job / 'host-launch.json')
        if descriptor.get('payload') != payload or descriptor.get('request_sha256') != sha(job / 'request.json'):
            raise ValueError('launch descriptor changed')
        if descriptor.get('manifest_sha256') != sha(context['run'] / 'manifest.json'):
            raise ValueError('launch manifest changed')
        if descriptor.get('helper_sha256') != sha(__file__):
            raise ValueError('launch helper changed')
        before = control_check(context, 'before')
        result['source_at_start_sha256'] = before['source_at_start_sha256']
        if (job / 'cancel.request.json').exists():
            result['cancelled'] = True
            raise ValueError('job cancelled before target launch')
        temporary = job / 'private-tmp'
        temporary.mkdir(mode=0o700)
        os.chown(str(temporary), context['spec']['uid'], context['spec']['gid'])
        cid = docker(create_argv(context)).strip()
        if not CID.match(cid):
            raise ValueError('docker create returned invalid container ID')
        result['container_id'] = cid
        save(job / 'container.json', {'container_id': cid, 'image_id': context['spec']['image_id'],
                                     'labels': labels(context)})
        info = own_container(context, cid)
        assert_mounts(context, info)
        save(job / 'container-created.json', {'container_id': cid, 'image_id': info['Image'],
                                             'mounts': info.get('Mounts'), 'state': info.get('State')})
        if (job / 'cancel.request.json').exists():
            result['cancelled'] = True
            raise ValueError('job cancelled before Docker start')
        with (job / 'stdout.log').open('wb') as stdout, (job / 'stderr.log').open('wb') as stderr:
            process = subprocess.Popen(['docker', 'start', '--attach', cid], stdin=subprocess.DEVNULL,
                                       stdout=stdout, stderr=stderr, close_fds=True)
            target_started = True
            deadline = time.monotonic() + request['timeout_seconds']
            while process.poll() is None:
                cancelled = (job / 'cancel.request.json').exists()
                if cancelled or time.monotonic() >= deadline:
                    result['cancelled'] = cancelled
                    result['timed_out'] = not cancelled
                    result.update(cleanup(context, cid, kill=True))
                    cid = None
                    process.wait(timeout=30)
                    break
                time.sleep(0.2)
            process.wait()
        if cid:
            info = own_container(context, cid)
            save(job / 'container-finished.json', {'container_id': cid, 'image_id': info['Image'],
                                                  'state': info.get('State')})
            state = info.get('State', {})
            if state.get('Running'):
                raise ValueError('Docker attach exited while job remains running')
            if (state.get('Status') != 'exited' or not state.get('StartedAt') or
                    state['StartedAt'].startswith('0001-') or state.get('Error')):
                target_started = False
                raise RuntimeError('Docker target did not start and exit normally: ' + str(state.get('Error', state.get('Status'))))
            result['exit_code'] = state.get('ExitCode')
            result.update(cleanup(context, cid))
            cid = None
        elif result.get('timed_out') or result.get('cancelled'):
            result['exit_code'] = result.get('container_exit_code', process.returncode)
        after = control_check(context, 'after')
        result['source_after_sha256'] = after['source_after_sha256']
    except Exception as error:
        result['error'] = type(error).__name__ + ': ' + str(error)
        if not target_started:
            result['sandbox_setup_failed'] = True
        if isinstance(error, JobIntegrityError):
            result['integrity_error'] = str(error)
        if isinstance(error, JobSourceCheckError):
            result['source_check_error'] = error.failure
    finally:
        if cid:
            try:
                result.update(cleanup(context, cid, kill=True))
            except Exception as error:
                result.update(cleanup_complete=False, container_running=True,
                              cleanup_error=type(error).__name__ + ': ' + str(error), container_id=cid)
        result['isolation'] = dict(context['spec'], cleanup_complete=result['cleanup_complete'],
                                   container_running=result['container_running'], container_id=cid or result.get('container_id'))
        result['elapsed_seconds'] = time.monotonic() - started
        save(job / 'result.json', result, exclusive=True)
    return {'ok': True, 'job_id': payload['job_id'], 'status': 'completed' if result['cleanup_complete'] else 'cleanup_failed'}


def launch(payload):
    try:
        context = load_context(payload)
    except Exception as error:
        record_setup_failure(payload, error, supervised=False)
        raise
    job = context['job']
    if (job / 'result.json').exists():
        raise ValueError('job already has a result; refusing duplicate launch')
    nonce = hashlib.sha256(os.urandom(32)).hexdigest()
    save(job / 'host-launch.intent.json', {'nonce': nonce, 'job_id': payload['job_id']}, exclusive=True)
    descriptor = {'payload': payload, 'request_sha256': sha(job / 'request.json'),
                  'manifest_sha256': sha(context['run'] / 'manifest.json'), 'helper_sha256': sha(__file__)}
    save(job / 'host-launch.json', descriptor, exclusive=True)
    process = None
    try:
        with (job / 'host-launch.json').open('rb') as stdin, (job / 'supervisor.log').open('wb') as log:
            process = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), '--supervise'],
                                       stdin=stdin, stdout=log, stderr=log, start_new_session=True, close_fds=True)
    except Exception as error:
        # Once spawn succeeds only that child may publish its final result.
        # Even a local log-close error must not declare a running child clean.
        if process is None:
            result = result_base(context)
            result.update(error=type(error).__name__ + ': ' + str(error), sandbox_setup_failed=True,
                          isolation=dict(context['spec'], cleanup_complete=True, container_running=False), elapsed_seconds=0)
            save(job / 'result.json', result, exclusive=True)
        raise
    save(job / 'host-supervisor.json', {'pid': process.pid, 'helper_sha256': sha(__file__)})
    return {'ok': True, 'job_id': payload['job_id'], 'status': 'submitted', 'logs': str(job)}


def cancel(payload):
    context = load_context(payload)
    job = context['job']
    marker = job / 'cancel.request.json'
    if not marker.exists():
        try:
            save(marker, {'job_id': payload['job_id'], 'requested_unix': time.time()}, exclusive=True)
        except FileExistsError:
            pass
    # Exactly one supervisor owns inspect/kill/remove; a second kill races
    # normal timeout/exit cleanup. This durable marker is checked before
    # Docker start and at most 200ms apart while an attached target runs.
    result_path = job / 'result.json'
    if result_path.exists() and read_json(result_path).get('isolation', {}).get('cleanup_complete') is False:
        return {'ok': True, 'job_id': payload['job_id'], 'status': 'cleanup_failed',
                'requires_operator': True, 'cleanup_complete': False}
    return {'ok': True, 'job_id': payload['job_id'], 'status': 'cancel_requested'}


def main():
    try:
        value = json.load(sys.stdin)
        if sys.argv[1:] == ['--supervise']:
            result = supervise(value['payload'])
        else:
            action = value.get('action')
            if action == 'describe':
                result = {'ok': True, 'job_isolation': control_spec(value)}
            elif action in ('launch', 'supervise', 'cancel'):
                result = {'launch': launch, 'supervise': supervise, 'cancel': cancel}[action](value)
            else:
                raise ValueError('unsupported host job action')
        print(json.dumps(result))
    except Exception as error:
        print(json.dumps({'error': type(error).__name__ + ': ' + str(error)}))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
