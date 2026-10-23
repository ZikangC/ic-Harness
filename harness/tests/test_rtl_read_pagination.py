"""Lossless bounded read pages, including long Unicode lines and stale cursors."""
import base64,hashlib,json,unittest
from harness.tests import test_rtl_remote as fixture


class ReadPaginationTests(unittest.TestCase):
 setUp=fixture.WorkerTests.setUp
 tearDown=fixture.WorkerTests.tearDown
 call=fixture.WorkerTests.call

 def put(self,name,content):
  self.call(action='write',path=name,content=content,base_sha256=None)

 def pages(self,name,**options):
  page=self.call(action='read',path=name,**options);found=[]
  while True:
   found.append(page)
   self.assertLessEqual(len(json.dumps(page,ensure_ascii=False).encode('utf-8')),24000)
   if page['next_cursor']is None:break
   self.assertFalse(page['eof'])
   following=self.call(action='read',path=name,cursor=page['next_cursor'],lines=options.get('lines',120))
   self.assertEqual(page['returned_range']['byte_end'],following['returned_range']['byte_start'])
   self.assertEqual(page['returned_range']['end'],following['returned_range']['start'])
   self.assertGreater(following['returned_range']['byte_end'],following['returned_range']['byte_start'])
   self.assertEqual(page['sha256'],following['sha256']);self.assertEqual(page['source_id'],following['source_id'])
   page=following
  self.assertTrue(page['eof'])
  return found

 def token_with(self,token,**changes):
  decoded=json.loads(base64.urlsafe_b64decode(token+'='*(-len(token)%4)));decoded.update(changes)
  return base64.urlsafe_b64encode(json.dumps(decoded).encode()).decode().rstrip('=')

 def test_long_single_unicode_line_roundtrips_exact_original_bytes(self):
  content='前缀：'+('中🙂e\u0301\\\"'*18000)+'尾部'
  self.put('long.json',content)
  pages=self.pages('long.json',lines=1)
  self.assertGreater(len(pages),2);self.assertTrue(pages[0]['truncated'])
  self.assertTrue(all(p['returned_range']['start']['line']==1 for p in pages))
  self.assertGreater(pages[1]['returned_range']['start']['column'],1)
  self.assertEqual(''.join(p['content']for p in pages).encode('utf-8'),content.encode('utf-8'))
  self.assertEqual(pages[-1]['sha256'],hashlib.sha256(content.encode('utf-8')).hexdigest())

 def test_multiline_pages_preserve_crlf_blank_lines_and_final_newline(self):
  content='header\r\n\r\n'+('界🙂'*10000)+'\r\n'+'\n'.join(f'row {n} ✓'for n in range(360))+'\n'
  self.put('architecture.md',content)
  pages=self.pages('architecture.md',lines=2)
  self.assertEqual(''.join(p['content']for p in pages),content)
  self.assertFalse(pages[0]['truncated']);self.assertFalse(pages[0]['eof'])
  self.assertEqual(pages[0]['text'],'1: header\n2: ')
  self.assertEqual(pages[-1]['returned_range']['end'],{'line':len(content.splitlines())+1,'column':1})

 def test_ordinary_numbered_read_is_backward_compatible(self):
  self.put('small.json','{\n  "receipt": true,\n  "note": "ok"\n}\n')
  page=self.call(action='read',path='small.json',start=1,lines=300)
  self.assertEqual(page['text'],'1: {\n2:   "receipt": true,\n3:   "note": "ok"\n4: }')
  self.assertEqual(json.loads('\n'.join(line.split(': ',1)[1]for line in page['text'].splitlines())),{'receipt':True,'note':'ok'})
  self.assertEqual(page['content'],(self.out/'work/small.json').read_text())
  self.assertTrue(page['eof']);self.assertFalse(page['truncated']);self.assertIsNone(page['next_cursor'])
  middle=self.call(action='read',path='small.json',start=2,lines=1)
  self.assertEqual(middle['text'],'2:   "receipt": true,')
  self.assertEqual(middle['content'],'  "receipt": true,\n')
  self.assertEqual(middle['returned_range']['start'],{'line':2,'column':1})

 def test_cursor_rejects_changed_source(self):
  self.put('changing.txt','x'*40000)
  token=self.call(action='read',path='changing.txt')['next_cursor']
  (self.out/'work/changing.txt').write_text('y'*40000)
  with self.assertRaisesRegex(ValueError,'stale'):self.call(action='read',path='changing.txt',cursor=token)

 def test_cursor_rejects_path_alias_scope_and_run_rebinding(self):
  self.put('one.txt','x'*40000);self.put('two.txt','x'*40000)
  token=self.call(action='read',path='one.txt')['next_cursor']
  for request in ({'path':'two.txt'},{'path':'@run/work/one.txt'},{'path':'@run/work/one.txt','scope':'architecture/revision-1/audit'}):
   with self.subTest(request=request),self.assertRaisesRegex(ValueError,'bound'):
    self.call(action='read',cursor=token,**request)
  original=self.runid;self.runid='second-run'
  from harness.runtime.rtl_worker import dispatch
  dispatch(self.root,self.runid,self.req)
  self.call(action='write',path='one.txt',content='x'*40000,base_sha256=None)
  with self.assertRaisesRegex(ValueError,'bound'):self.call(action='read',path='one.txt',cursor=token)
  self.runid=original

 def test_cursor_rejects_mid_utf8_offset_malformed_and_ambiguous_start(self):
  self.put('unicode.txt','🙂'*20000)
  token=self.call(action='read',path='unicode.txt')['next_cursor']
  invalid=['not-base64',self.token_with(token,byte_offset=1),self.token_with(token,byte_offset=-1),
           self.token_with(token,byte_offset=80001),self.token_with(token,byte_offset=True),
           self.token_with(token,source_id='different'),self.token_with(token,sha256='different')]
  for cursor in invalid:
   with self.subTest(cursor=cursor),self.assertRaisesRegex(ValueError,'cursor'):
    self.call(action='read',path='unicode.txt',cursor=cursor)
  with self.assertRaisesRegex(ValueError,'combined with start'):
   self.call(action='read',path='unicode.txt',cursor=token,start=2)

 def test_empty_and_past_eof_reads_terminate(self):
  self.put('empty.txt','');self.put('short.txt','one\ntwo')
  for name,options in [('empty.txt',{}),('short.txt',{'start':999})]:
   with self.subTest(name=name):
    page=self.call(action='read',path=name,**options)
    self.assertEqual(page['content'],'');self.assertEqual(page['text'],'')
    self.assertTrue(page['eof']);self.assertFalse(page['truncated']);self.assertIsNone(page['next_cursor'])
  with self.assertRaisesRegex(ValueError,'positive'):self.call(action='read',path='short.txt',lines=0)


if __name__=='__main__':unittest.main()
