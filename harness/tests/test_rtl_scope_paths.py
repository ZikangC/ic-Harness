"""Scoped paths publish once at the canonical target, with no alias-write privilege."""
import json,unittest
from pathlib import Path
from unittest import mock
from harness.runtime.rtl_worker import dispatch,sha,scope_relative_path
from harness.tests import test_rtl_remote as fixture


class ScopePathTests(unittest.TestCase):
 setUp=fixture.WorkerTests.setUp
 tearDown=fixture.WorkerTests.tearDown
 call=fixture.WorkerTests.call

 def audit(self):
  return self.call(action='revision_begin',reason='synthetic scoped publication')['audit_scope']

 def test_current_scope_qualified_write_is_one_canonical_publication(self):
  scope=self.audit();name=scope+'/review.json'
  result=self.call(action='write',scope=scope,path=name,content='{"verdict":"revise"}\n',base_sha256=None)
  self.assertTrue((self.out/scope/'review.json').is_file())
  self.assertFalse((self.out/scope/scope/'review.json').exists())
  self.assertEqual({key:result[key]for key in ('path','scope','run_path')},{'path':'review.json','scope':scope,'run_path':name})
  self.assertEqual(self.call(action='read',scope=scope,path=name)['content'],'{"verdict":"revise"}\n')
  self.assertEqual(self.call(action='list',scope=scope,path=scope)['files'],[{'path':'review.json','bytes':21}])

 def test_qualified_replace_preserves_version_check(self):
  scope=self.audit();name=scope+'/review.json'
  first=self.call(action='write',scope=scope,path='review.json',content='original',base_sha256=None)
  with self.assertRaisesRegex(ValueError,'version changed'):
   self.call(action='replace',scope=scope,path=name,old='original',content='changed',base_sha256=None)
  replaced=self.call(action='replace',scope=scope,path=name,old='original',content='changed',base_sha256=first['sha256'])
  self.assertEqual(replaced['path'],'review.json');self.assertEqual((self.out/name).read_text(),'changed')

 def test_other_reserved_scopes_and_escapes_rejected(self):
  scope=self.audit()
  forbidden=['work/report.json','architecture/revision-2/audit/report.json','audit/review-9/report.json',
             'rtl/candidates/candidate-1/work/report.json','snapshot/input/spec.md','../candidate/design.json',
             scope+'/../candidate/design.json',str(self.out/scope/'absolute.json')]
  for name in forbidden:
   with self.subTest(path=name),self.assertRaises(ValueError):
    self.call(action='write',scope=scope,path=name,content='bad',base_sha256=None)
  self.assertEqual(list((self.out/scope).iterdir()),[])
  with self.assertRaisesRegex(ValueError,'outside current scope'):
   self.call(action='list',scope=scope,path='work')

 def test_all_aliases_remain_read_only_for_writes(self):
  scope=self.audit()
  for name in ['@run/'+scope+'/review.json','@input/spec.md','@architecture/architecture.md','@skill/rtl-implementation/SKILL.md','@candidate/design.json']:
   with self.subTest(path=name),self.assertRaisesRegex(ValueError,'aliases are read-only'):
    self.call(action='write',scope=scope,path=name,content='bad',base_sha256=None)
  self.assertEqual(self.call(action='read',scope=scope,path='@input/spec.md')['content'],'Test specification')

 def test_normal_scope_relative_names_are_unchanged(self):
  result=self.call(action='write',path='rtl/block.sv',content='module block; endmodule\n',base_sha256=None)
  self.assertEqual(result['path'],'rtl/block.sv');self.assertEqual(result['run_path'],'work/rtl/block.sv')
  self.assertEqual(scope_relative_path('work','workbench/source.sv'),'workbench/source.sv')

 def test_explicit_local_reserved_directory_is_reusable_without_rebinding(self):
  scope=self.audit()
  with self.assertRaisesRegex(ValueError,'outside current scope'):
   self.call(action='write',scope=scope,path='work/report.json',content='wrong target',base_sha256=None)
  result=self.call(action='write',scope=scope,path='./work/report.json',content='local directory',base_sha256=None)
  self.assertEqual(result['path'],'./work/report.json');self.assertEqual(result['run_path'],scope+'/work/report.json')
  self.assertEqual(self.call(action='read',scope=scope,path=result['path'])['content'],'local directory')
  self.assertEqual(self.call(action='read',scope=scope,path='@run/'+result['run_path'])['content'],'local directory')

 def test_repeated_or_nested_qualified_prefixes_rejected_without_side_effects(self):
  scope=self.audit();before_jobs=list((self.out/'jobs').glob('*'))
  for name in [scope+'/'+scope+'/review.json',scope+'/work/report.json']:
   with self.subTest(path=name):
    with self.assertRaisesRegex(ValueError,'repeated or nested'):
     self.call(action='write',scope=scope,path=name,content='bad',base_sha256=None)
    with self.assertRaisesRegex(ValueError,'repeated or nested'):
     self.call(action='list',scope=scope,path=name)
    with self.assertRaisesRegex(ValueError,'repeated or nested'):
     dispatch(self.root,self.runid,{'action':'submit','scope':scope,'argv':['python','probe.py'],'cwd':name})
  self.assertEqual(list((self.out/scope).iterdir()),[]);self.assertEqual(list((self.out/'jobs').glob('*')),before_jobs)

 def test_list_paths_roundtrip_literal_reserved_and_current_scope_subtrees(self):
  scope=self.audit();outside=self.out/'work/report.json';outside.write_text('outside audit sentinel')
  values={'./work/report.json':'local work directory','./'+scope+'/report.json':'local current-scope name'}
  for name,content in values.items():self.call(action='write',scope=scope,path=name,content=content,base_sha256=None)
  original_read=Path.read_bytes
  def guarded_read(path):
   if path==outside:raise AssertionError('list locator rebound outside the audit scope')
   return original_read(path)
  with mock.patch.object(Path,'read_bytes',guarded_read):
   listed=self.call(action='list',scope=scope)['files'];self.assertEqual({row['path']for row in listed},set(values))
   for row in listed:self.assertEqual(self.call(action='read',scope=scope,path=row['path'])['content'],values[row['path']])
  self.assertEqual(outside.read_text(),'outside audit sentinel')

 def test_qualified_symlink_escape_still_rejected(self):
  scope=self.audit();target=self.out/'snapshot/input/spec.md';before=target.read_bytes()
  (self.out/scope/'link').symlink_to(target)
  with self.assertRaises(ValueError):self.call(action='write',scope=scope,path=scope+'/link',content='bad',base_sha256=sha(target))
  self.assertEqual(target.read_bytes(),before)

 def test_submit_cwd_normalizes_but_ordinary_argv_is_literal(self):
  scope=self.audit();self.call(action='write',scope=scope,path='sub/probe.py',content='pass\n',base_sha256=None)
  argv=['python',scope+'/sub/probe.py','unchanged-argument']
  # Queue metadata only: no host launcher or process is called by this fixture.
  result=dispatch(self.root,self.runid,{'action':'submit','scope':scope,'argv':argv,'cwd':scope+'/sub','timeout_seconds':1})
  request=json.loads((self.out/'jobs'/result['job_id']/'request.json').read_text())
  self.assertEqual(request['cwd'],str(self.out/scope/'sub'));self.assertEqual(request['argv'][1:],argv[1:])
  with self.assertRaisesRegex(ValueError,'outside current scope'):
   dispatch(self.root,self.runid,{'action':'submit','scope':scope,'argv':['python','probe.py'],'cwd':'work'})


if __name__=='__main__':unittest.main()
