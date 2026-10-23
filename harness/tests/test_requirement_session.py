"""Bounded framed extraction and whole-fact repair use the frozen source checker."""
import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock

from harness.runtime import requirement_session as session


def fixture():
    source='Indices 0..5. Width 8 bits. Clock 7 kHz. Density 2 Mbit/mm2 (decimal). Area 3 mm2.'
    sources={'spec.md':source,'reference.py':'def sequence():\n    return list(range(6))\n'}
    def fact(name,number,unit=None):
        row={'id':name,'source':{'file':'spec.md','line':1,'number':number}}
        if unit is not None:row['unit']=unit
        return row
    requirements={'facts':[fact('cardinality',99,'count'),fact('width',3),
                           fact('clock',4),fact('density',5),fact('area',6)],
                  'constraints':[{'id':'area_bound','limit':'area','kind':'hard','relation':'<=','unit':'mm2','quantity':'area','verification':'estimate'}],
                  'functional_and_state':'Six legal indices from zero through five are a hard obligation.',
                  'numerical':'Exact integer enumeration.','workloads':'One task enumerates the fixed legal set.',
                  'interfaces_and_storage':'Input width is eight bits.','choices':'Organization remains open.',
                  'conflicts':[],'open_conditions':[],'runtime_parameters':[],
                  'storage_area_model':{'status':'supplied','density_fact':'density'},'storage_rules':[]}
    return requirements,sources


def repaired(requirements):
    old={'id':'cardinality','formula':'upper_bound+1','unit':'count','citations':[{'file':'spec.md','quote':'Indices 0..5.','start':1,'end':1}]}
    upper={'id':'upper_bound','source':{'file':'spec.md','line':1,'number':2},'unit':'count'}
    request={'base_sha256':session.identity(requirements),'facts':[old,upper]}
    return request,session.repair_requirements(requirements,request)


def selector_fixture():
    requirements,_=fixture()
    sources={'spec.md':'Indices 0..5.\nWidth 8 bits.\nClock 7 kHz.\nDensity 2 Mbit/mm2 (decimal).\nArea 3 mm2.\n',
             'reference.py':'def sequence():\n    return list(range(6))\n'}
    span={'file':'spec.md','start':1,'end':1,'quote':'Indices 0..5.'}
    def source(name,line,unit=None,number=1):
        row={'id':name,'source':{'file':'spec.md','line':line,'number':number}}
        if unit is not None:row['unit']=unit
        return row
    requirements['facts']=[{'id':'cardinality','formula':'upper_bound+1','unit':'count','citations':[span]},
                           source('upper_bound',1,'count',2),source('width',2),source('clock',3),
                           source('density',4),source('area',5),{'id':'clock_hz','convert':{'fact':'clock','to_unit':'Hz'}}]
    return requirements,sources


class RequirementSessionTests(unittest.TestCase):
    def snapshot(self):
        out=Path(tempfile.mkdtemp(prefix='requirement-session-'))
        source=Path(__file__).resolve().parents[1]/'skills/microarchitecture-design'
        shutil.copytree(source,out/'snapshot/harness/skills/microarchitecture-design',ignore=shutil.ignore_patterns('__pycache__'))
        return out

    def materialized(self,out,raw,sources):
        result=session._checker(out).prepare_requirements(raw,sources)
        self.assertEqual(result['errors'],[])
        return result['requirements']

    def test_open_conditions_string_is_rejected_before_requirements_freeze(self):
        raw,sources=selector_fixture();raw['open_conditions']='Clock uncertainty remains unspecified.'
        before=copy.deepcopy(raw);out=self.snapshot();invoke=Mock(return_value={'requirements':raw})
        result,errors=session.prepare(out,sources,invoke,0)
        self.assertIsNone(result);self.assertEqual(errors,['open_conditions must be a list; empty is legal'])
        self.assertFalse((out/'extraction.json').exists());self.assertEqual(raw,before)
        declaration=json.loads((out/'extract-0.declaration.json').read_text())
        materialized=json.loads((out/'extract-0.json').read_text())
        self.assertEqual(declaration['open_conditions'],raw['open_conditions'])
        self.assertEqual(materialized['open_conditions'],raw['open_conditions'])
        self.assertFalse(json.loads((out/'extract-0.check.json').read_text())['ok'])

    def test_open_conditions_lists_are_accepted_without_conversion(self):
        for conditions in ([],['Clock uncertainty remains unspecified.']):
            with self.subTest(conditions=conditions):
                raw,sources=selector_fixture();raw['open_conditions']=conditions;before=copy.deepcopy(raw)
                out=self.snapshot();result,errors=session.prepare(out,sources,Mock(return_value={'requirements':raw}),0)
                self.assertEqual(errors,[]);self.assertEqual(result['open_conditions'],conditions)
                self.assertEqual(raw,before);self.assertTrue((out/'extraction.json').exists())

    def test_full_requirements_and_candidate_use_one_conditions_rule(self):
        from harness.tests.test_architecture import fixture as design_fixture
        out=self.snapshot();checker=session._checker(out)
        for conditions in ('Unspecified physical conditions.',None,{}):
            with self.subTest(conditions=conditions):
                document,sources=design_fixture();document['requirements']['open_conditions']=conditions
                before=copy.deepcopy(document)
                _,early=checker.validate_requirements(document['requirements'],sources)
                late=checker.evaluate(document,sources)
                error='open_conditions must be a list; empty is legal'
                self.assertIn(error,early);self.assertFalse(late['ok'])
                self.assertEqual(late['errors'].count(error),1)
                self.assertEqual(document,before)
        document,sources=design_fixture();document['requirements'].pop('open_conditions')
        self.assertIn('open_conditions must be a list; empty is legal',checker.validate_requirements(document['requirements'],sources)[1])
        # The shared audit arithmetic API accepts its existing minimal envelope;
        # it neither needs a full design contract nor receives a filled default.
        arithmetic={'facts':[],'constraints':[],'runtime_parameters':[]}
        self.assertEqual(checker.validate_requirements(arithmetic,sources),({},[]))
        self.assertNotIn('open_conditions',arithmetic)

    def test_framed_full_and_repair_objects_and_serialization_errors(self):
        requirements,_=fixture();request,_=repaired(requirements)
        self.assertEqual(session.parse_turn('<<<REQUIREMENTS>>>\n'+json.dumps(requirements)),{'requirements':requirements})
        self.assertEqual(session.parse_turn('<<<REQUIREMENTS_REPAIR>>>\n'+json.dumps(request)),{'repair':request})
        for text in (json.dumps(requirements),'explanation\n<<<REQUIREMENTS>>>\n'+json.dumps(requirements),
                     '<<<REQUIREMENTS>>>\n'+json.dumps(requirements)+'\nmore explanation',
                     '<<<REQUIREMENTS>>>\n'+json.dumps(requirements)+'\n<<<REQUIREMENTS_REPAIR>>>\n'+json.dumps(request)):
            with self.assertRaises(ValueError):session.parse_turn(text)
        with self.assertRaises(json.JSONDecodeError):session.parse_turn('<<<REQUIREMENTS>>>\n{"facts":[] "constraints":[]}')

    def test_fact_replacement_drops_obsolete_fields_and_preserves_siblings(self):
        requirements,sources=fixture();before=copy.deepcopy(requirements);source_before=copy.deepcopy(sources)
        request,result=repaired(requirements)
        self.assertNotIn('source',result['facts'][0]);self.assertEqual(result['facts'][0]['formula'],'upper_bound+1')
        self.assertEqual(result['facts'][1:-1],requirements['facts'][1:])
        self.assertEqual(result['facts'][-1]['id'],'upper_bound')
        for key in set(requirements)-{'facts'}:self.assertEqual(result[key],requirements[key])
        self.assertEqual(requirements,before);self.assertEqual(sources,source_before)
        request['facts'][0]['formula']='999'
        self.assertEqual(result['facts'][0]['formula'],'upper_bound+1')

    def test_stale_duplicate_partial_and_nontarget_repairs_are_rejected(self):
        requirements,_=fixture();request,_=repaired(requirements)
        bad=[dict(request,base_sha256='stale'),dict(request,facts=[request['facts'][0],request['facts'][0]]),
             dict(request,facts=[{'id':'cardinality','formula':'upper_bound+1'}]),
             dict(request,constraints=[]),dict(request,runtime_parameters=[]),dict(request,source={}),
             {'base_sha256':session.identity(requirements),'facts':[]}]
        before=copy.deepcopy(requirements)
        for row in bad:
            with self.subTest(row=row):
                with self.assertRaises(ValueError):session.repair_requirements(requirements,row)
                self.assertEqual(requirements,before)
        both=copy.deepcopy(request);both['facts'][0]['value']=6
        with self.assertRaises(ValueError):session.repair_requirements(requirements,both)

    def test_storage_model_replacement_is_whole_and_still_source_checked(self):
        requirements,sources=fixture();request,good=repaired(requirements);out=self.snapshot()
        request={'base_sha256':session.identity(good),'facts':[],
                 'storage_area_model':{'status':'unspecified','reason':'replacement is transport, not authority over source truth'}}
        changed=session.repair_requirements(good,request)
        self.assertNotIn('density_fact',changed['storage_area_model'])
        self.assertEqual(changed['facts'],good['facts']);self.assertEqual(good['storage_area_model']['status'],'supplied')
        # A supplied model referencing the derived cardinality cannot pass the
        # same frozen density rule merely because a transport replacement worked.
        changed['storage_area_model']={'status':'supplied','density_fact':'cardinality'}
        invoke=Mock(return_value={'requirements':changed})
        result,errors=session.prepare(out,sources,invoke,0)
        self.assertIsNone(result);self.assertTrue(any('density_fact' in error or 'density' in error for error in errors))
        self.assertFalse((out/'extraction.json').exists())

    def test_prepare_locally_repairs_invalid_source_with_real_source_checker(self):
        requirements,sources=fixture();request,good=repaired(requirements);before=copy.deepcopy(requirements)
        out=self.snapshot();prompts=[]
        def invoke(label,role,prompt,parser):
            prompts.append(prompt)
            if label=='extract-0':return parser('<<<REQUIREMENTS>>>\n'+json.dumps(requirements))
            self.assertEqual(label,'extract-1');self.assertIn(session.identity(requirements),prompt)
            self.assertIn('source selector number is outside',prompt);self.assertIn('complete corrected/new fact rows',prompt)
            return parser('<<<REQUIREMENTS_REPAIR>>>\n'+json.dumps(request))
        result,errors=session.prepare(out,sources,invoke,2)
        self.assertEqual(errors,[]);self.assertEqual(result,self.materialized(out,good,sources));self.assertEqual(requirements,before)
        self.assertEqual(len(prompts),2);self.assertIn('<<<REQUIREMENTS>>>',prompts[0])
        first=json.loads((out/'extract-0.check.json').read_text());self.assertFalse(first['ok'])
        self.assertTrue(json.loads((out/'extract-1.check.json').read_text())['ok'])
        self.assertEqual(json.loads((out/'extract-0.declaration.json').read_text()),requirements)
        self.assertEqual(json.loads((out/'extract-1.reply.json').read_text()),{'repair':request})
        self.assertEqual(json.loads((out/'extraction.declaration.json').read_text()),good)
        self.assertEqual(json.loads((out/'extraction.json').read_text()),result)

    def test_bad_formula_units_and_source_unit_overrides_remain_blocked(self):
        requirements,sources=fixture();_,good=repaired(requirements)
        bad_fact={'id':'clock','formula':'clock_source*clock_source','unit':'Hz',
                  'citations':[{'file':'spec.md','quote':'Clock 7 kHz.','start':1,'end':1}]}
        source_fact={'id':'clock_source','source':{'file':'spec.md','line':1,'number':4}}
        request={'base_sha256':session.identity(good),'facts':[good['facts'][0],bad_fact,source_fact]}
        out=self.snapshot();invoke=Mock(side_effect=[{'requirements':good}])
        # Start from a deliberately invalid full table so local transport never
        # gains authority to alter dimensions or bypass the original checker.
        full=copy.deepcopy(good);full['facts'][0]['formula']='upper_bound+clock'
        request['base_sha256']=session.identity(full)
        invoke=Mock(side_effect=[{'requirements':full},{'repair':request}])
        result,errors=session.prepare(out,sources,invoke,1)
        self.assertIsNone(result);self.assertTrue(any('dimension' in error for error in errors));self.assertFalse((out/'extraction.json').exists())
        replacement=copy.deepcopy(good['facts'][2]);replacement['unit']='Hz'
        full=copy.deepcopy(good);full['facts'][0]['formula']='upper_bound+clock'
        request={'base_sha256':session.identity(full),'facts':[good['facts'][0],replacement]}
        out=self.snapshot();result,errors=session.prepare(out,sources,Mock(side_effect=[{'requirements':full},{'repair':request}]),1)
        self.assertIsNone(result);self.assertTrue(any('override is not a conversion' in error for error in errors))

    def test_resume_reuses_failed_extraction_and_only_calls_unfinished_turn(self):
        requirements,sources=fixture();request,good=repaired(requirements);out=self.snapshot()
        first=Mock(side_effect=[{'requirements':requirements},RuntimeError('provider interrupted')])
        with self.assertRaises(RuntimeError):session.prepare(out,sources,first,2)
        old=(out/'extract-0.json').read_bytes()
        resumed=Mock(return_value={'repair':request});result,errors=session.prepare(out,sources,resumed,2)
        self.assertEqual(errors,[]);self.assertEqual(result,self.materialized(out,good,sources));self.assertEqual(resumed.call_count,1)
        self.assertEqual(resumed.call_args.args[0],'extract-1');self.assertEqual((out/'extract-0.json').read_bytes(),old)
        frozen=Mock(side_effect=AssertionError('accepted requirements must remain frozen'))
        self.assertEqual(session.prepare(out,sources,frozen,2),(result,[]));self.assertEqual(frozen.call_count,0)

    def test_completed_format_failure_is_replayed_without_model_recall(self):
        requirements,sources=fixture();_,good=repaired(requirements);out=self.snapshot()
        first=Mock(side_effect=[ValueError('narration instead of requirements frame'),RuntimeError('provider interrupted')])
        with self.assertRaises(RuntimeError):session.prepare(out,sources,first,2)
        self.assertFalse((out/'extract-0.json').exists());old=(out/'extract-0.check.json').read_bytes()
        resumed=Mock(return_value={'requirements':good});result,errors=session.prepare(out,sources,resumed,2)
        self.assertEqual(errors,[]);self.assertEqual(result,self.materialized(out,good,sources));self.assertEqual(resumed.call_count,1)
        self.assertEqual(resumed.call_args.args[0],'extract-1');self.assertEqual((out/'extract-0.check.json').read_bytes(),old)

    def test_full_frame_can_correct_other_fields_without_partial_compatibility(self):
        requirements,sources=fixture();_,good=repaired(requirements);bad=copy.deepcopy(good)
        bad['constraints'][0]['unit']='bits';out=self.snapshot()
        result,errors=session.prepare(out,sources,Mock(side_effect=[{'requirements':bad},{'requirements':good}]),1)
        self.assertEqual(errors,[]);self.assertEqual(result,self.materialized(out,good,sources))
        self.assertFalse(json.loads((out/'extract-0.check.json').read_text())['ok'])
        self.assertEqual(json.loads((out/'extract-1.reply.json').read_text()),{'requirements':good})

    def test_three_attempt_limit_and_accepted_tamper_rejection(self):
        requirements,sources=fixture();out=self.snapshot();invoke=Mock(return_value={'requirements':requirements})
        result,errors=session.prepare(out,sources,invoke,2)
        self.assertIsNone(result);self.assertEqual(invoke.call_count,3);self.assertTrue(errors)
        _,good=repaired(requirements);out=self.snapshot()
        result,errors=session.prepare(out,sources,Mock(return_value={'requirements':good}),0)
        self.assertEqual(errors,[]);self.assertEqual(result,self.materialized(out,good,sources))
        bad=copy.deepcopy(result);bad['facts'][1]['value']=99
        (out/'extraction.json').write_text(json.dumps(bad))
        invoke=Mock(side_effect=AssertionError('must not rewrite an accepted checkpoint'))
        with self.assertRaisesRegex(ValueError,'accepted requirements checkpoint'):session.prepare(out,sources,invoke,2)
        self.assertEqual(invoke.call_count,0)

    def test_source_and_convert_rows_are_complete_repair_declarations(self):
        requirements,_=fixture();source={'id':'width','source':{'file':'spec.md','line':1,'number':3}}
        converted={'id':'clock_hz','convert':{'fact':'clock','to_unit':'Hz'}}
        request={'base_sha256':session.identity(requirements),'facts':[source,converted]}
        result=session.repair_requirements(requirements,request)
        self.assertEqual(result['facts'][1],source);self.assertNotIn('value',result['facts'][1])
        self.assertEqual(result['facts'][-1],converted)
        for row in (dict(source,value=8),dict(source,citations=[{}]),dict(source,unit=''),
                    dict(converted,unit='Hz'),dict(converted,value=7000),
                    {'id':'clock_hz','convert':{'fact':'clock'}},
                    {'id':'clock_hz','formula':'clock*1000','convert':{'fact':'clock','to_unit':'Hz'}}):
            with self.subTest(row=row):
                with self.assertRaises(ValueError):session.repair_requirements(requirements,dict(request,facts=[row]))

    def test_explicit_removal_never_deletes_or_rewrites_siblings_automatically(self):
        requirements,_=fixture();_,good=repaired(requirements)
        extra={'id':'unused','source':{'file':'spec.md','line':1,'number':99},'unit':'count'}
        raw=copy.deepcopy(good);raw['facts'].append(extra);before=copy.deepcopy(raw)
        request={'base_sha256':session.identity(raw),'facts':[],'remove_fact_ids':['unused']}
        result=session.repair_requirements(raw,request)
        self.assertEqual(result,good);self.assertEqual(raw,before)
        for removed in (['unknown'],['unused','unused'],['unused',1]):
            with self.assertRaises(ValueError):session.repair_requirements(raw,dict(request,remove_fact_ids=removed))
        with self.assertRaises(ValueError):session.repair_requirements(raw,dict(request,facts=[extra]))

    def test_removal_rejects_all_remaining_declared_dependency_kinds(self):
        requirements,_=fixture();_,good=repaired(requirements)
        cases=[]
        for dependent in ({'id':'dependent','formula':'width*2','unit':'bits','citations':[{'file':'spec.md','start':1,'end':1,'quote':'Width 8 bits.'}]},
                          {'id':'dependent','convert':{'fact':'width','to_unit':'bytes'}}):
            raw=copy.deepcopy(good);raw['facts'].append(dependent);cases.append(raw)
        raw=copy.deepcopy(good);raw['constraints'].append(dict(raw['constraints'][0],id='width_limit',limit='width'));cases.append(raw)
        for domain in ({'kind':'range','lower':'width','upper':'upper_bound'},
                       {'kind':'range','lower':'upper_bound','upper':'width'},
                       {'kind':'set','values':['width']}):
            raw=copy.deepcopy(good);raw['runtime_parameters']=[{'id':'runtime','domain':domain}];cases.append(raw)
        for key in ('min_depth','width_multiple','read_ports','write_ports','read_latency_cycles'):
            raw=copy.deepcopy(good);raw['storage_rules']=[{'kind':'SRAM',key:'max(width,1)'}];cases.append(raw)
        raw=copy.deepcopy(good);raw['storage_area_model']={'status':'supplied','density_fact':'width'};cases.append(raw)
        for raw in cases:
            before=copy.deepcopy(raw)
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(ValueError,'referenced facts'):session.repair_requirements(raw,{'base_sha256':session.identity(raw),'facts':[],'remove_fact_ids':['width']})
                self.assertEqual(raw,before)
        # An explicit model edit may remove an entire unused dependency branch;
        # the host does not invent replacement expressions or numeric values.
        branch=cases[0];changed=session.repair_requirements(branch,{'base_sha256':session.identity(branch),'facts':[],'remove_fact_ids':['width','dependent']})
        self.assertNotIn('width',{f['id'] for f in changed['facts']})
        self.assertNotIn('dependent',{f['id'] for f in changed['facts']})

    def test_selector_prompt_materialization_and_freeze_keep_raw_identity(self):
        raw,sources=selector_fixture();before=copy.deepcopy(raw);out=self.snapshot();messages=[]
        def invoke(label,role,prompt,parser):
            messages.append(prompt)
            return parser('<<<REQUIREMENTS>>>\n'+json.dumps(raw))
        result,errors=session.prepare(out,sources,invoke,0)
        self.assertEqual(errors,[]);self.assertEqual(raw,before)
        self.assertIn('FROZEN SOURCE QUANTITY SELECTORS',messages[0]);self.assertIn('both one based',messages[0])
        facts={f['id']:f for f in result['facts']}
        self.assertEqual(facts['width']['value'],8);self.assertEqual(facts['clock_hz']['value'],7000)
        self.assertEqual(facts['clock_hz']['unit'],'Hz');self.assertNotIn('source',facts['width'])
        self.assertNotEqual(session.identity(raw),session.identity(result))
        self.assertEqual(json.loads((out/'extract-0.declaration.json').read_text()),raw)
        self.assertEqual(json.loads((out/'extract-0.reply.json').read_text()),{'requirements':raw})
        self.assertEqual(json.loads((out/'extract-0.json').read_text()),result)
        receipt=json.loads((out/'extraction.receipt.json').read_text())
        self.assertEqual(receipt['declaration_sha256'],session.identity(raw))
        self.assertEqual(receipt['requirements_sha256'],session.identity(result))
        self.assertEqual(receipt['catalogue_sha256'],session.identity(json.loads((out/'source-quantities.json').read_text())))
        self.assertEqual(receipt['selector_index_base'],1)
        self.assertEqual(json.loads((out/'extraction.declaration.json').read_text()),raw)
        frozen=Mock(side_effect=AssertionError('accepted declaration must not cause another model call'))
        self.assertEqual(session.prepare(out,sources,frozen,2),(result,[]));self.assertEqual(frozen.call_count,0)

    def test_failed_binding_retains_raw_base_and_rejects_materialized_repair_hash(self):
        raw,sources=selector_fixture();raw['facts'][2]['source']['line']=0;out=self.snapshot();messages=[]
        fixed=copy.deepcopy(raw['facts'][2]);fixed['source']['line']=2
        def invoke(label,role,prompt,parser):
            messages.append(prompt)
            if label=='extract-0':return parser('<<<REQUIREMENTS>>>\n'+json.dumps(raw))
            if label=='extract-1':
                partial=json.loads((out/'extract-0.json').read_text())
                self.assertNotEqual(session.identity(partial),session.identity(raw))
                request={'base_sha256':session.identity(partial),'facts':[fixed]}
            else:
                self.assertIn('does not match the last unaccepted raw declaration',prompt)
                request={'base_sha256':session.identity(raw),'facts':[fixed]}
            self.assertIn('PREVIOUS RAW REQUIREMENT DECLARATION',prompt)
            self.assertIn('Actual raw base_sha256: '+session.identity(raw),prompt)
            return parser('<<<REQUIREMENTS_REPAIR>>>\n'+json.dumps(request))
        result,errors=session.prepare(out,sources,invoke,2)
        self.assertEqual(errors,[]);self.assertEqual(len(messages),3)
        self.assertEqual(json.loads((out/'extract-0.declaration.json').read_text()),raw)
        self.assertFalse(json.loads((out/'extract-0.check.json').read_text())['ok'])
        self.assertFalse(json.loads((out/'extract-1.check.json').read_text())['ok'])
        accepted=json.loads((out/'extraction.declaration.json').read_text())
        self.assertEqual(accepted['facts'][2],fixed)
        self.assertEqual(next(f['value'] for f in result['facts'] if f['id']=='width'),8)

    def test_failed_binding_recovery_does_not_repeat_model_or_resolve_raw_base(self):
        raw,sources=selector_fixture();raw['facts'][2]['source']['number']=0;out=self.snapshot()
        first=Mock(side_effect=[{'requirements':raw},RuntimeError('provider interrupted')])
        with self.assertRaises(RuntimeError):session.prepare(out,sources,first,2)
        declaration=(out/'extract-0.declaration.json').read_bytes();materialized=(out/'extract-0.json').read_bytes()
        fixed=copy.deepcopy(raw['facts'][2]);fixed['source']['number']=1
        request={'base_sha256':session.identity(raw),'facts':[fixed]}
        resumed=Mock(return_value={'repair':request});result,errors=session.prepare(out,sources,resumed,2)
        self.assertEqual(errors,[]);self.assertEqual(resumed.call_count,1);self.assertEqual(resumed.call_args.args[0],'extract-1')
        self.assertIn(session.identity(raw),resumed.call_args.args[2])
        self.assertEqual((out/'extract-0.declaration.json').read_bytes(),declaration)
        self.assertEqual((out/'extract-0.json').read_bytes(),materialized)
        self.assertEqual(next(f['value'] for f in result['facts'] if f['id']=='width'),8)

    def test_bad_source_or_conversion_remains_blocked_without_host_repair(self):
        raw,sources=selector_fixture()
        bad=[]
        for selector in ({'file':'spec.md','line':2,'number':0},{'file':'elsewhere.py','line':2,'number':1},
                         {'file':'spec.md','line':99,'number':1},{'file':'spec.md','line':2,'number':1,'value':8}):
            row=copy.deepcopy(raw);row['facts'][2]['source']=selector;bad.append(row)
        row=copy.deepcopy(raw);row['facts'][-1]['convert']['to_unit']='bits';bad.append(row)
        for row in bad:
            with self.subTest(row=row):
                out=self.snapshot();result,errors=session.prepare(out,sources,Mock(return_value={'requirements':row}),0)
                self.assertIsNone(result);self.assertTrue(errors);self.assertFalse((out/'extraction.json').exists())
                self.assertEqual(json.loads((out/'extract-0.declaration.json').read_text()),row)

    def test_bad_source_removal_is_explicit_and_checks_entire_retained_table(self):
        requirements,sources=fixture();_,good=repaired(requirements);bad=copy.deepcopy(good)
        bad['facts'].append({'id':'unused','source':{'file':'spec.md','line':1,'number':99},'unit':'count'})
        out=self.snapshot();request={'base_sha256':session.identity(bad),'facts':[],'remove_fact_ids':['unused']}
        result,errors=session.prepare(out,sources,Mock(side_effect=[{'requirements':bad},{'repair':request}]),1)
        self.assertEqual(errors,[]);self.assertEqual(result,self.materialized(out,good,sources))
        first=json.loads((out/'extract-0.declaration.json').read_text());self.assertIn('unused',{f['id'] for f in first['facts']})
        self.assertFalse(json.loads((out/'extract-0.check.json').read_text())['ok'])
        # Removing an irrelevant bad row cannot erase another real unit defect.
        bad['constraints'][0]['unit']='bits';request['base_sha256']=session.identity(bad);out=self.snapshot()
        result,errors=session.prepare(out,sources,Mock(side_effect=[{'requirements':bad},{'repair':request}]),1)
        self.assertIsNone(result);self.assertTrue(any('constraint unit' in error for error in errors))

    def test_accepted_catalogue_and_raw_declaration_tampering_are_rejected(self):
        raw,sources=selector_fixture();out=self.snapshot();result,errors=session.prepare(out,sources,Mock(return_value={'requirements':raw}),0)
        self.assertEqual(errors,[])
        saved=json.loads((out/'extraction.declaration.json').read_text());saved['facts'][2]['source']['number']=2
        (out/'extraction.declaration.json').write_text(json.dumps(saved))
        with self.assertRaisesRegex(ValueError,'identity differs'):session.prepare(out,sources,Mock(side_effect=AssertionError('no model')),2)
        (out/'extraction.declaration.json').write_text(json.dumps(raw))
        changed=dict(sources);changed['spec.md']=sources['spec.md'].replace('Clock 7 kHz','Clock 9 kHz')
        with self.assertRaisesRegex(ValueError,'source-quantities'):session.prepare(out,changed,Mock(side_effect=AssertionError('no model')),2)

    def test_raw_literal_and_materialized_conversion_records_are_rejected(self):
        raw,sources=selector_fixture();out=self.snapshot();canonical=self.materialized(out,raw,sources)
        quoted={'id':'width','value':8,'unit':'bits','citations':[{'file':'spec.md','start':2,'end':2,'quote':'Width 8 bits.'}]}
        for row in (quoted,next(f for f in canonical['facts'] if f['id']=='clock_hz')):
            with self.assertRaisesRegex(ValueError,'raw repaired fact'):session.repair_requirements(raw,{'base_sha256':session.identity(raw),'facts':[row]})
        for declaration in (canonical,dict(raw,facts=[quoted if f['id']=='width' else f for f in raw['facts']])):
            out=self.snapshot();result,errors=session.prepare(out,sources,Mock(return_value={'requirements':declaration}),0)
            self.assertIsNone(result);self.assertTrue(errors);self.assertFalse((out/'extraction.json').exists())
            self.assertEqual(json.loads((out/'extract-0.declaration.json').read_text()),declaration)


if __name__=='__main__':unittest.main()
