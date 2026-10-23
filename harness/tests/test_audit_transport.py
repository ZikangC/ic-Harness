"""Model engineering content stays unchanged; the host owns transport identity."""
import copy,json,unittest,importlib.util
from harness.prompts.architecture import audit as audit_prompt, tool_view
from unittest.mock import Mock
import test_audit as fixtures
from harness.runtime import audit_session as session

class HostTransportTests(unittest.TestCase):
    def setup_case(self):
        document,sources=fixtures.fixture();out=fixtures.snapshot(sources)
        budget=fixtures.engine.evaluate(document,sources,True)
        ctx=fixtures.context_fixture();report=fixtures.report_content_fixture(ctx)
        report.update(verdict='insufficient_evidence',observations=[])
        return out,document,sources,budget,report

    def test_actual_raw_response_preserved_and_identity_added_only_to_canonical(self):
        out,doc,sources,budget,report=self.setup_case();before=copy.deepcopy(report)
        raw='<<<AUDIT_REPORT>>>\n'+json.dumps(report,ensure_ascii=False)
        invoke=Mock(side_effect=lambda name,role,prompt,parser:parser(raw))
        reviewed,checked=session.run(out,'bound',sources,doc,budget,invoke,0)
        self.assertTrue(checked['ok'],checked['errors']);self.assertEqual(report,before)
        self.assertEqual((out/'bound.step-0.model.response.txt').read_text(),raw)
        self.assertEqual((out/'bound.step-0.parser.response.txt').read_text(),raw)
        decoded=json.loads((out/'bound.step-0.model.json').read_text())
        self.assertEqual(decoded,{'final':before});self.assertNotIn('packet_sha256',decoded['final'])
        binding=json.loads((out/'bound.step-0.binding.json').read_text())
        self.assertEqual(binding['raw_response_sha256'],session.digest(raw.encode()))
        self.assertEqual(binding['canonical_sha256'],session.report_identity(reviewed))
        canonical=copy.deepcopy(reviewed)
        for field in ('packet_sha256','baseline_sha256'):canonical.pop(field)
        self.assertEqual(canonical,before);self.assertEqual(invoke.call_count,1)
        replay=Mock(side_effect=AssertionError('a completed raw response must replay'))
        self.assertEqual(session.run(out,'bound',sources,doc,budget,replay,0)[0],reviewed)
        replay.assert_not_called()

    def test_adapter_cannot_bypass_raw_parser_capture(self):
        out,doc,sources,budget,report=self.setup_case()
        with self.assertRaisesRegex(session.TransportBindingError,'bypassed parser'):
            session.run(out,'bypass',sources,doc,budget,Mock(return_value={'final':report}),0)
        self.assertFalse((out/'bypass.step-0.canonical.json').exists())

    def test_first_and_final_parser_text_keep_serialization_provenance(self):
        out,doc,sources,budget,report=self.setup_case()
        raw='<<<AUDIT_REPORT>>>\n'+json.dumps(report).replace('"scope":','"scope" ',1)
        final='<<<AUDIT_REPORT>>>\n'+json.dumps(report)
        def invoke(name,role,prompt,parser):
            with self.assertRaises(json.JSONDecodeError):parser(raw)
            return parser(final)
        review,check=session.run(out,'normalized',sources,doc,budget,invoke,0)
        self.assertTrue(check['ok'],check['errors'])
        self.assertEqual((out/'normalized.step-0.model.response.txt').read_text(),raw)
        self.assertEqual((out/'normalized.step-0.parser.response.txt').read_text(),final)
        record=json.loads((out/'normalized.step-0.binding.json').read_text())
        self.assertNotEqual(record['raw_response_sha256'],record['parsed_response_sha256'])
        self.assertEqual(record['canonical_sha256'],session.report_identity(review))

    def test_model_identity_fields_rejected_without_silent_overwrite(self):
        for marker,body in (
            ('<<<AUDIT_REPORT>>>',{'packet_sha256':'echo','verdict':'accept'}),
            ('<<<AUDIT_REPAIR>>>',{'base_report_sha256':'echo','checks':[]}),
            ('<<<AUDIT_BASELINE>>>',{'baseline_sha256':'echo'}),
            ('<<<BASELINE_REPAIR>>>',{'base_sha256':'echo','reference_updates':[]}),
            ('<<<BASELINE_ANALYSIS_REPAIR>>>',{'base_sha256':'echo','analysis':{}})):
            parser=session.parse_baseline_turn if 'BASELINE' in marker else session.parse_turn
            with self.subTest(marker=marker),self.assertRaisesRegex(ValueError,'reserved host metadata'):
                parser(marker+'\n'+json.dumps(body))
        out,doc,sources,budget,report=self.setup_case();report['packet_sha256']='echo'
        called=fixtures.wire_provider(return_value={'final':report})
        with self.assertRaises(session.AuditSessionLimit):session.run(out,'echo',sources,doc,budget,called,0)
        self.assertEqual(called.call_count,2)
        self.assertFalse((out/'echo.step-0.canonical.json').exists())
        self.assertIn('reserved host metadata',(out/'echo.step-0.binding.json').read_text())

    def test_reference_repair_targets_host_current_version_and_preserves_body(self):
        out,doc,sources,budget,report=self.setup_case();bad=copy.deepcopy(report)
        bad['checks'][0]['references']=[];topic=bad['checks'][0]['topic']
        request={'reference_replacements':[{'collection':'checks','topic':topic,'references':report['checks'][0]['references']}]}
        model=fixtures.wire_provider(side_effect=[{'final':bad},{'report_repair':request}])
        reviewed,checked=session.run(out,'refs',sources,doc,budget,model,0,max_report_repairs=1)
        self.assertTrue(checked['ok'],checked['errors']);self.assertEqual(model.call_count,2)
        raw=json.loads((out/'refs.step-1.model.json').read_text());self.assertEqual(raw,{'report_repair':request})
        binding=json.loads((out/'refs.step-1.binding.json').read_text())
        previous=json.loads((out/'refs.step-0.canonical.json').read_text())
        self.assertEqual(binding['context']['target_sha256'],session.report_identity(previous))
        expected=copy.deepcopy(previous);expected['checks'][0]['references']=report['checks'][0]['references']
        self.assertEqual(reviewed,expected)
        self.assertNotIn('base_report_sha256',model.call_args.args[2].split('CURRENT STEP:')[-1])

    def test_raw_canonical_and_snapshot_tampering_refuses_replay(self):
        for altered in ('raw','canonical','snapshot','target'):
            with self.subTest(altered=altered):
                out,doc,sources,budget,report=self.setup_case()
                if altered=='target':
                    bad=copy.deepcopy(report);bad['checks'][0]['references']=[]
                    called=fixtures.wire_provider(side_effect=[{'final':bad},RuntimeError('interrupted')])
                    with self.assertRaises(RuntimeError):session.run(out,'replay',sources,doc,budget,called,0,max_report_repairs=1)
                else:session.run(out,'replay',sources,doc,budget,fixtures.wire_provider(return_value={'final':report}),0)
                if altered=='raw':(out/'replay.step-0.model.response.txt').write_text('changed raw')
                elif altered in ('canonical','target'):
                    path=out/'replay.step-0.canonical.json';value=json.loads(path.read_text());value['scope']='changed';path.write_text(json.dumps(value))
                else:
                    path=out/'snapshot/input/spec.md';path.write_text(path.read_text()+'\nchanged input')
                forbidden=Mock(side_effect=AssertionError('changed provenance cannot trigger a model'))
                with self.assertRaises(session.TransportBindingError):session.run(out,'replay',sources,doc,budget,forbidden,0,max_report_repairs=1)
                forbidden.assert_not_called()

    def test_changed_executed_recompute_cannot_be_replayed_as_observation(self):
        out,doc,sources,budget,report=self.setup_case()
        first=fixtures.wire_provider(side_effect=[{'tool_request':fixtures.arithmetic_request()},RuntimeError('interrupted')])
        with self.assertRaises(RuntimeError):session.run(out,'receipt',sources,doc,budget,first,1)
        result_path=out/'receipt.step-0.tool.result.json';value=json.loads(result_path.read_text())
        value['values']['reconstructed_bits']=257;result_path.write_text(json.dumps(value))
        forbidden=Mock(side_effect=AssertionError('changed executed receipt must stop before another model'))
        with self.assertRaisesRegex(session.TransportBindingError,'execution changed'):
            session.run(out,'receipt',sources,doc,budget,forbidden,1)
        forbidden.assert_not_called()

    def test_cached_baseline_cannot_self_assert_changed_quantitative_evidence(self):
        out,doc,sources,budget,report=self.setup_case()
        path=out/'audit-baseline.current.json';record=json.loads(path.read_text())
        record['analysis_result']['values']['forged_quantity']=123
        record['analysis_result_sha256']=session.report_identity(record['analysis_result'])
        path.write_text(json.dumps(record))
        forbidden=Mock(side_effect=AssertionError('cached fake evidence cannot trigger a model'))
        with self.assertRaisesRegex(session.TransportBindingError,'actual quantitative execution'):
            session.prepare_baseline(out,'changed',sources,forbidden,0)
        forbidden.assert_not_called()

    def test_partial_proposal_lists_only_bound_executed_child_values(self):
        ctx=fixtures.context_fixture();request=fixtures.proposal_request()
        bad=copy.deepcopy(request['model']['count_checks'][0]);bad.update(id='bad_count',source_paths=['unknown_source_path'])
        request['model']['count_checks'].append(bad)
        path=fixtures.BASE/'skills/microarchitecture-audit/scripts/candidate_checks.py'
        spec=importlib.util.spec_from_file_location('transport_partial_proposal',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        result=module.check(request['model'],ctx['source_analysis'],ctx['document'],ctx['budget'],ctx['sources'],fixtures.engine,
            source_declaration=ctx['baseline']['baseline']['analysis'],execution_dependencies=ctx['execution_dependencies'])
        self.assertFalse(result['ok']);self.assertFalse(result['coverage_complete'])
        result['request_id']='partial.step-0'
        verified=fixtures.validator.executed_values(result,ctx['sources'],ctx['source_analysis'],ctx['document'],ctx['budget'],ctx['execution_dependencies'])
        cid=next(cid for cid in verified['calculation_ids'] if cid.endswith('direct_work__source'))
        self.assertEqual(verified['values'][cid],80)
        self.assertFalse(any('bad_count' in cid for cid in verified['calculation_ids']))
        self.assertTrue(any(row['status']=='declaration_failed' for row in result['subreceipts']))
        prompt=audit_prompt('',ctx['sources'],history=[{'kind':'tool_result','request':request,'result':result,'executed_values':verified}])
        displayed=json.loads(prompt.split('VALID EXECUTED CALCULATION REFERENCES (copy exact ids and values): ',1)[1].split('\n',1)[0])
        self.assertEqual({row['calculation_id'] for row in displayed},set(verified['calculation_ids']))
        feedback=session.tool_feedback(request,result,verified)
        self.assertEqual(feedback['valid_calculation_ids'],verified['calculation_ids'])
        # A child's changed value cannot inherit its original binding/digest.
        forged=copy.deepcopy(result);row=next(r for r in forged['subreceipts'] if cid in r.get('values',{}))
        row['values'][cid]=81
        checked=fixtures.validator.executed_values(forged,ctx['sources'],ctx['source_analysis'],ctx['document'],ctx['budget'],ctx['execution_dependencies'])
        self.assertNotIn(cid,checked['values']);self.assertTrue(checked['errors'])

    def test_nonnumeric_failed_checks_are_listed_without_fabricated_observations(self):
        context=fixtures.context_fixture()
        failed={'id':'proposal_calculation','status':'fail','expected':[],
                'actual':['declared storage profile does not exist'],'topic':'resource_service'}
        child={'id':'input/frozen','status':'executed','checks':[failed],'values':{},'calculation_ids':[]}
        # This test covers prompt rendering after the separate binding verifier;
        # execution identity and hostile inputs have dedicated real-tool tests.
        verified={'values':{},'calculation_ids':[],'subreceipts':[child],'errors':[]}
        result={'request_id':'review.step-0','ok':True,'checks':[failed]}
        prompt=audit_prompt('',context['sources'],history=[{'kind':'tool_result','request':{'tool':'check_proposal'},
                                                          'result':result,'executed_values':verified}])
        references=json.loads(prompt.split('BINDING-VERIFIED EXECUTED CHECK REFERENCES (copy exact identities): ',1)[1].split('. A nonnumeric',1)[0])
        self.assertEqual(references,[{'request_id':'review.step-0','receipt_id':'input/frozen','check_id':'proposal_calculation','status':'fail'}])
        calculations=json.loads(prompt.split('VALID EXECUTED CALCULATION REFERENCES (copy exact ids and values): ',1)[1].split('\n',1)[0])
        self.assertEqual(calculations,[])
        self.assertIn('without inventing a number',prompt);self.assertIn('observations[] still requires an actual calculation_id',prompt)

    def test_actual_tool_view_keeps_good_and_failed_quantities_without_duplicate_proofs(self):
        from test_audit_candidate_checks import example, source_result, engine, audit as candidate
        for deficit in (False,True):
            with self.subTest(deficit=deficit):
                model,declaration,proposal,sources=example()
                if deficit:
                    proposal['candidates'][0]['traffic'][0]['payload_bytes']=1
                    model['operand_checks'][0]['issues'][0]['cycle']='bad-cycle'
                sources['design.json']=json.dumps(proposal,ensure_ascii=False,sort_keys=True)
                raw_source=source_result(declaration,sources)
                dependencies={kind+'_bundle_sha256':raw_source['binding'][kind+'_bundle_sha256'] for kind in ('checker','calculator')}
                budget=engine.evaluate(proposal,{key:sources[key] for key in ('spec.md','reference.py')})
                result=candidate.check(model,raw_source,proposal,budget,sources,engine,
                    source_declaration=declaration,execution_dependencies=dependencies)
                result['request_id']='real-tool-'+str(deficit)
                verified=fixtures.validator.executed_values(result,sources,raw_source,proposal,budget,dependencies)
                self.assertEqual(verified['errors'],[])
                request={'tool':'check_proposal','model':model}
                entry={'kind':'tool_result','request':request,'result':result,'executed_values':verified,
                       'feedback':session.tool_feedback(request,result,verified)}
                before=copy.deepcopy(entry);compact=tool_view(entry)
                self.assertEqual(entry,before);self.assertEqual(compact['request'],request)
                self.assertEqual(compact['result']['checks'],result['checks'])
                self.assertEqual(compact['result']['observations'],result['observations'])
                for key in ('errors','missing_profiles','missing_count_checks','missing_flow_checks','missing_source_declarations','coverage_complete'):
                    self.assertEqual(compact['result'][key],result[key])
                flow=next(row for row in compact['result']['checks'] if row['id'].startswith('flow__'))
                self.assertEqual((flow['expected'],flow['actual'],flow['status']),(64,8,'fail') if deficit else (64,64,'pass'))
                if deficit:
                    child=next(row for row in compact['result']['subreceipts'] if row['status']=='declaration_failed')
                    self.assertEqual(child['errors'],next(row for row in result['subreceipts'] if row['id']==child['id'])['errors'])
                rendered=json.dumps(compact)
                for key in ('checker_files_sha256','calculator_files_sha256','receipt_sha256','declaration_sha256','model_sha256'):
                    self.assertNotIn(key,rendered)
                self.assertLess(len(rendered),len(json.dumps(entry))/2)
                prompt=audit_prompt('',sources,history=[entry])
                actual=json.loads(prompt.split('VALID EXECUTED CALCULATION REFERENCES (copy exact ids and values): ',1)[1].split('\n',1)[0])
                self.assertEqual({row['calculation_id']:row['actual'] for row in actual},verified['values'])

    def test_lookup_quotes_and_recompute_comparisons_survive_tool_compaction(self):
        context=fixtures.context_fixture()
        lookup=fixtures.validator.lookup({'tool':'lookup_source','queries':[{'file':'spec.md','query':'area 3'}]},context['sources'])
        lookup['request_id']='lookup'
        compact=tool_view({'kind':'tool_result','request':{'tool':'lookup_source'},'result':lookup})
        self.assertEqual(compact['result'],lookup)
        request=fixtures.arithmetic_request();request['calculations'][0]['expected']=128
        recompute=fixtures.validator.recompute(request,context['sources'],fixtures.engine)
        recompute['request_id']='math'
        compact=tool_view({'kind':'tool_result','request':request,'result':recompute})
        for key in ('comparisons','assumptions','scope','errors'):
            self.assertEqual(compact['result'][key],recompute[key])
        self.assertFalse(compact['result']['comparisons']['reconstructed_bits']['matches_expected'])
        self.assertIn('comparison_evidence',recompute)
        self.assertNotIn('comparison_evidence',compact['result'])
        self.assertEqual(recompute['comparison_evidence']['source_sha256s'],
                         {name:fixtures.validator.hashlib.sha256(text.encode()).hexdigest()
                          for name,text in context['sources'].items()})

    def transformed_request(self,operations=80,cost=True):
        request=fixtures.proposal_request();row=request['model']['count_checks'][0]
        row.update(realization='transformed',equivalence_evidence='Independent synthetic effect interpretation; protocol tests do not prove equivalence.')
        if cost:row['physical_realization']={'basis':'independent resource recipe','groups':[{'cost_group':'compute',
            'steps':[{'id':'effects','kind':'elementwise','shape':[operations],'ops_per_element':1}],
            'scope':{'kind':'task','axis':'run'},'basis':'complete synthetic task effects and native cost',
            'references':[{'file':'spec.md','quote':'items 12'},{'file':'design.json','quote':'"id": "small"'}]}]}
        return request

    def transformation_report(self,out,label,sources,doc,budget,tool_step):
        ctx=fixtures.packet(sources,doc,budget)
        receipt=json.loads((out/(label+'.step-'+str(tool_step)+'.tool.result.json')).read_text())
        receipt['request_id']=label+'.step-'+str(tool_step);ctx['calculations']=[receipt];ctx['proposal_checks']=[receipt]
        report=fixtures.report_content_fixture(ctx)
        physical_id=next((cid for cid in receipt['calculation_ids'] if '__physical__' in cid),None)
        refs=[{'file':'spec.md','quote':'items 12'},{'file':'design.json','quote':'"id": "small"'}]
        if physical_id is not None:refs.append({'request_id':receipt['request_id'],'calculation_id':physical_id,'actual':receipt['values'][physical_id]})
        report['realization_reviews']=[{'count_check_id':'direct_work','status':'reviewed',
            'evidence':'Independent synthetic correspondence review; actual semantic proof is outside this fixture.','references':refs}]
        return report

    def test_missing_transformation_review_reopens_judgment_without_extra_tools(self):
        for mode in ('missing','unverified'):
            with self.subTest(mode=mode):
                out,doc,sources,budget,_=self.setup_case();request=self.transformed_request()
                def reply(name,role,prompt,parser):
                    if name.endswith('step-0'):return {'tool_request':request}
                    report=self.transformation_report(out,'transform',sources,doc,budget,0)
                    if name.endswith('step-1'):
                        if mode=='missing':report.pop('realization_reviews')
                        else:report['realization_reviews'][0]['status']='unverified'
                    else:
                        self.assertIn('audit_evidence_reopen',prompt)
                        self.assertNotIn('CURRENT STEP: return ONLY <<<AUDIT_REPAIR>>>',prompt)
                    return {'final':report}
                model=fixtures.wire_provider(callback=reply)
                report,checked=session.run(out,'transform',sources,doc,budget,model,1)
                self.assertTrue(checked['ok'],checked['errors']);self.assertEqual(model.call_count,3)
                self.assertEqual(report['realization_reviews'][0]['status'],'reviewed')
                self.assertEqual(len(list(out.glob('*.tool.execution.json'))),1)

    def test_missing_native_cost_can_acquire_typed_evidence_with_original_tool_allowance(self):
        out,doc,sources,budget,_=self.setup_case()
        def reply(name,role,prompt,parser):
            if name.endswith('step-0'):return {'tool_request':self.transformed_request(cost=False)}
            if name.endswith('step-2'):
                self.assertIn('audit_evidence_reopen',prompt)
                return {'tool_request':self.transformed_request()}
            tool_step=0 if name.endswith('step-1') else 2
            return {'final':self.transformation_report(out,'typed',sources,doc,budget,tool_step)}
        model=fixtures.wire_provider(callback=reply)
        report,checked=session.run(out,'typed',sources,doc,budget,model,2)
        self.assertTrue(checked['ok'],checked['errors']);self.assertEqual(model.call_count,4)
        self.assertEqual(len(list(out.glob('*.tool.execution.json'))),2)

    def test_executed_native_cost_failure_cannot_reopen_accept_judgment(self):
        out,doc,sources,budget,_=self.setup_case()
        def reply(name,role,prompt,parser):
            if name.endswith('step-0'):return {'tool_request':self.transformed_request(operations=96)}
            report=self.transformation_report(out,'physical',sources,doc,budget,0)
            if name.endswith('step-2'):
                self.assertIn('CURRENT STEP: return ONLY <<<AUDIT_REPAIR>>>',prompt)
                self.assertNotIn('audit_evidence_reopen',prompt)
            return {'final':report}
        model=fixtures.wire_provider(callback=reply)
        with self.assertRaises(session.AuditSessionLimit):
            session.run(out,'physical',sources,doc,budget,model,1,max_report_repairs=1)
        self.assertEqual(model.call_count,3)
        checked=json.loads((out/'physical.step-1.report-check.result.json').read_text())
        self.assertTrue(any(e.startswith('accept contradicts an executed proposal') for e in checked['errors']))
        self.assertFalse((out/'physical.json').exists())
        self.assertEqual(len(list(out.glob('*.tool.execution.json'))),1)

    def test_earlier_executed_counterexample_requires_new_explicit_judgment(self):
        out,doc,sources,budget,_=self.setup_case()
        def reply(name,role,prompt,parser):
            if name.endswith('step-0'):return {'tool_request':self.transformed_request(operations=96)}
            if name.endswith('step-1'):return {'tool_request':self.transformed_request()}
            report=self.transformation_report(out,'counterexample',sources,doc,budget,1)
            if name.endswith('step-3'):
                self.assertIn('audit_evidence_reopen',prompt)
                self.assertNotIn('CURRENT STEP: return ONLY <<<AUDIT_REPAIR>>>',prompt)
                prior=json.loads((out/'counterexample.step-0.tool.result.json').read_text())
                later=json.loads((out/'counterexample.step-1.tool.result.json').read_text())
                report['counterexample_reviews']=[]
                row=next(row for row in report['checks'] if row['topic']=='cycle_accounting')
                row['evidence']='The earlier independent recipe counted 96 effects and exceeded the declared 80-effect cost. Re-reading the same synthetic task supports the corrected 80-effect auditor mapping; the source and candidate were not changed. This is a model-authored interpretation, not a host proof.'
                cited=0
                for receipt in prior['subreceipts']:
                    for check in receipt['checks']:
                        if check.get('status')!='fail':continue
                        keys=[receipt['id']+'::'+check['id']+'__'+suffix for suffix in ('actual','expected')]
                        key=next(key for key in keys if key in receipt['calculation_ids'])
                        failed_ref={'request_id':'counterexample.step-0','calculation_id':key,'actual':receipt['values'][key]}
                        row['references'].append(failed_ref);cited+=1
                        corrected=next(child for child in later['subreceipts'] if any(value['id']==check['id'] and value['status']=='pass' for value in child['checks']))
                        report['counterexample_reviews'].append({'request_id':'counterexample.step-0','receipt_id':receipt['id'],
                            'check_id':check['id'],'disposition':'audit_declaration_error','reason':row['evidence'],
                            'correction_check':{'request_id':'counterexample.step-1','receipt_id':corrected['id'],'check_id':check['id']},
                            'references':[{'file':'spec.md','quote':'items 12'},{'file':'design.json','quote':'"id": "small"'},failed_ref]})
                self.assertGreater(cited,0)
            return {'final':report}
        model=fixtures.wire_provider(callback=reply)
        report,checked=session.run(out,'counterexample',sources,doc,budget,model,2)
        self.assertTrue(checked['ok'],checked['errors']);self.assertEqual(model.call_count,4)
        rejected=json.loads((out/'counterexample.step-2.report-check.result.json').read_text())
        self.assertTrue(any(e.startswith('counterexample requires explicit adjudication:') for e in rejected['errors']))
        self.assertEqual(len(list(out.glob('*.tool.execution.json'))),2)
        self.assertIn('earlier independent recipe',next(row for row in report['checks'] if row['topic']=='cycle_accounting')['evidence'])

    def test_wrong_counterexample_disposition_reopens_same_packet_for_source_correction(self):
        document,sources=fixtures.fixture();original=copy.deepcopy(document)
        sources['spec.md']+='\neffects=80\n'
        sources['reference.py']='def reference(xs):\n    assert len(xs) == 80\n    return [value + 1 for value in xs]\n'
        out=fixtures.snapshot(sources);(out/'audit-baseline.current.json').unlink()
        baseline=fixtures.baseline_fixture(sources)
        baseline['analysis']['computations'][0]['steps'][0]['shape']=[96]
        budget=fixtures.engine.evaluate(document,sources,True)
        emitted=[]
        def reply(name,role,prompt,parser):
            if name.endswith('baseline-0'):return baseline
            if name.endswith('step-0'):return {'tool_request':fixtures.proposal_request()}
            context=json.loads((out/'reinterpret.packet.json').read_text())
            receipt=json.loads((out/'reinterpret.step-0.tool.result.json').read_text());receipt['request_id']='reinterpret.step-0'
            context['calculations']=[receipt];context['proposal_checks']=[receipt]
            report=fixtures.report_content_fixture(context)
            report.update(verdict='insufficient_evidence',scope='A source interpretation is pending correction and execution; this mechanical transport test does not approve the architecture.')
            child=next(row for row in receipt['subreceipts'] if any(check['status']=='fail' for check in row['checks']))
            failure=next(check for check in child['checks'] if check['status']=='fail')
            self.assertEqual((failure['expected'],failure['actual']),(96,80))
            refs=[{'file':'spec.md','start':2,'end':2},{'file':'reference.py','start':1,'end':3},
                  {'file':'design.json','quote':'"id": "small"'}]
            disposition={'request_id':receipt['request_id'],'receipt_id':child['id'],'check_id':failure['id'],
                         'disposition':'audit_declaration_error','reason':'Initial classification incorrectly treats a source-path shape as an audit mapping error.',
                         'correction_check':{'request_id':receipt['request_id'],'receipt_id':child['id'],'check_id':failure['id']},
                         'references':refs}
            if name.endswith('step-2'):
                self.assertIn('audit_evidence_reopen',prompt);self.assertNotIn('CURRENT STEP: return ONLY <<<AUDIT_REPAIR>>>',prompt)
                disposition.pop('correction_check')
                disposition.update(disposition='source_interpretation_error',baseline_obligation_id='independent-work',
                    reason='The original fixed input and reference require 80 elementwise additions. The baseline shape of 96 is an interpretation error; the corrected source witness has not yet executed.')
                report['baseline_corrections']=[{'obligation_id':'independent-work','correction':'Use the 80-element source shape for scalar_work.',
                    'reason':'Both original inputs define the 80-element complete-task addition; 96 was an auditor interpretation.',
                    'analysis_targets':[{'section':'computations','id':'scalar_work','workload':'primary'}],
                    'references':refs[:2]}]
            report['counterexample_reviews']=[disposition];emitted.append(copy.deepcopy(report))
            return {'final':report}
        model=fixtures.wire_provider(callback=reply)
        review,checked=session.run(out,'reinterpret',sources,document,budget,model,1,max_baseline_repairs=0,max_report_repairs=1)
        self.assertTrue(checked['ok'],checked['errors']);self.assertEqual(review['verdict'],'insufficient_evidence')
        self.assertEqual(model.call_count,4);self.assertEqual(document,original)
        old=json.loads((out/'reinterpret.step-1.model.json').read_text())['final']
        self.assertEqual(old,emitted[0]);self.assertEqual(old['counterexample_reviews'][0]['disposition'],'audit_declaration_error')
        self.assertEqual(review['counterexample_reviews'][0]['disposition'],'source_interpretation_error')
        validation=json.loads((out/'reinterpret.step-1.report-check.result.json').read_text())
        self.assertTrue(any(error.startswith('counterexample/') and 'actual passing check' in error for error in validation['errors']))
        self.assertEqual(len(list(out.glob('*.tool.execution.json'))),1)
        frame=json.loads((out/'reinterpret.step-2.binding.json').read_text())['context']
        self.assertIsNone(frame['target_sha256']);self.assertEqual(frame['packet_sha256'],review['packet_sha256'])
        self.assertTrue((out/'audit-baseline.invalidated.json').exists())

    def test_invalid_requirement_status_reopens_model_judgment_without_host_fix(self):
        for collection in ('requirements','checks'):
            for mode in ('missing','invalid'):
                with self.subTest(collection=collection,mode=mode):
                    out,doc,sources,budget,_=self.setup_case()
                    def reply(name,role,prompt,parser):
                        if name.endswith('step-0'):return {'tool_request':fixtures.proposal_request()}
                        ctx=fixtures.packet(sources,doc,budget)
                        receipt=json.loads((out/'enum.step-0.tool.result.json').read_text());receipt['request_id']='enum.step-0'
                        ctx['calculations']=[receipt];ctx['proposal_checks']=[receipt]
                        report=fixtures.report_content_fixture(ctx)
                        if name.endswith('step-1'):
                            if mode=='missing':report[collection][0].pop('status')
                            else:report[collection][0]['status']='invalid_model_enum'
                        else:
                            self.assertIn('audit_evidence_reopen',prompt)
                            self.assertNotIn('CURRENT STEP: return ONLY <<<AUDIT_REPAIR>>>',prompt)
                        return {'final':report}
                    model=fixtures.wire_provider(callback=reply)
                    report,checked=session.run(out,'enum',sources,doc,budget,model,1)
                    self.assertTrue(checked['ok'],checked['errors']);self.assertEqual(model.call_count,3)
                    raw=json.loads((out/'enum.step-1.model.json').read_text())
                    old=json.loads((out/'enum.step-1.canonical.json').read_text())
                    rejected=raw['final'][collection][0]
                    self.assertEqual(old[collection][0],rejected)
                    if mode=='missing':self.assertNotIn('status',rejected)
                    else:self.assertEqual(rejected['status'],'invalid_model_enum')
                    self.assertEqual(report[collection][0]['status'],'covered' if collection=='requirements' else 'conditional')
                    self.assertEqual(len(list(out.glob('*.tool.execution.json'))),1)
                    target=json.loads((out/'enum.step-2.binding.json').read_text())['context']
                    self.assertIsNone(target['target_sha256'])

    def test_repeated_invalid_status_uses_original_response_cap(self):
        out,doc,sources,budget,_=self.setup_case()
        def reply(name,role,prompt,parser):
            if name.endswith('step-0'):return {'tool_request':fixtures.proposal_request()}
            ctx=fixtures.packet(sources,doc,budget)
            receipt=json.loads((out/'enumcap.step-0.tool.result.json').read_text());receipt['request_id']='enumcap.step-0'
            ctx['calculations']=[receipt];ctx['proposal_checks']=[receipt]
            report=fixtures.report_content_fixture(ctx);report['requirements'][0]['status']='invalid_model_enum'
            return {'final':report}
        model=fixtures.wire_provider(callback=reply)
        with self.assertRaises(session.AuditSessionLimit):session.run(out,'enumcap',sources,doc,budget,model,1,max_report_repairs=1)
        self.assertEqual(model.call_count,5) # 2*1 + 2 + the existing one repair response
        self.assertEqual(len(list(out.glob('*.tool.execution.json'))),1)
        self.assertFalse((out/'enumcap.json').exists())

    def test_invalid_unverified_kind_reopens_revision_judgment_without_host_fix(self):
        out,doc,sources,budget,_=self.setup_case();original=copy.deepcopy(doc);emitted=[]
        def reply(name,role,prompt,parser):
            if name.endswith('step-0'):return {'tool_request':fixtures.proposal_request()}
            ctx=json.loads((out/'kind.packet.json').read_text())
            receipt=json.loads((out/'kind.step-0.tool.result.json').read_text());receipt['request_id']='kind.step-0'
            ctx['calculations']=[receipt];ctx['proposal_checks']=[receipt]
            report=fixtures.report_content_fixture(ctx);report['verdict']='revise'
            report['findings']=[{'id':'synthetic-design-gap','category':'design','severity':'blocking','disposition':'open',
                'location':'synthetic implementation contract','issue':'A necessary fixture design change remains open.',
                'evidence':'Mechanical routing fixture only; the host must preserve this model design judgment.',
                'required_change':'Close the model-defined necessary design issue before delivery.',
                'references':copy.deepcopy(report['checks'][0]['references'])}]
            if name.endswith('step-1'):report['unverified'][0]['kind']='external'
            else:
                self.assertIn('audit_evidence_reopen',prompt)
                self.assertNotIn('CURRENT STEP: return ONLY <<<AUDIT_REPAIR>>>',prompt)
            emitted.append(copy.deepcopy(report));return {'final':report}
        model=fixtures.wire_provider(callback=reply)
        review,checked=session.run(out,'kind',sources,doc,budget,model,1,max_report_repairs=0)
        self.assertTrue(checked['ok'],checked['errors']);self.assertEqual(review['verdict'],'revise')
        self.assertEqual(model.call_count,3);self.assertEqual(doc,original)
        raw=json.loads((out/'kind.step-1.model.json').read_text())['final']
        old=json.loads((out/'kind.step-1.canonical.json').read_text())
        self.assertEqual(raw,emitted[0]);self.assertEqual(old['unverified'][0]['kind'],'external')
        self.assertEqual(review['unverified'][0]['kind'],'implementation_evidence')
        self.assertEqual(review['findings'],old['findings'])
        self.assertEqual(old['packet_sha256'],review['packet_sha256'])
        self.assertEqual(old['baseline_sha256'],review['baseline_sha256'])
        self.assertEqual(len(list(out.glob('*.tool.execution.json'))),1)
        self.assertFalse((out/'kind.step-2.repair.json').exists())
        frame=json.loads((out/'kind.step-2.binding.json').read_text())['context']
        self.assertIsNone(frame['target_sha256']);self.assertEqual(frame['packet_sha256'],review['packet_sha256'])

    def test_repeated_invalid_unverified_kind_uses_original_response_cap(self):
        out,doc,sources,budget,report=self.setup_case();report['unverified'][0]['kind']='external'
        model=fixtures.wire_provider(return_value={'final':report})
        with self.assertRaisesRegex(session.AuditSessionLimit,'bounded audit responses exhausted'):
            session.run(out,'kindcap',sources,doc,budget,model,0,max_report_repairs=1)
        self.assertEqual(model.call_count,3) # Original 2*0 + 2 + 1 total response allowance.
        self.assertEqual(len(list(out.glob('kindcap.step-*.report-check.result.json'))),3)
        self.assertFalse((out/'kindcap.json').exists());self.assertFalse(list(out.glob('*.repair.json')))

    def test_invalid_unverified_kind_does_not_hide_forged_observation(self):
        out,doc,sources,budget,_=self.setup_case()
        def reply(name,role,prompt,parser):
            if name.endswith('step-0'):return {'tool_request':fixtures.proposal_request()}
            ctx=fixtures.packet(sources,doc,budget)
            receipt=json.loads((out/'unsafekind.step-0.tool.result.json').read_text());receipt['request_id']='unsafekind.step-0'
            ctx['calculations']=[receipt];ctx['proposal_checks']=[receipt]
            report=fixtures.report_content_fixture(ctx);report['unverified'][0]['kind']='external'
            report['observations'][0]['actual']+=1;return {'final':report}
        model=fixtures.wire_provider(callback=reply)
        with self.assertRaisesRegex(session.AuditReportRejected,'unsafe evidence'):
            session.run(out,'unsafekind',sources,doc,budget,model,1)
        self.assertEqual(model.call_count,2);self.assertFalse((out/'unsafekind.json').exists())
        checked=json.loads((out/'unsafekind.step-1.report-check.result.json').read_text())
        self.assertTrue(any('calculation reference' in error for error in checked['errors']))

    def test_invalid_check_status_does_not_hide_forged_observation(self):
        out,doc,sources,budget,_=self.setup_case()
        def reply(name,role,prompt,parser):
            if name.endswith('step-0'):return {'tool_request':fixtures.proposal_request()}
            ctx=fixtures.packet(sources,doc,budget)
            receipt=json.loads((out/'unsafeenum.step-0.tool.result.json').read_text());receipt['request_id']='unsafeenum.step-0'
            ctx['calculations']=[receipt];ctx['proposal_checks']=[receipt]
            report=fixtures.report_content_fixture(ctx);report['checks'][0]['status']='invalid_model_enum'
            report['observations'][0]['actual']+=1
            return {'final':report}
        model=fixtures.wire_provider(callback=reply)
        with self.assertRaisesRegex(session.AuditReportRejected,'unsafe evidence'):
            session.run(out,'unsafeenum',sources,doc,budget,model,1)
        self.assertEqual(model.call_count,2)
        checked=json.loads((out/'unsafeenum.step-1.report-check.result.json').read_text())
        self.assertTrue(any('calculation reference' in e for e in checked['errors']))
        self.assertFalse((out/'unsafeenum.json').exists())

    def test_received_invalid_response_survives_interrupted_serialization_repair(self):
        out,doc,sources,budget,report=self.setup_case()
        raw='<<<AUDIT_REPORT>>>\n{"scope" bad}'
        def interrupted(name,role,prompt,parser):
            try:parser(raw)
            except json.JSONDecodeError:raise RuntimeError('format provider interrupted')
        with self.assertRaises(RuntimeError):session.run(out,'interrupted',sources,doc,budget,interrupted,0)
        self.assertTrue((out/'interrupted.step-0.binding.json').exists())
        resumed=fixtures.wire_provider(return_value={'final':report})
        reviewed,checked=session.run(out,'interrupted',sources,doc,budget,resumed,0)
        self.assertTrue(checked['ok'],checked['errors']);self.assertEqual(resumed.call_count,1)
        self.assertEqual(resumed.call_args.args[0],'interrupted.step-1')
        self.assertEqual((out/'interrupted.step-0.model.response.txt').read_text(),raw)

if __name__=='__main__':unittest.main()
