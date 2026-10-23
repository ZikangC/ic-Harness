"""Explicit multi-file inputs keep one frozen source closure through audit."""
import copy
import json
from pathlib import Path
import py_compile
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from harness.runtime import audit_session as session, run_store as store, source_evidence as evidence
from harness.runtime.input_bundle import load_input_bundle, original_sources, is_original_source


class InputBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='input-bundle-test-');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.input=self.root/'input';self.input.mkdir()
        self.write('spec.md','whole task budget 3\n')
        self.write('reference.py',"raise RuntimeError('reference must never execute')\n")

    def write(self,name,text):
        path=self.input/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(text.encode('utf-8'))

    def manifest(self,**changes):
        data={'schema_version':1,'spec':'spec.md','reference':'chip/model/nano.py',
              'support':['chip/__init__.py','chip/model/__init__.py','chip/model/params.py','chip/cfg/nano.yaml']}
        data.update(changes);self.write('sources.json',json.dumps(data,indent=2)+'\n');return data

    def package(self):
        self.write('chip/model/nano.py',"from .params import WIDTH\nraise RuntimeError('not executed')\n")
        self.write('chip/model/params.py','WIDTH = 80\n')
        self.write('chip/cfg/nano.yaml','width: 80\n')
        self.write('chip/__init__.py','');self.write('chip/model/__init__.py','')
        self.manifest()

    def freeze(self,files):
        out=self.root/'run';out.mkdir()
        for name,data in files.items():
            path=out/'snapshot'/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
        store.dump(out/'manifest.json',{'sha256':{name:store.digest(data) for name,data in files.items()}})
        return out

    def test_default_two_file_input_preserves_legacy_fields_without_execution(self):
        sources,files,info=load_input_bundle(self.input)
        self.assertEqual(set(sources),{'spec.md','reference.py'})
        self.assertEqual(info['source_paths'],{'spec.md':'spec.md','reference.py':'reference.py'})
        self.assertGreater(info['python_ast_nodes'],0);self.assertNotIn('input/sources.json',files)
        self.assertEqual(files['input/reference.py'],(self.input/'reference.py').read_bytes())

    def test_explicit_package_preserves_paths_empty_files_and_raw_manifest(self):
        self.package();self.write('unlisted.py','syntax error !\n')
        self.write('chip/cfg/nano.yaml','width: 80\r\n')
        originals={str(path.relative_to(self.input)):path.read_bytes() for path in self.input.rglob('*') if path.is_file()}
        sources,files,info=load_input_bundle(self.input)
        self.assertEqual(sources['reference.py'],sources['support/chip/model/nano.py'])
        self.assertEqual(info['source_paths']['reference.py'],'chip/model/nano.py')
        self.assertEqual(info['source_paths']['support/chip/model/nano.py'],'chip/model/nano.py')
        self.assertEqual(sources['support/chip/model/__init__.py'],'')
        self.assertEqual(set(info['python_ast_nodes_by_source']),
                         {'reference.py','support/chip/model/nano.py','support/chip/__init__.py',
                          'support/chip/model/__init__.py','support/chip/model/params.py'})
        self.assertEqual(files['input/sources.json'],originals['sources.json'])
        self.assertEqual(files['input/support/chip/cfg/nano.yaml'],originals['chip/cfg/nano.yaml'])
        self.assertFalse(any('unlisted' in name for name in files))
        self.assertEqual(originals,{str(path.relative_to(self.input)):path.read_bytes() for path in self.input.rglob('*') if path.is_file()})
        self.write('chip/model/params.py','def broken(:\n')
        with self.assertRaises(SyntaxError):load_input_bundle(self.input)

    def test_manifest_rejects_unsafe_duplicate_missing_nontext_and_escaping_paths(self):
        self.package()
        for name in ('../outside.py','/tmp/outside.py','chip//model/x.py','chip/./x.py','chip/../x.py',
                     'chip\\x.py','C:/x.py','chip/:x.py','chip/\nx.py','','chip/'):
            with self.subTest(name=name):
                self.manifest(support=[name])
                with self.assertRaises(ValueError):load_input_bundle(self.input)
        for changes in ({'schema_version':True},{'schema_version':2},{'extra':1},
                        {'support':['chip/model/params.py','chip/model/params.py']},
                        {'spec':'sources.json'},{'reference':'sources.json'},{'support':['sources.json']},
                        {'support':['missing.py']}):
            with self.subTest(changes=changes):
                self.manifest(**changes)
                with self.assertRaises(ValueError):load_input_bundle(self.input)
        self.write('binary.txt','text');(self.input/'binary.txt').write_bytes(b'\xff')
        self.manifest(support=['binary.txt'])
        with self.assertRaises(UnicodeDecodeError):load_input_bundle(self.input)
        outside=self.root/'outside.py';outside.write_text('WIDTH=80\n')
        (self.input/'escape.py').symlink_to(outside);self.manifest(support=['escape.py'])
        with self.assertRaisesRegex(ValueError,'escapes input directory'):load_input_bundle(self.input)
        (self.input/'manifest-alias.json').symlink_to(self.input/'sources.json')
        self.manifest(support=['manifest-alias.json'])
        with self.assertRaisesRegex(ValueError,'cannot be an input source'):load_input_bundle(self.input)
        self.write('sources.json','{"schema_version":1,"schema_version":1,"spec":"spec.md","reference":"reference.py","support":[]}')
        with self.assertRaisesRegex(ValueError,'duplicate input manifest field'):load_input_bundle(self.input)

    def test_original_classification_keeps_program_evidence_out_of_input_scope(self):
        self.package();sources,_,_=load_input_bundle(self.input)
        sources['design.json']='proposal';sources['budget.json']='computed';sources['architecture.md']='document'
        self.assertEqual(set(original_sources(sources)),set(sources)-{'design.json','budget.json','architecture.md'})
        for name in ('support/../budget.json','support//x.py','support/x\\y.py','support/C:/x.py','support','support/'):
            self.assertFalse(is_original_source(name))
            with self.assertRaises(ValueError):original_sources(dict(sources,**{name:'not input'}))
        self.assertFalse(is_original_source('design.json'));self.assertTrue(is_original_source('support/design.json'))
        with self.assertRaises(ValueError):original_sources(sources,strict=True)
        record=evidence.catalogue(sources,input_only=True)
        self.assertEqual(set(record['source_sha256']),set(original_sources(sources)))
        span={'file':'support/chip/model/params.py','start':1,'end':1}
        self.assertEqual(evidence.selected_span(span,sources,input_only=True)['quote'],'WIDTH = 80')
        with self.assertRaises(ValueError):evidence.selected_span(dict(span,file='budget.json'),sources,input_only=True)

    def test_complete_source_hashes_bind_transport_replay_and_reject_omission_or_tampering(self):
        self.package();sources,files,_=load_input_bundle(self.input);out=self.freeze(files)
        context=session.response_context(out,sources,'baseline')
        self.assertEqual(context['input_sha256'],{name:store.digest(text.encode()) for name,text in sources.items()})
        provider=Mock(side_effect=lambda name,role,prompt,parser:parser('{"scope":"synthetic"}'))
        session.bound_response(out,'bound',provider,json.loads,context,'explicit source closure')
        replay=Mock(side_effect=AssertionError('must replay the bound response'))
        self.assertEqual(session.bound_response(out,'bound',replay,json.loads,context,''),{'scope':'synthetic'})
        replay.assert_not_called()
        helper='support/chip/model/params.py'
        for changed in (dict(sources,**{helper:'WIDTH = 81\n'}),{k:v for k,v in sources.items() if k!=helper},
                        dict(sources,**{'support/extra.py':'WIDTH=80\n'})):
            with self.assertRaises(session.TransportBindingError):session.frozen_transport_context(out,changed)
        path=out/'snapshot/input'/helper;before=path.read_bytes();path.write_text('WIDTH = 81\n')
        with self.assertRaises(session.TransportBindingError):session.frozen_transport_context(out,sources)
        path.write_bytes(before);self.assertEqual(session.frozen_transport_context(out,sources),
                                                 {k:v for k,v in context.items() if k in ('input_sha256','manifest_sha256','snapshot_sha256')})

    def test_declared_cache_like_support_paths_are_frozen_and_fully_bound(self):
        self.package()
        special=['__pycache__/notes.txt','notes.pyc','.DS_Store']
        for name in special:self.write(name,'explicit UTF-8 support text\n')
        declaration=json.loads((self.input/'sources.json').read_text())
        self.manifest(support=declaration['support']+special)
        sources,files,_=load_input_bundle(self.input)
        files['harness/__pycache__/declared.txt']=b'explicit manifest entry'
        files['harness/declared.pyc']=b'explicit manifest entry'
        files['harness/.DS_Store']=b'explicit manifest entry'
        out=self.freeze(files);context=session.frozen_transport_context(out,sources)
        for name in special:self.assertIn('support/'+name,context['input_sha256'])
        for name in ('input/support/notes.pyc','harness/declared.pyc','harness/__pycache__/declared.txt','harness/.DS_Store'):
            path=out/'snapshot'/name;before=path.read_bytes();path.write_bytes(b'changed')
            with self.subTest(name=name),self.assertRaises(session.TransportBindingError):
                session.frozen_transport_context(out,sources)
            path.write_bytes(before)
        self.assertEqual(session.frozen_transport_context(out,sources),context)

    def test_undeclared_input_cache_names_are_rejected_but_harness_bytecode_is_ignored(self):
        self.package();sources,files,_=load_input_bundle(self.input)
        files['harness/module.py']=b'VALUE = 1\n'
        out=self.freeze(files);context=session.frozen_transport_context(out,sources)
        module=out/'snapshot/harness/module.py'
        py_compile.compile(str(module),doraise=True)
        self.assertTrue(list((module.parent/'__pycache__').glob('*.pyc')))
        self.assertEqual(session.frozen_transport_context(out,sources),context)
        for name in ('input/__pycache__/hidden.pyc','input/support/hidden.pyc','input/.DS_Store',
                     'harness/__pycache__/notes.txt','harness/__pycache__/fake.pyc'):
            path=out/'snapshot'/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('unlisted text')
            with self.subTest(name=name),self.assertRaises(session.TransportBindingError):
                session.frozen_transport_context(out,sources)
            path.unlink()
        self.assertEqual(session.frozen_transport_context(out,sources),context)

    def test_audit_design_origin_requires_the_same_complete_input_closure(self):
        self.package();self.write('chip/model/unused_helper.py','UNUSED = 1\n')
        declaration=json.loads((self.input/'sources.json').read_text())
        self.manifest(support=declaration['support']+['chip/model/unused_helper.py'])
        sources,files,_=load_input_bundle(self.input)
        config=self.root/'config';config.mkdir()
        (config/'environment.toml').write_text('[project]\nroot=".."\ninput_dir="input"\noutput_dir="output"\n')
        (config/'models.toml').write_text('[roles]\narchitecture="synthetic"\n')
        (config/'architecture.toml').write_text('schema_version=1\n')
        origin=self.root/'accepted';origin.mkdir();proposal=origin/'design.json'
        proposal.write_text(json.dumps({'architecture_md':'synthetic diagnostic proposal'}))
        hashes={name:store.digest(data) for name,data in files.items()}
        store.dump(origin/'manifest.json',{'sha256':hashes})
        args=SimpleNamespace(config=config/'environment.toml',feedback=None,audit_design=proposal,run_id='audit',resume=None)
        with patch.object(store,'verify_skills'):loaded=store.load(args)
        self.assertEqual(loaded[3],sources);self.assertEqual(loaded[4]['audit/design.json'],proposal.read_bytes())
        declaration=json.loads((self.input/'sources.json').read_text())
        self.manifest(support=[name for name in declaration['support'] if name!='chip/model/unused_helper.py'])
        with patch.object(store,'verify_skills'),self.assertRaisesRegex(ValueError,'originating inputs differ'):
            store.load(args)
        # An unknown origin retains the existing standalone diagnostic behavior.
        (origin/'manifest.json').unlink()
        with patch.object(store,'verify_skills'):unbound=store.load(args)
        self.assertNotIn('support/chip/model/unused_helper.py',unbound[3])

    def test_run_input_check_and_resume_bind_manifest_mapping_and_every_helper(self):
        self.package();config=self.root/'config';config.mkdir()
        (config/'environment.toml').write_text('[project]\nroot=".."\ninput_dir="input"\noutput_dir="output"\n')
        (config/'models.toml').write_text('[roles]\narchitecture="synthetic"\n')
        (config/'architecture.toml').write_text('schema_version=1\n')
        args=SimpleNamespace(config=config/'environment.toml',feedback=None,audit_design=None,run_id='bundle',resume=None)
        with patch.object(store,'verify_skills'):
            root,models,settings,sources,files,hashes,check=store.load(args)
        self.assertEqual(set(check['source_paths']),set(sources));self.assertEqual(set(check['input_sha256']),set(sources))
        self.assertEqual(check['reference_execution'],'not_run')
        out,lock=store.open_run(args,root,models,settings,files,hashes,check);store.close_run(out,lock,{'status':'test'})
        args.run_id=None;args.resume='bundle'
        helper=out/'snapshot/input/support/chip/model/params.py';before=helper.read_bytes();helper.write_text('WIDTH=81\n')
        with self.assertRaisesRegex(ValueError,'snapshot changed'):store.open_run(args,root,models,settings,files,hashes,check)
        helper.write_bytes(before)
        resumed,lock=store.open_run(args,root,models,settings,files,hashes,check);store.close_run(resumed,lock,{'status':'test'})
        self.write('chip/model/params.py','WIDTH = 81\n')
        with patch.object(store,'verify_skills'):changed=store.load(args)
        with self.assertRaisesRegex(ValueError,'inputs/config/code/skill changed'):
            store.open_run(args,*changed[:3],changed[4],changed[5],changed[6])


class MultiFileAuditFlowTests(unittest.TestCase):
    def test_model_baseline_quantities_local_checks_and_review_share_all_input_sources(self):
        import test_audit as fixtures
        document,sources=fixtures.fixture();sources=dict(sources,**{'support/chip/model/params.py':'WIDTH = 80\n',
            'support/chip/model/reference_v2.py':sources['reference.py'],'support/chip/__init__.py':''})
        baseline=fixtures.baseline_fixture(sources);helper={'file':'support/chip/model/params.py','start':1,'end':1}
        baseline['analysis']['facts']=[{'id':'helper_width','source':{'file':helper['file'],'line':1,'number':1},'unit':'count'}]
        baseline['analysis']['computations'][0]['steps'][0]['shape']=['helper_width']
        baseline['analysis']['computations'][0]['references'].append(helper)
        temp=tempfile.TemporaryDirectory(prefix='input-bundle-audit-');self.addCleanup(temp.cleanup);out=Path(temp.name)
        for name in ('microarchitecture-design','microarchitecture-audit'):
            shutil.copytree(fixtures.BASE/'skills'/name,out/'snapshot/harness/skills'/name,ignore=shutil.ignore_patterns('__pycache__','.DS_Store'))
        for name,text in sources.items():
            path=out/'snapshot/input'/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text)
        store.dump(out/'manifest.json',{'sha256':{str(p.relative_to(out/'snapshot')):store.digest(p.read_bytes())
            for p in (out/'snapshot').rglob('*') if p.is_file()}})
        def baseline_reply(name,role,prompt,parser):
            self.assertIn('FILE support/chip/model/params.py',prompt)
            return fixtures.wire_reply(baseline,parser)
        record=session.prepare_baseline(out,'source',sources,baseline_reply,0)
        self.assertTrue(record['source_execution']['coverage_complete'])
        self.assertEqual(record['analysis_result']['fact_values']['helper_width'],80)
        for path in out.glob('source*.request.json'):
            request=json.loads(path.read_text())
            if 'sources' in request:self.assertEqual(request['sources'],sources)
        base=out/'snapshot/harness/skills/microarchitecture-audit'
        self.assertNotEqual(session.baseline_identity(base,sources),session.baseline_identity(base,dict(sources,**{helper['file']:'WIDTH=81\n'})))
        budget=fixtures.engine.evaluate(document,sources,True)
        def review_reply(name,role,prompt,parser):
            self.assertIn('FILE support/chip/model/params.py',prompt)
            if name.endswith('step-0'):return fixtures.wire_reply({'tool_request':fixtures.proposal_request()},parser)
            context=json.loads((out/'review.packet.json').read_text())
            receipt=json.loads((out/'review.step-0.tool.result.json').read_text());receipt['request_id']='review.step-0'
            context['calculations']=[receipt];context['proposal_checks']=[receipt]
            return fixtures.wire_reply({'final':fixtures.report_content_fixture(context)},parser)
        report,checked=session.run(out,'review',sources,document,budget,review_reply,1,prepared_baseline=record)
        self.assertTrue(checked['ok'],checked['errors']);self.assertEqual(report['verdict'],'accept')
        quantity=json.loads((out/'review.quantities.request.json').read_text());self.assertEqual(quantity['sources'],sources)
        local=json.loads((out/'review.step-0.tool.request.json').read_text())
        self.assertEqual(original_sources(local['sources']),sources)
        frame=json.loads((out/'review.step-1.binding.json').read_text())['context']
        self.assertEqual(set(frame['input_sha256']),set(sources))
        cited=evidence.catalogue(sources,input_only=True)['citations']
        selected=next(row for row in cited if row['file']==helper['file'])
        request={'reference_updates':[{'obligation_id':'independent-work','reference_index':0,'evidence_id':selected['evidence_id']}]}
        repaired=session.repair_baseline(baseline,request,cited,sources)
        self.assertEqual(repaired['obligations'][0]['references'][0],{'evidence_id':selected['evidence_id']})


if __name__=='__main__':unittest.main()
