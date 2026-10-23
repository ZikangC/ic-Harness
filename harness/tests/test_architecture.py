import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from harness.checks import architecture as a
from harness.stages import architecture as controller
from harness.prompts import architecture as prompts
from harness.runtime import run_store as store


def fixture():
    source='area 3; capacity 1000; clock 80000000; items 12; rate 480000; depth 2; width 8'
    facts=[{'id':name,'value':value,'unit':('Hz' if name=='clock' else 'fixture'),'citations':[{'file':'spec.md','start':1,'end':1,'quote':source}]}
           for name,value,quote in [('area_limit',3,'area 3'),('capacity_limit',1000,'capacity 1000'),('clock',80000000,'clock 80000000'),('items',12,'items 12'),('rate',480000,'rate 480000'),('depth_min',2,'depth 2'),('width_unit',8,'width 8')]]
    req={'facts':facts,'constraints':[
        {'id':'area','limit':'area_limit','kind':'hard','relation':'<=','unit':'mm2','verification':'estimate','quantity':'area'},
        {'id':'capacity','limit':'capacity_limit','kind':'hard','relation':'<=','unit':'bits','verification':'estimate','quantity':'capacity'},
        {'id':'throughput','limit':'rate','kind':'target','relation':'>=','unit':'item/s','quantity':'items_per_second','workload':'primary'}],
        'runtime_parameters':[],'open_conditions':[],'storage_area_model':{'status':'unspecified','reason':'synthetic input provides no density'},'storage_rules':[{'kind':'SRAM','min_depth':'depth_min','width_multiple':'width_unit','read_ports':'1','write_ports':'1','read_latency_cycles':'1'}]}
    c={'id':'small','assumptions':[{'id':'logic','value':2,'unit':'mm2','basis':'fixture only'}],
       'calculations':[],
       'numeric_probes':[{'id':'exact','operation':'identity','input':1,'format':{'bits':8,'fractional_bits':0,'signed':True},'rounding':'nearest_even','overflow':'reject','assertions':[{'metric':'integer','relation':'==','expected':1}],'basis':'synthetic exact integer boundary'}],
       'macros':[{'name':'banks','kind':'SRAM','count':'2','depth':'16','width':'8','ports_and_schedule':'fixture'}],
       'metrics':{},
       'workloads':[{'id':'primary','clock_fact':'clock','components':[{'id':'setup','composition':'serial','references':['setup'],'basis':'fixture'},{'id':'compute','composition':'serial','references':['compute'],'basis':'fixture'},{'id':'output','composition':'serial','references':['output'],'basis':'fixture'}]}],
       'resource_checks':[{'id':'weights','demand_per_cycle':'8','supply_per_cycle':'8','unit':'bits/cycle','basis':'fixture'}],
       'abi_fields':[{'name':'address','width':'24','offset':'8','carrier_width':'32'}]}
    c.update({
       'coverage':{k:{'status':'covered','basis':'isolated synthetic test model'} for k in ('cost_groups','traffic','buffers','schedule_windows','area_components')},
       'cost_groups':[{'id':'compute','model':'compute','workload':'primary','work_ids':['arithmetic'],'resources':['weights'],'units':'1','operations_per_unit_cycle':'1','overhead_cycles':'0','basis':'fixture'},
          {'id':'setup','model':'cycles','workload':'primary','work_ids':[],'resources':['weights'],'cycles':20,'basis':'fixture setup'},
          {'id':'output','model':'cycles','workload':'primary','work_ids':[],'resources':['weights'],'cycles':20,'basis':'fixture output'}],
       'traffic':[{'id':'reads','model':'continuous_reserved','payload_bytes':'8','transfers':'8','alignment_bytes':'8','transaction_bytes':'8','latency_cycles':'8','transactions_per_cycle':'1','inflight':'8','response_buffer_bytes':'64','basis':'fixture'}],
       'buffers':[{'id':'scratch','count':'2','depth':'16','width':'8','objects':[{'id':'vector','shape':[16],'element_bits':8,'copies':1,'alignment_bits':8,'metadata_bits':0,'live_in':['run'],'basis':'16 element synthetic vector'}],'kind':'SRAM','storage_reference':'banks','basis':'fixture'}],
       'storage_phases':[{'id':'run','basis':'all synthetic live work'}],
       'schedule_windows':[{'id':'load','workload':'primary','cycles':8,'repetition_basis':'seven read slots followed by consumption; toy task witness only','issues':[{'cost_group':'compute','cycle':1,'lanes':[0],'operands':['weight'],'basis':'one-lane fixture issue'}],'reads':[{'id':'r'+str(i),'storage_reference':'banks','instance':0,'word_address':i,'cycle':i} for i in range(7)],'writes':[],'uses':[{'cost_group':'compute','read_id':'r'+str(i),'cycle':i+1,'lane':0,'operand':'weight','bit_offset':0,'width':8} for i in range(7)],'basis':'fixture finite synchronous reads'}],
       'area_components':[{'id':'logic','kind':'logic','count':'1','unit_area_lower':'logic','unit_area_upper':'logic','source':'synthetic fixture, not measured','uncertainty':'fixture exact','basis':'fixture'},{'id':'macro','kind':'macro','resource_reference':'banks','unit_area_lower':'16*8/1024','unit_area_upper':'16*8/1024','source':'synthetic fixture','uncertainty':'fixture exact','basis':'fixture'}]})
    c['abi_fields'][0]['carrier']='command-address'
    big=copy.deepcopy(c);big['id']='large';big['assumptions'][0]['value']=4
    work_model={'workloads':[{'id':'primary','quantity':'items','unit':'samples','basis':'synthetic cost profile'}],'items':[{'id':'arithmetic','category':'compute','semantics':'synthetic work declared for mechanical cost tests','condition':'always','state_lifetime':'task-local','status':'complete','dependencies':[],'count':{'kind':'custom','operations':80,'unit':'ops','basis':'known synthetic fixture, not a real design'},'scopes':{'primary':{'kind':'task','axis':'run'}},'references':[{'file':'spec.md','quote':'items 12'}]}]}
    return {'requirements':req,'work_model':work_model,'candidates':[c,big],'selected':'small','architecture_md':('document content '*20).strip()},{'spec.md':source,'reference.py':'def unrelated(x): return x\n'}


def requirements_declaration(requirements):
    # Model ingress now selects synthetic source tokens. Canonical fixture
    # values remain available for isolated calculator/controller assertions.
    raw=copy.deepcopy(requirements)
    raw['facts']=[{'id':f['id'],'source':{'file':'spec.md','line':1,'number':i},'unit':f['unit']}
                  for i,f in enumerate(requirements['facts'],1)]
    return raw


def seed_work(out):
    from harness.runtime.design_session import work_context_identity
    d,s=fixture();store.dump(out/'work-model.current.json',{'work_model':d['work_model'],'work_model_sha256':store.digest(json.dumps(d['work_model'],ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()),'context_sha256':work_context_identity(d['requirements'],s),'origin':'unit-test fixture'})


def review():
    return {'verdict':'accept','checks':[{'topic':t,'status':'pass','evidence':'fixture evidence'} for t in prompts.REVIEW_TOPICS],'findings':[],'unverified':[]}


class CheckTests(unittest.TestCase):
    def test_safe_arithmetic(self):
        self.assertEqual(a.expression('ceil(work/lanes)+max(load,store)',{'work':25,'lanes':4,'load':2,'store':3}),10)
        for v in ('__import__("os").system("true")','x[0]','2**100'):
            with self.assertRaises((ValueError,SyntaxError)):a.expression(v,{})

    def test_replication_and_hard_budget_gate(self):
        d,s=fixture();r=a.calculate(d,s)
        self.assertTrue(r['ok']);self.assertEqual(r['candidates']['small']['macro_inventory_bits'],256)
        self.assertTrue(a.delivery_decision(d,r,review(),{'ok':True})['deliverable'])
        d['selected']='large'
        self.assertFalse(a.delivery_decision(d,r,review(),{'ok':True})['deliverable'])
        d['candidates'][0]['macros'][0]['count']='3'
        updated=a.calculate(d,s);self.assertTrue(updated['ok']);self.assertEqual(updated['candidates']['small']['macro_inventory_bits'],384)

    def test_empty_conditions_and_input_storage_rules(self):
        d,s=fixture();self.assertTrue(a.calculate(d,s)['ok'])
        d['candidates'][0]['macros'][0]['width']='7'
        self.assertTrue(any('macro dimensions violate input storage rule' in e for e in a.calculate(d,s)['errors']))

    def test_full_task_throughput_and_metric_link(self):
        d,s=fixture();d['candidates'][0]['workloads'][0]['items_per_second']='clock*items/80'
        self.assertFalse(a.calculate(d,s)['ok'])
        d,s=fixture();d['candidates'][0]['metrics']['throughput']='clock*items/80'
        self.assertFalse(a.calculate(d,s)['ok'])

    def test_component_sum_and_port_service(self):
        d,s=fixture();d['candidates'][0]['cost_groups'][1]['cycles']='10'
        result=a.calculate(d,s);self.assertTrue(result['ok']);self.assertEqual(result['candidates']['small']['workloads']['primary']['task_cycles'],110)
        d,s=fixture();d['candidates'][0]['resource_checks'][0]['demand_per_cycle']='64'
        self.assertFalse(a.calculate(d,s)['ok'])

    def test_physical_abi_field(self):
        d,s=fixture();d['candidates'][0]['abi_fields'][0]['width']='36'
        self.assertFalse(a.calculate(d,s)['ok'])

    def test_review_schema_and_failed_coverage(self):
        r=review();r['findings']=[{'severity':'note'}]
        self.assertTrue(a.validate_review(r))
        d,s=fixture();r=review();r['checks'][3]['status']='fail'
        self.assertFalse(a.delivery_decision(d,a.calculate(d,s),r,{'ok':True})['deliverable'])

    def test_unknown_hard_estimate_is_not_pass(self):
        d,s=fixture();d['candidates'][0]['metrics']['area']={'status':'unverified','reason':'no basis'}
        self.assertFalse(a.delivery_decision(d,a.calculate(d,s),review(),{'ok':True})['deliverable'])
        d,s=fixture()
        d['requirements']['constraints'][0].update({'id':'clock_check','limit':'clock','verification':'implementation','quantity':'clock','unit':'Hz'})
        for c in d['candidates']:
            c['metrics']['clock_check']={'status':'unverified','reason':'timing not measured'}
        self.assertTrue(a.delivery_decision(d,a.calculate(d,s),review(),{'ok':True})['deliverable'])

    def test_invalid_model_identifiers_are_revision_findings(self):
        d,s=fixture();d['requirements']['facts'][0]['id']=1
        _,errors=a.validate_requirements(d['requirements'],s)
        self.assertIn('fact id must be an identifier string: 1',errors)

    def test_storage_rules_rejected_during_extraction(self):
        d,s=fixture();d['requirements']['storage_rules'][0]['min_depth']='>=2'
        _,errors=a.validate_requirements(d['requirements'],s)
        self.assertTrue(any('storage rule must be executable' in e for e in errors))

    def test_false_citation(self):
        d,s=fixture();d['requirements']['facts'][0]['citations'][0]['quote']='area 2'
        self.assertFalse(a.calculate(d,s)['ok'])

    def test_plain_markdown_and_separate_contexts(self):
        d,s=fixture();data={k:v for k,v in d.items() if not k.endswith('_md')}
        from harness.runtime.design_session import identity
        raw='<<<ARCHITECTURE_DATA>>>\n'+json.dumps({'evaluated_design_sha256':identity(d)})+'\n<<<ARCHITECTURE_MD>>>\n'+d['architecture_md']
        self.assertEqual(prompts.parse_design(raw),{'evaluated_design_sha256':identity(d),'architecture_md':d['architecture_md']})
        audit=prompts.audit('AUDIT_SKILL',s,d,{'ok':True})
        self.assertNotIn('DESIGN_SKILL',audit)
        self.assertIn('AUDIT_SKILL',audit)


class LoopTests(unittest.TestCase):
    def setup_run(self):
        out=Path(tempfile.mkdtemp(prefix='architecture-loop-test-'))
        h=Path(__file__).resolve().parents[1]
        for name in ('microarchitecture-design','microarchitecture-audit'):
            p=out/'snapshot/harness/skills'/name;p.mkdir(parents=True)
            import shutil
            shutil.copytree(h/'skills'/name,p,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__'))
        # These tests isolate outer audit/revision policy; the real tool protocol
        # has separate execution tests below. No production bypass exists.
        mocked=patch.object(controller,'generate',side_effect=lambda out,label,prompt,req,src,invoke,limit,**options: invoke(label,'architecture',prompt,prompts.parse_design))
        mocked.start();self.addCleanup(mocked.stop)
        baseline=patch.object(controller.audit_session,'prepare_baseline',return_value={'scope':'mock controller-only policy'})
        baseline.start();self.addCleanup(baseline.stop)
        mocked_audit=patch.object(controller.audit_session,'run',side_effect=lambda out,label,src,doc,budget,invoke,limit,prior=None,**options:(invoke(label,'review',prompts.audit('TEST AUDIT',src,doc,budget)),{'ok':True,'scope':'mock policy isolation'}))
        mocked_audit.start();self.addCleanup(mocked_audit.stop)
        return out

    def settings(self,**kwargs):
        return {'max_extraction_repairs':0,'max_design_revisions':1,'max_stagnant_revisions':2,'max_model_calls':10,'request_timeout_seconds':30,'total_active_seconds':300,'thinking':'off','max_design_tool_calls':3,'max_audit_tool_calls':2,'max_audit_baseline_repairs':0,'max_design_format_repairs':2,'max_audit_report_repairs':3,'reserved_audit_calls':0,'reserved_audit_seconds':0,**kwargs}

    def test_revision_after_blocking_review(self):
        d,s=fixture();r=review();r['verdict']='revise';r['checks'][3]['status']='fail';r['findings']=[{'id':'abi','severity':'blocking','disposition':'open','category':'design','location':'ABI','issue':'missing address reconstruction','evidence':'test isolates necessary revision','required_change':'supply address reconstruction'}]
        out=self.setup_run();cfg=self.settings();calls=controller.CallBudget(out,cfg)
        with patch.object(controller,'call_model',side_effect=[{'requirements':requirements_declaration(d['requirements'])},d,r,d,review()]) as model:
            state=controller.execute(out,s,{},out,cfg,calls)
        self.assertEqual(state['status'],'accepted_conditional_design')
        self.assertEqual(model.call_count,5)
        self.assertTrue((out/'design-0.json').exists());self.assertTrue((out/'design-1.json').exists())

    def test_resolved_observation_is_not_requirement_change(self):
        d,s=fixture();d['requirement_corrections']=[{'id':'confirmed','requires_change':False,'corrected_requirement':None}]
        out=self.setup_run();cfg=self.settings(max_design_revisions=0)
        with patch.object(controller,'call_model',side_effect=[{'requirements':requirements_declaration(d['requirements'])},d,review()]):
            state=controller.execute(out,s,{},out,cfg,controller.CallBudget(out,cfg))
        self.assertEqual(state['status'],'accepted_conditional_design')

    def test_hard_failure_not_released(self):
        d,s=fixture();d['selected']='large';out=self.setup_run();cfg=self.settings(max_design_revisions=0)
        with patch.object(controller,'call_model',side_effect=[{'requirements':requirements_declaration(d['requirements'])},d,review()]):
            state=controller.execute(out,s,{},out,cfg,controller.CallBudget(out,cfg))
        self.assertEqual(state['status'],'stopped_revision_limit')
        self.assertFalse((out/'architecture.md').exists())

    def test_invalid_checks_skip_audit_and_stop(self):
        d,s=fixture();d['candidates'][0]['workloads'][0]['items']='missing';out=self.setup_run();cfg=self.settings(max_design_revisions=0)
        with patch.object(controller,'call_model',side_effect=[{'requirements':requirements_declaration(d['requirements'])},d]) as model:
            state=controller.execute(out,s,{},out,cfg,controller.CallBudget(out,cfg))
        self.assertEqual(state['status'],'stopped_revision_limit');self.assertEqual(model.call_count,2)

    def test_stagnation_ignores_document_rewording(self):
        d,s=fixture();other=copy.deepcopy(d);other['architecture_md']='changed words'
        self.assertEqual(controller.progress_signature(d),controller.progress_signature(other))
        r=review();r['verdict']='revise';r['findings']=[{'id':'abi','severity':'blocking','disposition':'open','category':'design','location':'ABI','issue':'missing address reconstruction','evidence':'test isolates necessary revision','required_change':'supply address reconstruction'}];out=self.setup_run();cfg=self.settings(max_design_revisions=4,max_stagnant_revisions=1)
        with patch.object(controller,'call_model',side_effect=[{'requirements':requirements_declaration(d['requirements'])},d,r,other,r]):
            state=controller.execute(out,s,{},out,cfg,controller.CallBudget(out,cfg))
        self.assertEqual(state['status'],'stopped_no_progress')

    def test_insufficient_audit_evidence_does_not_trigger_design_revision(self):
        d,s=fixture();r=review();r['verdict']='insufficient_evidence';out=self.setup_run();cfg=self.settings(max_design_revisions=3)
        with patch.object(controller,'call_model',side_effect=[{'requirements':requirements_declaration(d['requirements'])},d,r]) as model:
            state=controller.execute(out,s,{},out,cfg,controller.CallBudget(out,cfg))
        self.assertEqual(state['status'],'stopped_audit_evidence');self.assertEqual(model.call_count,3)
        self.assertFalse((out/'design-1.json').exists());self.assertFalse((out/'architecture.md').exists())

    def test_call_budget_persists_and_limits(self):
        out=self.setup_run();cfg=self.settings(max_model_calls=1);c=controller.CallBudget(out,cfg);c.reserve()
        with self.assertRaises(controller.LimitReached):c.reserve()
        c.save();new=controller.CallBudget(out,cfg)
        with self.assertRaises(controller.LimitReached):new.reserve()

    def test_frozen_requirements_cannot_be_replaced(self):
        d,s=fixture();changed=copy.deepcopy(d);changed['requirements']['constraints']=[]
        out=self.setup_run();cfg=self.settings()
        with patch.object(controller,'call_model',side_effect=[{'requirements':requirements_declaration(d['requirements'])},changed]):
            state=controller.execute(out,s,{},out,cfg,controller.CallBudget(out,cfg))
        self.assertEqual(state['status'],'needs_requirement_revision')

    def test_run_identity_and_snapshot_tamper(self):
        import shutil
        root=Path(tempfile.mkdtemp(prefix='architecture-identity-test-'))
        h=Path(__file__).resolve().parents[1]
        shutil.copytree(h,root/'harness',ignore=shutil.ignore_patterns('__pycache__'))
        (root/'input').mkdir()
        (root/'input/spec.md').write_text('area 3 capacity 1000')
        (root/'input/reference.py').write_text('def alternate(x): return x\n')
        args=SimpleNamespace(config=root/'harness/config/environment.toml',feedback=None,resume=None,run_id='identity',validate_inputs=False)
        project,models,cfg,sources,files,hashes,check=store.load(args)
        out,lock=store.open_run(args,project,models,cfg,files,hashes,check)
        store.close_run(out,lock,{'status':'test'})
        args.run_id=None;args.resume='identity'
        (out/'snapshot/input/spec.md').write_text('tampered')
        with self.assertRaisesRegex(ValueError,'snapshot changed'):
            store.open_run(args,project,models,cfg,files,hashes,check)

    def test_vendored_skill_tamper(self):
        import shutil
        h=Path(__file__).resolve().parents[1]
        root=Path(tempfile.mkdtemp(prefix='architecture-vendor-test-'))
        shutil.copytree(h/'skills',root/'skills')
        (root/'skills/.DS_Store').write_bytes(b'Finder metadata')
        store.verify_skills(root)
        (root/'skills/microarchitecture-design/SKILL.md').write_text('changed')
        with self.assertRaises(ValueError):store.verify_skills(root)

    def test_resume_review_without_regenerating_design(self):
        d,s=fixture();out=self.setup_run();cfg=self.settings()
        with patch.object(controller,'call_model',side_effect=[{'requirements':requirements_declaration(d['requirements'])},d,RuntimeError('provider failure')]):
            with self.assertRaises(RuntimeError):controller.execute(out,s,{},out,cfg,controller.CallBudget(out,cfg))
        with patch.object(controller,'call_model',return_value=review()) as model:
            state=controller.execute(out,s,{},out,cfg,controller.CallBudget(out,cfg))
        self.assertEqual(state['status'],'accepted_conditional_design');self.assertEqual(model.call_count,1)


class SkillToolTests(unittest.TestCase):
    def setup_run(self):
        import shutil
        out=Path(tempfile.mkdtemp(prefix='skill-tool-test-'))
        h=Path(__file__).resolve().parents[1]
        shutil.copytree(h/'skills/microarchitecture-design',out/'snapshot/harness/skills/microarchitecture-design',ignore=shutil.ignore_patterns('__pycache__'))
        seed_work(out)
        return out

    def test_real_cli_and_ledger_bounds(self):
        from harness.runtime.design_session import execute_tool
        d,s=fixture();out=self.setup_run()
        result=execute_tool(out,'good',d,s,True)
        self.assertTrue(result['ok']);self.assertEqual(result['gate']['status'],'pass')
        self.assertTrue((out/'good.execution.json').exists())
        cases=[('cost_groups','overhead_cycles','-1'),('traffic','inflight','7'),('traffic','payload_bytes','0'),('buffers','required_bits','257'),('schedule_windows','accesses','9'),('area_components','unit_area_upper','1')]
        for index,(section,key,value) in enumerate(cases):
            broken=copy.deepcopy(d);broken['candidates'][0][section][0][key]=value
            result=execute_tool(out,'bad'+str(index),broken,s)
            self.assertFalse(result['ok'],section+key)

    def test_same_carrier_overlap_and_unknown_budget(self):
        from harness.runtime.design_session import execute_tool
        d,s=fixture();out=self.setup_run()
        d['candidates'][0]['abi_fields'].append({'name':'overlap','carrier':'command-address','width':'8','offset':'16','carrier_width':'32'})
        self.assertFalse(execute_tool(out,'overlap',d,s)['ok'])
        d,s=fixture();d['candidates'][0]['metrics']['area']={'status':'unverified','reason':'no basis'}
        # Area ledger must bind the metric, so this missing cost is deterministically blocked.
        self.assertNotEqual(execute_tool(out,'unknown',d,s)['gate']['status'],'pass')

    def test_tool_result_precedes_final_and_resume_reuses_turns(self):
        from harness.runtime.design_session import generate
        d,s=fixture();out=self.setup_run();request={'tool_request':{'tool':'evaluate_design','document':copy.deepcopy(d)}}
        invoke=unittest.mock.Mock(side_effect=[request,RuntimeError('provider interruption')])
        with self.assertRaises(RuntimeError):generate(out,'design','PROMPT',d['requirements'],s,invoke,2)
        self.assertTrue((out/'design.step-0.tool.result.json').exists())
        from harness.runtime.design_session import identity
        invoke=unittest.mock.Mock(return_value={'final':{'evaluated_design_sha256':identity(d),'architecture_md':d['architecture_md']}})
        result=generate(out,'design','PROMPT',d['requirements'],s,invoke,2)
        self.assertEqual(invoke.call_count,1);self.assertIn('tool_receipt',result)
        self.assertIn('minimum_cycles',invoke.call_args.args[2])

    def test_final_without_execution_and_changed_data_rejected(self):
        from harness.runtime.design_session import generate,DesignToolLimit
        d,s=fixture();out=self.setup_run()
        with self.assertRaises(DesignToolLimit):generate(out,'early','',d['requirements'],s,unittest.mock.Mock(return_value={'final':d}),1)
        changed=copy.deepcopy(d);changed['candidates'][0]['assumptions'][0]['value']=1
        invoke=unittest.mock.Mock(side_effect=lambda *args: {'tool_request':{'tool':'evaluate_design','document':copy.deepcopy(d)}} if 'step-0' in args[0] else {'final':changed})
        with self.assertRaises(DesignToolLimit):generate(out,'changed','',d['requirements'],s,invoke,1)

    def test_omp_machine_events_do_not_leak_thinking(self):
        from harness.adapters.omp import final_text,OMPTransportError
        message={'role':'assistant','stopReason':'stop','content':[{'type':'thinking','thinking':'wrong draft and native tool tags'}, {'type':'text','text':'{"probe":"ok"}'}]}
        stream='\n'.join(json.dumps(x) for x in [{'type':'message_end','message':message},{'type':'agent_end'}])
        self.assertEqual(final_text(stream),'{"probe":"ok"}')
        with self.assertRaises(ValueError):final_text(stream+ '\n'+json.dumps({'type':'message_end','message':message}))
        message['stopReason']='length'
        with self.assertRaises(OMPTransportError):final_text(json.dumps({'type':'message_end','message':message}))

    def test_first_design_prompt_has_only_proposal_schema(self):
        d,s=fixture();prompt=prompts.design('SKILL',s,d['requirements'])
        self.assertNotIn('Return a multipart response with exactly',prompt)
        self.assertIn('first turn ONLY',prompt)

    def test_failed_native_attempt_is_not_the_final_answer(self):
        from harness.adapters.omp import final_text
        events=[{'type':'message_end','message':{'role':'assistant','stopReason':'toolUse','content':[{'type':'toolCall','id':'bad','name':'unavailable_tool'}]}}, {'type':'message_end','message':{'role':'toolResult','toolCallId':'bad','isError':True}}, {'type':'message_end','message':{'role':'assistant','stopReason':'stop','content':[{'type':'text','text':'{"ok":true}'}]}}, {'type':'agent_end'}]
        self.assertEqual(final_text('\n'.join(map(json.dumps,events))),'{"ok":true}')
        events[1]['message']['isError']=False
        with self.assertRaises(ValueError):final_text('\n'.join(map(json.dumps,events)))

    def test_native_call_requires_failed_or_guard_block_evidence(self):
        from harness.adapters.omp import final_text
        events=[{'type':'message_end','message':{'role':'assistant','stopReason':'toolUse','content':[{'type':'toolCall','id':'blocked','name':'mcp_unknown'}]}},{'type':'message_end','message':{'role':'assistant','stopReason':'stop','content':[{'type':'text','text':'{"ok":true}'}]}},{'type':'agent_end'}]
        text='\n'.join(map(json.dumps,events))
        with self.assertRaises(ValueError):final_text(text)
        self.assertEqual(final_text(text,blocked_ids=['blocked']),'{"ok":true}')

    def test_frequency_cannot_be_invented_or_literal(self):
        d,s=fixture()
        d['candidates'][0]['workloads'][0]['clock_hz']='80000000'
        self.assertFalse(a.calculate(d,s)['ok'])
        d,s=fixture();d['candidates'][0]['workloads'][0]['clock_hz']='clock*2'
        self.assertFalse(a.calculate(d,s)['ok'])

    def test_reference_final_reuses_evaluated_data_after_format_error(self):
        from harness.runtime.design_session import generate,identity
        d,s=fixture();out=self.setup_run()
        reference={'evaluated_design_sha256':identity(d),'architecture_md':d['architecture_md']}
        invoke=unittest.mock.Mock(side_effect=[{'tool_request':{'tool':'evaluate_design','document':copy.deepcopy(d)}},ValueError('malformed serialization'),{'final':reference}])
        result=generate(out,'ref','',d['requirements'],s,invoke,2)
        self.assertEqual(result['candidates'],d['candidates']);self.assertIn('tool_receipt',result)
        self.assertTrue((out/'ref.step-1.json').exists())

    def test_forged_reference_and_malformed_request_stop(self):
        from harness.runtime.design_session import generate,DesignToolLimit,parse_turn
        d,s=fixture();out=self.setup_run()
        fake={'evaluated_design_sha256':'forged','architecture_md':d['architecture_md']}
        with self.assertRaises(DesignToolLimit):generate(out,'fake','',d['requirements'],s,unittest.mock.Mock(return_value={'final':fake}),1)
        raw='Explanation\n<<<BUDGET_REQUEST>>>\n'+json.dumps({'tool':'evaluate_design','document':d})
        self.assertIn('tool_request',parse_turn(raw))
        with self.assertRaises(ValueError):parse_turn(raw+'\n</native-tool>')

    def test_restricted_request_and_final_budget_failure(self):
        from harness.runtime.design_session import parse_turn,execute_tool
        with self.assertRaises(ValueError):parse_turn('<<<BUDGET_REQUEST>>>\n'+json.dumps({'tool':'shell','document':{}}))
        d,s=fixture();d['selected']='large'
        result=execute_tool(self.setup_run(),'over',d,s,True)
        self.assertTrue(result['ok']);self.assertEqual(result['gate']['status'],'fail')

class CalculationRepairTests(unittest.TestCase):
    def test_frequency_fact_units_are_converted_without_design_aliases(self):
        d,s=fixture();f=next(x for x in d['requirements']['facts'] if x['id']=='clock')
        s['spec.md'] += '\nfrequency 80 MHz'
        f.update(value=80,unit='MHz',citations=[{'file':'spec.md','start':2,'quote':'frequency 80 MHz'}])
        result=a.calculate(d,s);self.assertTrue(result['ok'],result['errors'])
        self.assertEqual(result['candidates']['small']['workloads']['primary']['clock_hz'],80000000)
        c=d['candidates'][0];c['assumptions'].append({'id':'invented_clock','value':80000000,'unit':'Hz','basis':'not input'})
        c['workloads'][0]['clock_fact']='invented_clock'
        self.assertFalse(a.calculate(d,s)['ok'])

    def test_controller_real_script_and_independent_review(self):
        from harness.runtime.design_session import identity
        import shutil
        d,s=fixture();out=SkillToolTests().setup_run()
        h=Path(__file__).resolve().parents[1]
        shutil.copytree(h/'skills/microarchitecture-audit',out/'snapshot/harness/skills/microarchitecture-audit')
        from test_audit import freeze_snapshot
        freeze_snapshot(out,s)
        cfg=LoopTests().settings(max_design_revisions=0)
        reference={'evaluated_design_sha256':identity(d)}
        final='<<<ARCHITECTURE_DATA>>>\n'+json.dumps(reference)
        final+='\n<<<ARCHITECTURE_MD>>>\n'+d['architecture_md']
        roles=[]
        def invoke(folder,label,role,prompt,models,root,timeout,thinking,parser=None,before_call=None,record_observed=None):
            before_call();roles.append(role)
            if label.startswith('extract-'):return parser('<<<REQUIREMENTS>>>\n'+json.dumps(requirements_declaration(d['requirements'])))
            if '.baseline-' in label:
                from test_audit import baseline_fixture
                return parser('<<<AUDIT_BASELINE>>>\n'+json.dumps(baseline_fixture(s)))
            if role=='architecture' and label.endswith('step-0'):
                return parser('<<<BUDGET_REQUEST>>>\n'+json.dumps({'tool':'evaluate_design','document':d}))
            if role=='architecture' and label.endswith('step-1'):
                self.assertIn('minimum_cycles',prompt)
                return parser(final)
            self.assertEqual(role,'review');self.assertNotIn('SKILL TOOL HISTORY',prompt)
            from test_audit import report_content_fixture, proposal_request, wire_reply
            if label=='review-0.step-0':return wire_reply({'tool_request':proposal_request()},parser)
            context=json.loads((out/'review-0.packet.json').read_text());receipt=json.loads((out/'review-0.step-0.tool.result.json').read_text());receipt['request_id']='review-0.step-0';context['calculations']=[receipt];context['proposal_checks']=[receipt]
            return wire_reply({'final':report_content_fixture(context)},parser)
        with patch.object(controller,'call_model',side_effect=invoke):
            state=controller.execute(out,s,{},out,cfg,controller.CallBudget(out,cfg))
        self.assertEqual(state['status'],'accepted_conditional_design')
        self.assertEqual(roles,['review','architecture','architecture','architecture','review','review'])
        self.assertTrue((out/'design-0.step-0.tool.execution.json').exists())
        self.assertTrue((out/'design-0.final-gate.execution.json').exists())
        self.assertEqual([p.name for p in out.glob('*.md')],['architecture.md'])
        self.assertEqual((out/'architecture.md').read_text(),json.loads((out/'review-0.packet.json').read_text())['sources']['architecture.md'])

    def test_derived_choices_and_literal_calculation_recomputed(self):
        d,s=fixture()
        c=d['candidates'][0]
        c['assumptions'] += [{'id':'lanes','value':3,'unit':'units','basis':'choice'}, {'id':'units','formula':'lanes*2','unit':'units','basis':'derived replication'}]
        c['calculations'] += [{'id':'task_setup','value':'units+1','unit':'cycles','basis':'derived cost'}]
        result=a.calculate(d,s)
        self.assertTrue(result['ok']);self.assertEqual(result['candidates']['small']['values']['task_setup'],7)

    def test_missing_expression_context_and_conflicting_fields_block(self):
        d,s=fixture();c=d['candidates'][0]
        c['calculations'] += [{'id':'missing','unit':'cycles','basis':'incomplete'}, {'id':'conflicting','value':3,'formula':'1','unit':'cycles','basis':'ambiguous'}, {'id':'bad_dependency','formula':'undefined_cost+1','unit':'cycles','basis':'undefined'}]
        result=a.calculate(d,s)
        self.assertFalse(result['ok'])
        self.assertTrue(any('small calculations/missing' in e for e in result['errors']))
        self.assertTrue(any('conflicting' in e for e in result['errors']))
        self.assertTrue(any('undefined identifier undefined_cost' in e for e in result['errors']))
        self.assertIn('large',result['candidates'])

    def test_fact_overwrite_rejected(self):
        d,s=fixture();d['candidates'][0]['assumptions'].append({'id':'area_limit','value':99,'unit':'mm2','basis':'invalid override'})
        result=a.calculate(d,s)
        self.assertFalse(result['ok']);self.assertEqual(result['candidates']['small']['values']['area_limit'],3)

    def test_format_error_does_not_spend_tool_requests(self):
        from harness.runtime.design_session import generate,identity
        d,s=fixture();out=SkillToolTests().setup_run()
        ref={'evaluated_design_sha256':identity(d),'architecture_md':d['architecture_md']}
        request={'tool_request':{'tool':'evaluate_design','document':copy.deepcopy(d)}}
        invoke=unittest.mock.Mock(side_effect=[ValueError('format'),copy.deepcopy(request),copy.deepcopy(request),{'final':ref}])
        result=generate(out,'repairs','',d['requirements'],s,invoke,2)
        self.assertIn('tool_receipt',result)
        self.assertEqual(len(list(out.glob('*.tool.execution.json'))),2)

    def test_repeated_bad_responses_stop_and_extra_request_is_not_executed(self):
        from harness.runtime.design_session import generate,DesignToolLimit
        d,s=fixture();out=SkillToolTests().setup_run()
        bad=unittest.mock.Mock(side_effect=ValueError('format'))
        with self.assertRaises(DesignToolLimit):generate(out,'bad','',d['requirements'],s,bad,1)
        self.assertEqual(bad.call_count,3)
        request=unittest.mock.Mock(return_value={'tool_request':{'tool':'evaluate_design','document':copy.deepcopy(d)}})
        with self.assertRaises(DesignToolLimit):generate(out,'extra','',d['requirements'],s,request,1)
        self.assertEqual(len(list(out.glob('extra*.tool.execution.json'))),1)

if __name__=='__main__':unittest.main()

