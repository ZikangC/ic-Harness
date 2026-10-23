"""Small JSON requests over SSH; generated artifacts remain on the server."""
import json,subprocess,shlex,time
from pathlib import PurePosixPath

class PendingJobsInterrupted(KeyboardInterrupt):
 """The caller stopped waiting; detached server jobs retain their real lifecycle."""

def wait_for_jobs(remote,poll_seconds=2):
 """Drain existing jobs before a freeze/transition; do not interpret tool verdicts."""
 seen=set()
 try:
  while True:
   pending=remote.call({'action':'jobs','pending_only':True})['jobs']
   if any(job.get('status')=='cleanup_failed'for job in pending):
    raise RuntimeError('job isolation cleanup failed; reconcile the recorded container before continuing')
   if not pending:
    return {'waited':bool(seen),'jobs':[remote.call({'action':'status','job_id':job,'brief':True})for job in sorted(seen)]}
   seen.update(job['job_id']for job in pending)
   time.sleep(poll_seconds)
 except KeyboardInterrupt as error:
  raise PendingJobsInterrupted()from error

class Remote:
 def __init__(self,environment,run_id,worker=None):
  self.environment=environment;self.run_id=run_id;r=environment['remote'];name=r.get('project_name')or environment['_project_name'];self.project=str(PurePosixPath(r['container_workspace_root'])/name);self.worker=worker or self.project+'/harness/runtime/rtl_worker.py'
 def host_call(self,action,job_id=None):
  r=self.environment['remote'];container_workspace=PurePosixPath(r['container_workspace_root']);host_workspace=PurePosixPath(r['host_workspace_root'])
  project_relative=PurePosixPath(self.project).relative_to(container_workspace)
  helper=PurePosixPath(self.worker).with_name('rtl_job_host.py')
  host_helper=host_workspace/helper.relative_to(container_workspace)
  payload={'action':action,'control_container':r['container'],'host_project_root':str(host_workspace/project_relative),'container_project_root':self.project,'host_workspace_root':str(host_workspace),'container_workspace_root':str(container_workspace),'worker':self.worker,'eda_python':self.environment['eda']['python'],'run_id':self.run_id}
  if job_id is not None:payload['job_id']=job_id
  command=[r.get('host_python','/usr/bin/python3'),'-B',str(host_helper)]
  argv=['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o',f"ConnectTimeout={r['connect_timeout_seconds']}",'-o','ServerAliveInterval=10','-o','ServerAliveCountMax=3',r['ssh_host'],shlex.join(command)]
  try:p=subprocess.run(argv,input=json.dumps(payload),text=True,capture_output=True)
  except (OSError,subprocess.SubprocessError) as error:
   raise ConnectionError('job launcher transport failure; action='+action+'; category='+type(error).__name__+'; outcome unknown')from None
  try:result=json.loads(p.stdout)
  except ValueError:raise ConnectionError('job launcher protocol failure; action='+action+'; outcome unknown')from None
  if result.get('error'):raise RuntimeError(result['error'])
  if p.returncode:raise ConnectionError('job launcher transport failure; action='+action+'; outcome unknown')
  return result

 def call(self,request):
  action=request.get('action')
  if action in ('init','preflight'):
   description=self.host_call('describe')
   request=dict(request,job_isolation=description['job_isolation'])
  if action=='cancel':
   self.host_call('cancel',request['job_id'])
   return self._worker_call({'action':'status','job_id':request['job_id']})
  if action=='cancel_all':
   pending=self._worker_call({'action':'jobs','pending_only':True})['jobs']
   for job in pending:self.host_call('cancel',job['job_id'])
   return {'jobs':[self._worker_call({'action':'status','job_id':job['job_id']})for job in pending]}
  result=self._worker_call(request)
  if action=='submit':
   launched=self.host_call('launch',result['job_id'])
   if launched.get('job_id')!=result['job_id']:raise ConnectionError('job launcher returned a different job identity; outcome unknown')
   # Keep worker paths in the container namespace presented to model tools.
   result.update({key:launched[key]for key in ('ok','status')if key in launched})
  return result

 def _worker_call(self,request):
  r=self.environment['remote'];argv=['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o',f"ConnectTimeout={r['connect_timeout_seconds']}",'-o','ServerAliveInterval=10','-o','ServerAliveCountMax=3',r['ssh_host'],'docker','exec','-i','-w',self.project,r['container'],self.environment['eda']['python'],self.worker,'--root',self.project,'--run',self.run_id]
  index=argv.index(r['ssh_host'])+1;argv=argv[:index]+[shlex.join(argv[index:])]
  # Freezing and verifying complete build trees are synchronous and size-dependent.
  # SSH retains connection/liveness checks; submitted jobs retain their own timeout.
  # Diagnostic labels never copy arbitrary request data into controller reports.
  action=request.get('action')
  labels=('init','preflight','put_evidence','append_evidence','jobs','cancel_all','status','cancel','set_status','list','read','write','replace','submit','job_check','revision_begin','proposal','evaluate','revision_adopt','rtl_review_begin','rtl_review_status','rtl_review_decision','rtl_repair_begin','rtl_repair_end','rtl_session_begin','rtl_session_end')
  action=action if isinstance(action,str)and action in labels else'unknown'
  try:p=subprocess.run(argv,input=json.dumps(request),text=True,capture_output=True)
  except (OSError,subprocess.SubprocessError) as error:
   raise ConnectionError('remote transport failure; action='+str(action)+'; category='+type(error).__name__+'; outcome unknown')from None
  try:result=json.loads(p.stdout)
  except ValueError:raise ConnectionError('remote transport failure; action='+str(action)+'; exit='+str(p.returncode)+'; outcome unknown; provider/profile diagnostics not retained')from None
  if 'error'in result:raise RuntimeError(result['error'])
  if p.returncode:raise ConnectionError('remote transport failure; action='+str(action)+'; exit='+str(p.returncode)+'; outcome unknown')
  return result
 def evidence(self,path,content):return self.call({'action':'put_evidence','path':'control/'+path,'content':content})
