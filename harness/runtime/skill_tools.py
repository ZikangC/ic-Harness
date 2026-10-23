"""JSON subprocess execution for caller-selected frozen skill scripts."""
import json
import subprocess
import sys
from harness.adapters.omp import parse_json
from harness.runtime.run_store import dump,digest


def execute_script(out,label,script,request,arguments=()):
    script.resolve().relative_to((out/'snapshot/harness/skills').resolve())
    dump(out/(label+'.request.json'),request)
    result=subprocess.run([sys.executable,'-B',str(script),*arguments],input=json.dumps(request,ensure_ascii=False),capture_output=True,text=True,cwd=out,timeout=30,check=False)
    if result.returncode:raise RuntimeError('frozen skill script failed; exit='+str(result.returncode))
    report=parse_json(result.stdout);dump(out/(label+'.result.json'),report)
    dump(out/(label+'.execution.json'),{'script_sha256':digest(script.read_bytes()),'request_sha256':digest((out/(label+'.request.json')).read_bytes()),'result_sha256':digest((out/(label+'.result.json')).read_bytes()),'exit_code':result.returncode,'timeout_seconds':30})
    return report
