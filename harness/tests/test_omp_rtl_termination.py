import io,json,tempfile,unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch
from harness.adapters.omp_tools import run_session,_safe_assistant_error
class Remote:
 def evidence(self,name,content):
  if name.endswith('.prompt.txt'):self.prompt=content
 def call(self,req):
  if req['action']=='rtl_session_begin':
   label=req['session_id'];actor={'version':1,'run_id':'fixture','session_id':label,'label':label,'role':req['role'],'scope':req['scope'],'model_role':req['model_role'],'prompt_sha256':sha256(self.prompt.encode()).hexdigest()}
   return {'registered':True,'actor':actor,'actor_path':'control/sessions/'+label+'.json','actor_sha256':sha256((json.dumps(actor,ensure_ascii=False,indent=2)+'\n').encode()).hexdigest()}
class TerminationTests(unittest.TestCase):
 def test_trusted_actor_registered_before_model_and_completed_evidence_before_end(self):
  with tempfile.TemporaryDirectory()as name:
   p=Path(name);ctx=p/'context.json';ctx.write_text(json.dumps({'harness_dir':str(p),'run_id':'fixture','role':'architecture','scope':'architecture/revision-1/candidate','session_id':'inherited-parent'}));recorded=[]
   class RecordingRemote(Remote):
    def evidence(self,*args):super().evidence(*args);recorded.append(('evidence',args[0]))
    def call(self,value):recorded.append(('call',value.copy()));return super().call(value)
   class Process:
    def __init__(self,*a,**kw):
     recorded.append(('spawn',json.loads(Path(kw['env']['HARNESS_RTL_CONTEXT']).read_text())))
     self.stdin=io.StringIO();self.stdout=io.StringIO(json.dumps({'type':'message_end','message':{'role':'assistant','stopReason':'stop','content':[]}})+'\n'+json.dumps({'type':'agent_end'}))
     Path(kw['env']['HARNESS_RTL_TOOL_RECEIPT']).write_text(json.dumps({'checks':[{'active_tools':['rtl_remote','rtl_revise_architecture']}],'blocked':[]}))
    def wait(self):return 0
   with patch('harness.adapters.omp_tools.subprocess.Popen',Process):
    result=run_session('synthetic','implementation',{'roles':{'implementation':'fixture'},'runtime':{'command_template':['omp','{prompt}']}},p,ctx,p,RecordingRemote(),'actual-child')
   self.assertTrue(result['completed'])
   self.assertEqual(recorded[1],('evidence','actual-child.events.jsonl'))
   self.assertEqual(recorded[2],('call',{'action':'rtl_session_begin','session_id':'actual-child','role':'architecture','scope':'architecture/revision-1/candidate','model_role':'implementation'}))
   self.assertEqual(recorded[3][0],'spawn');self.assertEqual(recorded[3][1]['session_id'],'actual-child')
   self.assertEqual(recorded[3][1]['session_actor_sha256'],json.loads(ctx.read_text())['session_actor_sha256'])
   self.assertEqual(recorded[-2],('evidence','actual-child.result.json'))
   self.assertEqual(recorded[-1],('call',{'action':'rtl_session_end','session_id':'actual-child'}))
   events_index=recorded.index(('call',next(v for k,v in recorded if k=='call'and v['action']=='append_evidence')))
   self.assertLess(events_index,len(recorded)-2)
 def test_registration_failure_never_starts_model(self):
  class FailingRemote(Remote):
   def call(self,value):raise ConnectionError('registration unavailable')
  with tempfile.TemporaryDirectory()as name:
   p=Path(name);ctx=p/'context.json';ctx.write_text(json.dumps({'harness_dir':str(p),'run_id':'fixture','role':'implementation','scope':'work'}))
   with patch('harness.adapters.omp_tools.subprocess.Popen')as spawn:
    with self.assertRaises(ConnectionError):run_session('test','implementation',{'roles':{'implementation':'test'},'runtime':{'command_template':['omp','{prompt}']}},p,ctx,p,FailingRemote(),'actual-parent')
    spawn.assert_not_called()
 def test_mismatched_registered_actor_pin_stops_before_model(self):
  for field in ('actor_sha256','actor_path','role','run_id'):
   with self.subTest(field=field),tempfile.TemporaryDirectory()as name:
    p=Path(name);ctx=p/'context.json';ctx.write_text(json.dumps({'harness_dir':str(p),'run_id':'fixture','role':'implementation','scope':'work'}))
    class WrongRemote(Remote):
     def call(self,req):
      value=super().call(req)
      if req['action']=='rtl_session_begin':
       if field in ('role','run_id'):value['actor'][field]='other'
       else:value[field]='other'
      return value
    with patch('harness.adapters.omp_tools.subprocess.Popen')as spawn:
     with self.assertRaisesRegex(ValueError,'registered OMP session identity'):
      run_session('test','implementation',{'roles':{'implementation':'test'},'runtime':{'command_template':['omp','{prompt}']}},p,ctx,p,WrongRemote(),'actual-parent')
     spawn.assert_not_called()
 def test_missing_actor_context_or_unsafe_label_fails_before_rpc_and_model(self):
  for context,label in [({'harness_dir':'.'},'valid'),({'harness_dir':'.','run_id':'fixture','role':'implementation','scope':'work'},'../escape')]:
   with self.subTest(label=label),tempfile.TemporaryDirectory()as name:
    p=Path(name);ctx=p/'context.json';ctx.write_text(json.dumps(context))
    with patch('harness.adapters.omp_tools.subprocess.Popen')as spawn,patch.object(Remote,'call')as rpc:
     with self.assertRaises(ValueError):run_session('test','implementation',{'roles':{'implementation':'test'},'runtime':{'command_template':['omp','{prompt}']}},p,ctx,p,Remote(),label)
     spawn.assert_not_called();rpc.assert_not_called()
 def run_case(self,reason,ended):
  with tempfile.TemporaryDirectory()as name:
   p=Path(name);h=p/'harness';(h/'adapters').mkdir(parents=True);(h/'adapters/rtl_tools.js').touch();ctx=p/'context.json';ctx.write_text(json.dumps({'harness_dir':str(h),'run_id':'fixture','role':'implementation','scope':'work'}))
   events=[{'type':'message_end','message':{'role':'assistant','stopReason':reason,'content':[{'type':'text','text':'answer'}]}}]+([{'type':'agent_end'}]if ended else[])
   class Process:
    def __init__(self,*a,**kw):
     self.stdin=io.StringIO();self.stdout=io.StringIO('\n'.join(json.dumps(e)for e in events));receipt=Path(kw['env']['HARNESS_RTL_TOOL_RECEIPT']);receipt.write_text(json.dumps({'checks':[{'active_tools':['rtl_remote','rtl_revise_architecture']}],'blocked':[]}))
    def wait(self):return 0
   with patch('harness.adapters.omp_tools.subprocess.Popen',Process):return run_session('test','implementation',{'roles':{'implementation':'test'},'runtime':{'command_template':['omp','{prompt}']}},p,ctx,p,Remote(),'test')['completed']
 def test_only_terminal_stop_is_completion(self):
  self.assertTrue(self.run_case('stop',True))
  for reason,ended in [('error',True),('length',True),('aborted',True),('toolUse',True),('stop',False)]:self.assertFalse(self.run_case(reason,ended))
 def test_prompt_evidence_failure_never_starts_model(self):
  class FailingRemote(Remote):
   def evidence(self,*args):raise TimeoutError('transport')
  with tempfile.TemporaryDirectory()as name:
   p=Path(name);ctx=p/'context.json';ctx.write_text(json.dumps({'harness_dir':str(p),'run_id':'fixture','role':'implementation','scope':'work'}))
   with patch('harness.adapters.omp_tools.subprocess.Popen')as spawn:
    with self.assertRaises(TimeoutError):run_session('test','implementation',{'roles':{'implementation':'test'},'runtime':{'command_template':['omp','{prompt}']}},p,ctx,p,FailingRemote(),'test')
    spawn.assert_not_called()
 def test_broken_stdin_reaps_model_process_group(self):
  class BrokenInput:
   def write(self,value):raise BrokenPipeError('closed stdin')
  class Process:
   pid=12345;stdin=BrokenInput()
   def wait(self,timeout=None):return 0
  with tempfile.TemporaryDirectory()as name:
   p=Path(name);ctx=p/'context.json';ctx.write_text(json.dumps({'harness_dir':str(p),'run_id':'fixture','role':'implementation','scope':'work'}))
   with patch('harness.adapters.omp_tools.subprocess.Popen',return_value=Process()),patch('os.killpg')as kill:
    with self.assertRaises(BrokenPipeError):run_session('test','implementation',{'roles':{'implementation':'test'},'runtime':{'command_template':['omp','{prompt}']}},p,ctx,p,Remote(),'test')
    self.assertEqual(kill.call_args.args[0],12345)
 def test_error_categories_do_not_copy_provider_fields(self):
  for status,category in [(401,'authentication_or_permission'),(403,'authentication_or_permission'),(429,'rate_limited'),(408,'timeout'),(504,'timeout'),(503,'upstream_error'),(400,'request_error')]:
   self.assertEqual(_safe_assistant_error({'errorStatus':status,'errorMessage':'private provider text','provider':'private profile'}),{'category':category,'http_status':status})
  self.assertEqual(_safe_assistant_error({'errorMessage':'Anthropic stream stalled while waiting for the next event; private text'}),{'category':'stream_stalled'})
  for status in ('429 secret',True,999,None):
   self.assertEqual(_safe_assistant_error({'errorStatus':status,'errorMessage':'unrecognized private text','errorId':987654321}),{'category':'unknown'})
 def test_error_event_is_redacted_and_recovery_does_not_change_completion(self):
  secret='private-test-token-should-never-be-persisted'
  failure={'type':'message_end','message':{'role':'assistant','stopReason':'error','errorStatus':429,'errorMessage':secret,'errorId':123,'provider':secret,'api':secret,'content':[{'type':'text','text':secret}]}}
  normal={'type':'message_end','message':{'role':'assistant','stopReason':'stop','content':[{'type':'text','text':'answer'}]}}
  for recovered in (False,True):
   with tempfile.TemporaryDirectory()as name:
    p=Path(name);ctx=p/'context.json';ctx.write_text(json.dumps({'harness_dir':str(p),'run_id':'fixture','role':'implementation','scope':'work'}));recorded=[]
    events=[failure]+([normal]if recovered else[])+[{'type':'agent_end'}]
    class RecordingRemote(Remote):
     def evidence(self,*args):super().evidence(*args);recorded.append(args)
     def call(self,*args):recorded.append(args);return super().call(*args)
    class Process:
     def __init__(self,*a,**kw):self.stdin=io.StringIO();self.stdout=io.StringIO('\n'.join(json.dumps(e)for e in events))
     def wait(self):return 0
    with patch('harness.adapters.omp_tools.subprocess.Popen',Process)as spawn:
     result=run_session('test','implementation',{'roles':{'implementation':'test'},'runtime':{'command_template':['omp','{prompt}']}},p,ctx,p,RecordingRemote(),'test')
    self.assertEqual(result['completed'],recovered);self.assertEqual(result['assistant_turns'],2 if recovered else 1)
    self.assertEqual(result['final_text'],'answer'if recovered else'')
    if recovered:self.assertNotIn('assistant_error',result)
    else:self.assertEqual(result['assistant_error'],{'category':'rate_limited','http_status':429})
    persisted=json.dumps([recorded,result]);self.assertNotIn(secret,persisted);self.assertNotIn('errorMessage',persisted);self.assertNotIn('errorId',persisted)
