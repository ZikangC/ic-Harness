"""One-shot persistent launch; an existing intent is observed, never resubmitted."""
from pathlib import Path
import datetime, hashlib, json, os, subprocess, sys, time
RUN=Path(__file__).resolve().parent

def load(path):return json.loads(path.read_text())
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def save_new(name,value):
    with (RUN/name).open('x') as f:json.dump(value,f,indent=2);f.write('\n')

def identity(pid):
    p=Path('/proc')/str(pid);st=(p/'stat').read_text().rsplit(')',1)[1].split()
    return {'pid':pid,'start_ticks':st[19],'state':st[0],
            'command':(p/'cmdline').read_bytes().replace(b'\0',b' ').decode(),
            'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip()}

if any((RUN/name).exists() for name in ('launch.intent.json','launch.claim.json','status.json')):
    print(json.dumps({'submitted':False,'reason':'existing launch state; inspect without resubmitting',
                      'status':load(RUN/'status.json') if (RUN/'status.json').is_file() else None}))
    raise SystemExit(0)
request=load(RUN/'request.json');prepared=load(RUN/'preparation.json')
assert sha(RUN/'supervisor.py')==prepared['new_script_sha256']
for rel,expected in prepared['protected_old_files'].items():assert sha(Path(rel))==expected
intent={'job_id':request['job_id'],'candidate_sha256':request['candidate_sha256'],
        'created_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'request_sha256':sha(RUN/'request.json'),'supervisor_sha256':sha(RUN/'supervisor.py'),
        'argv':['/opt/eda/bin/python3','-B',str(RUN/'supervisor.py')],
        'launch_semantics':'one persistent setsid child; no automatic retry or restart'}
save_new('launch.intent.json',intent)
with (RUN/'supervisor.log').open('xb') as log:
    child=subprocess.Popen(intent['argv'],cwd=RUN,start_new_session=True,
                           stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,
                           env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
    record={**intent,'supervisor':identity(child.pid),
            'script_sha256':{'supervisor.py':sha(RUN/'supervisor.py'),
             **{str(p.relative_to(RUN)):sha(p) for p in sorted((RUN/'scripts').glob('*.ys'))}}}
    save_new('launch.json',record)
time.sleep(.5)
code=child.poll()
print(json.dumps({'submitted':True,'run_dir':str(RUN),'job_id':request['job_id'],
                  'supervisor':record['supervisor'],'early_exit_code':code,
                  'status':load(RUN/'status.json') if (RUN/'status.json').is_file() else None}))
if code is not None:raise SystemExit(1)
