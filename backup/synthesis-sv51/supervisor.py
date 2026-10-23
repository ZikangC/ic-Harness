"""One persistent, sequential run of four pinned synthesis scripts. No RTL edits."""
from pathlib import Path
import collections, datetime, fcntl, hashlib, json, os, resource, shutil, subprocess, time, traceback

RUN = Path(__file__).resolve().parent
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat()
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def load(p): return json.loads(Path(p).read_text())
def save(name, value):
    p = RUN/name
    tmp = p.with_name(p.name+'.tmp.'+str(os.getpid()))
    with tmp.open('w') as f:
        json.dump(value, f, indent=2); f.write('\n'); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, p)

CFG = load(RUN/'request.json')
BOOT = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
HZ = os.sysconf('SC_CLK_TCK')
def proc(pid):
    p = Path('/proc')/str(pid)
    st = (p/'stat').read_text().rsplit(')', 1)[1].split()
    status = (p/'status').read_text().splitlines()
    rss = next((int(x.split()[1])*1024 for x in status if x.startswith('VmRSS:')), 0)
    return {'pid': int(pid), 'start_ticks': st[19], 'state': st[0], 'ppid': int(st[1]),
            'cpu_seconds': (int(st[11])+int(st[12]))/HZ, 'rss_bytes': rss,
            'command': (p/'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace'),
            'boot_id': BOOT}

def active_eda():
    result = []
    for p in Path('/proc').iterdir():
        if not p.name.isdigit(): continue
        try:
            if (p/'comm').read_text().strip() in ('yosys', 'yosys-abc', 'abc', 'openroad', 'sta'):
                ident = proc(p.name)
                if ident['state'] != 'Z': result.append(ident)
        except (FileNotFoundError, ProcessLookupError, PermissionError): pass
    return result

def verify():
    prep = load(RUN/'preparation.json')
    assert str(RUN) == CFG['migration']['container_run']
    assert sha(RUN/'request.json') == prep['request_sha256']
    assert sha(RUN/'source_manifest.json') == prep['source_manifest_sha256']
    assert sha(RUN/'supervisor.py') == prep['new_script_sha256']
    assert sha(RUN/'transfer_manifest.json') == prep['transfer_manifest_sha256']
    for rel, expected in load(RUN/'transfer_manifest.json')['files'].items():
        p = RUN/rel
        assert p.is_file() and not p.is_symlink() and sha(p) == expected, rel
    for rel, pin in CFG['expected_rtl'].items():
        assert sha(RUN/'input'/rel) == pin['sha256'], rel
    assert sha(RUN/CFG['review_report_path']) == CFG['review_report_sha256']
    for name, expected in CFG['phase_script_sha256'].items():
        assert sha(RUN/'scripts'/name) == expected, name
    for path, pin in CFG['tool_pins'].items():
        assert sha(path) == pin['sha256'], path
    assert sha('/opt/eda/bin/python3') == '19f68825fdeba18480909a26f59626c4300de554b13566a7eaa9dff4ed4c7333'
    assert sha(RUN/'input/NangateOpenCellLibrary_typical.lib') == CFG['liberty_sha256']

def admission():
    active = active_eda()
    mem = {line.split(':')[0]: int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith(('MemAvailable:', 'MemTotal:'))}
    disk = shutil.disk_usage(RUN)
    observation = {'observed_utc': now(), 'active_eda': active, 'memory': mem,
                   'disk_free_bytes': disk.free, 'load': os.getloadavg(), 'cpu_count': os.cpu_count(),
                   'scope': 'single sequential job, nice 10, thread environment 1, per-process 128 GiB address space limit'}
    save('admission.json', observation)
    assert not active, 'Active EDA detected on destination; do not compete or resubmit'
    assert mem['MemAvailable'] > CFG['min_available_gib']*1024**3, 'Insufficient memory headroom'
    assert disk.free > CFG['min_disk_free_gib']*1024**3, 'Insufficient disk headroom'
    for other in RUN.parent.glob('synth--*/request.json'):
        if other.parent != RUN and load(other).get('candidate_sha256') == CFG['candidate_sha256']:
            raise RuntimeError('Same candidate already has another local synthesis request: '+str(other))

def binary_int(v):
    if isinstance(v, int): return v
    assert isinstance(v, str) and set(v) <= set('01'), repr(v)
    return int(v, 2)

def memory_report():
    design = load(RUN/'reports/coarse.design.json')
    modules = design['modules']
    macros, internal_memories, mem_modules = [], [], []
    def walk(name, path, stack):
        assert name not in stack, 'Recursive module hierarchy'
        mod = modules[name]
        params = mod.get('parameter_default_values', {})
        attrs = mod.get('attributes', {})
        if binary_int(attrs.get('blackbox', 0)):
            assert 'sram_1r1w' in name, 'Unexpected blackbox: '+name
            assert not mod.get('cells') and not mod.get('memories'), 'SRAM model was not blackboxed'
            depth, width = binary_int(params['DEPTH']), binary_int(params['WIDTH'])
            macros.append({'instance': path, 'module': name, 'depth': depth, 'width': width, 'bits': depth*width})
            return
        if 'nano_mem' in name:
            mem_modules.append({'instance': path, 'FDEPTH': binary_int(params['FDEPTH']), 'FAW': binary_int(params['FAW'])})
        for n, m in mod.get('memories', {}).items():
            internal_memories.append({'instance': path+'/'+n, 'kind': 'RTLIL memory', 'width': m.get('width'), 'size': m.get('size')})
        for n, cell in mod.get('cells', {}).items():
            typ = cell['type']
            if typ in modules: walk(typ, path+'/'+n, stack+[name])
            elif typ.startswith('$mem'):
                p = cell.get('parameters', {})
                internal_memories.append({'instance': path+'/'+n, 'kind': typ,
                    'width': binary_int(p['WIDTH']) if 'WIDTH' in p else None,
                    'size': binary_int(p['SIZE']) if 'SIZE' in p else None})
    walk(CFG['top'], CFG['top'], [])
    result = {'observed_utc': now(), 'macro_instances': len(macros), 'macro_bits': sum(m['bits'] for m in macros),
              'macros': macros, 'nonmacro_memories_at_coarse': internal_memories, 'nano_mem_instances': mem_modules,
              'macro_area': None, 'macro_timing_models': None,
              'area_scope': 'Actual SRAM area unknown; mapped standard-cell area excludes these blackboxes. Nonmacro memory can be mapped to logic.'}
    save('reports/memory_resources.json', result)
    expected = CFG['macro_expectation']
    assert len(macros) == expected['instances'] and result['macro_bits'] == expected['total_bits'], 'SRAM topology differs from pinned expectation'
    assert mem_modules and all(m['FDEPTH'] == 64 and m['FAW'] == 6 for m in mem_modules), 'Unexpected FIFO parameters'
    del design

# Exclusively claim this run before publishing any mutable status.
lock = (RUN/'job.lock').open('a')
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
with (RUN/'launch.claim.json').open('x') as f:
    json.dump({'job_id': CFG['job_id'], 'claimed_utc': now(), 'supervisor': proc(os.getpid())}, f, indent=2)
state = {'job_id': CFG['job_id'], 'candidate_sha256': CFG['candidate_sha256'], 'rtl_closure_sha256': CFG['rtl_closure_sha256'],
         'state': 'preflight', 'started_utc': now(), 'supervisor': proc(os.getpid()), 'phase': None, 'phases': []}
child = None
try:
    save('status.json', state)
    verify(); admission()
    os.nice(10)
    limit = CFG['address_space_gib']*1024**3
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    save('identity.json', {'verified_utc': now(), 'candidate_sha256': CFG['candidate_sha256'],
        'native_identity_scope': CFG['migration']['identity_scope'], 'rtl_closure_sha256': CFG['rtl_closure_sha256'],
        'origin_candidate_sha256': CFG['origin_candidate_sha256'], 'architecture_baseline_sha256': CFG['origin_architecture_baseline_sha256'],
        'review_report_sha256': CFG['review_report_sha256'], 'constraints': CFG['constraints'],
        'destination': CFG['migration'], 'supervisor': proc(os.getpid())})
    env = dict(os.environ, OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1',
               TMPDIR=str(RUN/'tmp'), PYTHONDONTWRITEBYTECODE='1')
    for phase in ('01_elaborate', '02_coarse', '03_generic', '04_map'):
        verify()
        begin = time.monotonic()
        record = {'phase': phase, 'started_utc': now(), 'script_sha256': sha(RUN/'scripts'/(phase+'.ys')),
                  'argv': ['/opt/eda/bin/yosys', '-T', '-l', str(RUN/'logs'/(phase+'.yosys.log')), '-s', str(RUN/'scripts'/(phase+'.ys'))]}
        state.update(state='running', phase=phase, current=record)
        save('status.json', state)
        with (RUN/'logs'/(phase+'.console.log')).open('xb') as out:
            child = subprocess.Popen(record['argv'], cwd=RUN, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, env=env)
            record['process'] = proc(child.pid)
            save('reports/'+phase+'.launch.json', record)
            while child.poll() is None:
                try: record['process'] = proc(child.pid)
                except (FileNotFoundError, ProcessLookupError): pass
                record['elapsed_seconds'] = time.monotonic()-begin
                record['observed_utc'] = now()
                state['supervisor'] = proc(os.getpid())
                save('status.json', state)
                time.sleep(5)
            record.update(exit_code=child.returncode, ended_utc=now(), elapsed_seconds=time.monotonic()-begin)
            child = None
        save('reports/'+phase+'.result.json', record)
        state['phases'].append(dict(record))
        save('status.json', state)
        assert record['exit_code'] == 0, phase+' failed; inspect logs and result'
        verify()
        if phase == '02_coarse': memory_report()
    state.update(state='completed', completed_utc=now(), phase=None,
                 conclusion='All four synthesis commands and structure checks completed. SRAM physical area and STA are unavailable; reports require interpretation.')
    state.pop('current', None)
    save('reports/completion.json', state)
    save('status.json', state)
except BaseException as exc:
    # If a supervisor-only check fails while a child survives, record it; never kill or retry it.
    alive = child is not None and child.poll() is None
    state.update(state='supervisor_error_child_running' if alive else 'failed', ended_utc=now(), error=str(exc), traceback=traceback.format_exc())
    if alive:
        try: state['surviving_child'] = proc(child.pid)
        except (FileNotFoundError, ProcessLookupError): pass
    save('status.json', state)
    print(state['traceback'], flush=True)
    raise
