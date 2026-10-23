"""Verify this uploaded closure and prepare run-local outputs. Does not launch EDA."""
from pathlib import Path
import datetime, hashlib, json, shutil, subprocess

RUN = Path(__file__).resolve().parent
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def load(p): return json.loads(Path(p).read_text())
def save_new(name, obj):
    with (RUN/name).open('x') as f:
        json.dump(obj, f, indent=2); f.write('\n')

assert not any((RUN/n).exists() for n in ('preparation.json', 'launch.intent.json', 'launch.claim.json', 'status.json')), 'Existing run: inspect state, do not prepare again'
cfg = load(RUN/'request.json')
assert str(RUN) == cfg['migration']['container_run']
manifest = load(RUN/'transfer_manifest.json')
for rel, expected in manifest['files'].items():
    p = RUN/rel
    assert not p.is_symlink() and p.is_file() and sha(p) == expected, rel
for rel, pin in cfg['expected_rtl'].items():
    assert sha(RUN/'input'/rel) == pin['sha256'], rel
for name, expected in cfg['phase_script_sha256'].items():
    assert sha(RUN/'scripts'/name) == expected, name
for path, pin in cfg['tool_pins'].items():
    assert sha(path) == pin['sha256'], path
assert sha('/opt/eda/bin/python3') == '19f68825fdeba18480909a26f59626c4300de554b13566a7eaa9dff4ed4c7333'
assert sha(cfg['liberty']) == cfg['liberty_sha256']
assert sha(RUN/cfg['review_report_path']) == cfg['review_report_sha256']
lib = RUN/'input/NangateOpenCellLibrary_typical.lib'
assert not lib.exists()
shutil.copyfile(cfg['liberty'], lib)
assert sha(lib) == cfg['liberty_sha256']
for d in ('logs', 'reports', 'checkpoints', 'tmp'):
    (RUN/d).mkdir(exist_ok=False)
for directory in ('input', 'scripts', 'provenance'):
    for p in (RUN/directory).rglob('*'):
        if p.is_file(): p.chmod(0o444)
tools = {
    'yosys_version': subprocess.check_output(['/opt/eda/bin/yosys', '-V'], text=True).strip(),
    'binaries': cfg['tool_pins'],
    'python': {'path': '/opt/eda/bin/python3', 'sha256': sha('/opt/eda/bin/python3'),
               'version': subprocess.check_output(['/opt/eda/bin/python3', '--version'], text=True).strip()},
    'liberty': {'source': cfg['liberty'], 'snapshot': str(lib), 'sha256': sha(lib)},
    'mapping': 'abc -fast; exploratory cell area without timing constraints',
    'constraints': cfg['constraints'],
}
save_new('tools.json', tools)
save_new('preparation.json', {
    'created_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'new_script_sha256': sha(RUN/'supervisor.py'), 'protected_old_files': {},
    'transfer_manifest_sha256': sha(RUN/'transfer_manifest.json'),
    'request_sha256': sha(RUN/'request.json'),
    'source_manifest_sha256': sha(RUN/'source_manifest.json'),
    'phase_scripts_identical': True, 'rtl_closure_sha256': cfg['rtl_closure_sha256'],
    'no_eda_started': True,
})
print(json.dumps({'prepared': str(RUN), 'rtl_files': len(cfg['expected_rtl']), 'all_hashes_verified': True, 'no_eda_started': True, 'tools': tools}))
