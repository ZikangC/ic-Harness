import copy,json,unittest,importlib.util
from unittest.mock import Mock
from test_audit import snapshot,baseline_fixture,wire_provider,wire_reply
from test_audit_source_model import witness
from harness.runtime import audit_session as session

class SourceAnalysisRepairTests(unittest.TestCase):
    def source_coverage_fixture(self):
        """Mechanical source identity fixture, not a chip acceptance claim."""
        import test_audit as audit
        sources={'spec.md':'items 12','reference.py':'factor = 3'}
        baseline=baseline_fixture(sources);refs=[{'file':'spec.md','start':1,'end':1}]
        baseline['obligations'][0]['references']=copy.deepcopy(refs)
        baseline['analysis']['facts']=[{'id':'factor','source':{'file':'reference.py','line':1,'number':1},'unit':'count'}]
        baseline['analysis']['workloads'][0]['references']=copy.deepcopy(refs)
        computation=baseline['analysis']['computations'][0]
        computation['references']=copy.deepcopy(refs);computation['steps'][0]['shape']=['factor']
        spec=importlib.util.spec_from_file_location('coverage_source',audit.BASE/'skills/microarchitecture-audit/scripts/source_model.py')
        source=importlib.util.module_from_spec(spec);spec.loader.exec_module(source)
        dependencies={'checker_bundle_sha256':session.script_bundle_hash(audit.BASE/'skills/microarchitecture-audit/scripts'),
                      'calculator_bundle_sha256':session.script_bundle_hash(audit.BASE/'skills/microarchitecture-design/scripts')}
        return baseline,sources,source,audit,dependencies

    def test_verified_analysis_source_counts_without_duplicate_obligation_reference(self):
        baseline,sources,source,audit,deps=self.source_coverage_fixture();before=copy.deepcopy(baseline)
        raw=source.analyze(baseline['analysis'],sources,audit.engine)
        checked=audit.validator.validate_baseline(baseline,sources,source_analysis=raw,execution_dependencies=deps)
        self.assertTrue(checked['ok'],checked['errors']);self.assertEqual(checked['input_evidence_files'],['reference.py','spec.md'])
        self.assertEqual(raw['computations']['scalar_work']['totals']['primary'],3)
        self.assertEqual(baseline,before)
        invalid=copy.deepcopy(baseline['analysis']['computations'][0]);invalid.update(id='independent_failure',steps=[{'id':'bad','kind':'dot','width':-1}])
        baseline['analysis']['computations'].append(invalid)
        raw=source.analyze(baseline['analysis'],sources,audit.engine)
        checked=audit.validator.validate_baseline(baseline,sources,source_analysis=raw,execution_dependencies=deps)
        self.assertTrue(checked['ok'],checked['errors']);self.assertFalse(raw['ok'])
        self.assertFalse(audit.validator.executed_source_values(raw,sources,baseline['analysis'],deps)['coverage_complete'])

    def test_failed_unbound_and_annotation_source_references_do_not_count(self):
        baseline,sources,source,audit,deps=self.source_coverage_fixture()
        for kind in ('failed','unbound','annotation','stale_dependency'):
            with self.subTest(kind=kind):
                current=copy.deepcopy(baseline);trusted=copy.deepcopy(deps)
                if kind=='failed':current['analysis']['facts'][0]['source']['line']=99
                if kind=='annotation':
                    current['analysis']['facts']=[];current['analysis']['computations'][0]['steps'][0]['shape']=[3]
                    current['analysis']['annotation']={'references':[{'file':'reference.py','start':1,'end':1}]}
                raw=source.analyze(current['analysis'],sources,audit.engine)
                if kind=='unbound':raw['model_sha256']='self-declared'
                if kind=='stale_dependency':trusted['checker_bundle_sha256']='stale'
                checked=audit.validator.validate_baseline(current,sources,source_analysis=raw,execution_dependencies=trusted)
                self.assertFalse(checked['ok']);self.assertNotIn('reference.py',checked['input_evidence_files'])

    def test_final_report_preserves_verified_analysis_only_source_coverage(self):
        import test_audit as audit
        document,sources=audit.fixture();sources['reference.py']='factor = 80\n'
        baseline=baseline_fixture(sources);refs=[{'file':'spec.md','start':1,'end':1}]
        baseline['obligations'][0]['references']=copy.deepcopy(refs)
        baseline['analysis']['facts']=[{'id':'factor','source':{'file':'reference.py','line':1,'number':1},'unit':'count'}]
        baseline['analysis']['workloads'][0]['references']=copy.deepcopy(refs)
        computation=baseline['analysis']['computations'][0]
        computation['references']=copy.deepcopy(refs);computation['steps'][0]['shape']=['factor']
        spec=importlib.util.spec_from_file_location('final_coverage_source',audit.BASE/'skills/microarchitecture-audit/scripts/source_model.py')
        source=importlib.util.module_from_spec(spec);spec.loader.exec_module(source)
        raw=source.analyze(baseline['analysis'],sources,audit.engine)
        self.assertTrue(raw['ok'],raw['errors']);self.assertEqual(raw['computations']['scalar_work']['totals']['primary'],80)
        record={'baseline':baseline,'baseline_sha256':session.report_identity(baseline),'analysis_result':raw,
                'audit_script_bundle_sha256':session.script_bundle_hash(audit.BASE/'skills/microarchitecture-audit/scripts'),
                'calculator_bundle_sha256':session.script_bundle_hash(audit.BASE/'skills/microarchitecture-design/scripts')}
        dependencies={'checker_bundle_sha256':record['audit_script_bundle_sha256'],'calculator_bundle_sha256':record['calculator_bundle_sha256']}
        record['source_execution']=audit.validator.executed_source_values(raw,sources,baseline['analysis'],dependencies)
        self.assertTrue(record['source_execution']['coverage_complete'],record['source_execution']['errors'])
        context=session.packet(sources,document,audit.engine.evaluate(document,sources,True),baseline=record)
        result=audit.validator.recompute(audit.arithmetic_request(),context['sources'],audit.engine)
        result['request_id']='final-coverage.step-0';context['calculations']=[result]
        context['proposal_checks']=[audit.executed_proposal_check(context)]
        report=audit.report_fixture(context);before=copy.deepcopy((context,report))
        checked=audit.validator.validate(report,context)
        self.assertTrue(checked['ok'],checked['errors']);self.assertEqual((context,report),before)
        for kind in ('receipt','dependency'):
            with self.subTest(kind=kind):
                changed=copy.deepcopy(context)
                if kind=='receipt':changed['source_analysis']['subreceipts'][0]['receipt_sha256']='forged'
                else:changed['execution_dependencies']['checker_bundle_sha256']='stale'
                checked=audit.validator.validate(report,changed)
                self.assertFalse(checked['ok']);self.assertTrue(any('baseline source evidence:' in error for error in checked['errors']))

    def test_same_run_partial_baseline_is_handed_off_without_another_source_call(self):
        import test_audit as audit
        document,sources=audit.fixture();out=snapshot(sources);(out/'audit-baseline.current.json').unlink()
        baseline=baseline_fixture(sources)
        failed=copy.deepcopy(baseline['analysis']['computations'][0])
        failed.update(id='unresolved_path',steps=[{'id':'bad','kind':'dot','width':-1}])
        baseline['analysis']['computations'].append(failed);calls=[]
        def invoke(name,role,prompt,parser):
            calls.append(name)
            if name.endswith('baseline-0'):return wire_reply(baseline,parser)
            if name.endswith('step-0'):return wire_reply({'tool_request':audit.proposal_request()},parser)
            receipt=json.loads((out/'handoff.step-0.tool.result.json').read_text());receipt['request_id']='handoff.step-0'
            context=json.loads((out/'handoff.packet.json').read_text());context['calculations']=[receipt];context['proposal_checks']=[receipt]
            report=audit.report_content_fixture(context);report['verdict']='insufficient_evidence'
            report['scope']='Valid source transactions reviewed; an unresolved independent path prevents acceptance.'
            accept=audit.report_fixture(context)
            self.assertFalse(audit.validator.validate(accept,context)['ok'])
            return wire_reply({'final':report},parser)
        prepared=session.prepare_baseline(out,'early',sources,invoke,0);before=copy.deepcopy(prepared)
        review,checked=session.run(out,'handoff',sources,document,audit.engine.evaluate(document,sources,True),invoke,1,
                                   max_baseline_repairs=0,prepared_baseline=prepared)
        self.assertTrue(checked['ok'],checked['errors']);self.assertEqual(review['verdict'],'insufficient_evidence')
        self.assertEqual([name for name in calls if 'baseline-' in name],['early.baseline-0'])
        self.assertEqual(prepared,before);self.assertFalse((out/'audit-baseline.current.json').exists())
        self.assertTrue((out/'handoff.baseline-handoff.json').exists())

    def test_prepared_partial_record_refuses_changed_raw_result_or_snapshot(self):
        import test_audit as audit
        for kind in ('raw_response','source_result','snapshot'):
            with self.subTest(kind=kind):
                document,sources=audit.fixture();out=snapshot(sources);(out/'audit-baseline.current.json').unlink()
                baseline=baseline_fixture(sources)
                failed=copy.deepcopy(baseline['analysis']['computations'][0]);failed.update(id='bad',steps=[{'id':'bad','kind':'dot','width':-1}])
                baseline['analysis']['computations'].append(failed)
                prepared=session.prepare_baseline(out,'early',sources,wire_provider(return_value=baseline),0)
                if kind=='raw_response':(out/'early.baseline-0.model.response.txt').write_text('changed raw response')
                elif kind=='snapshot':(out/'snapshot/harness/skills/microarchitecture-design/scripts/evaluate.py').write_text('changed closure')
                else:prepared['analysis_result']['values']['invented']=999
                invoke=Mock(side_effect=AssertionError('changed evidence cannot invoke a model'))
                with self.assertRaises(session.TransportBindingError):
                    session.run(out,'handoff',sources,document,audit.engine.evaluate(document,sources,True),invoke,1,prepared_baseline=prepared)
                invoke.assert_not_called()

    def test_failed_quantitative_witness_can_only_support_a_non_accept_report(self):
        model,sources=witness();out=snapshot(sources);(out/'audit-baseline.current.json').unlink()
        baseline=baseline_fixture(sources);baseline['analysis']=copy.deepcopy(model)
        baseline['analysis']['computations'][0]['steps'][0]['rhs']=[99]
        record=session.prepare_baseline(out,'failed',sources,wire_provider(return_value=baseline),0)
        self.assertFalse(record['analysis_result']['ok']);self.assertEqual(record['quantitative_status'],'partial')
        self.assertTrue(record['source_execution']['subreceipts']);self.assertFalse(record['source_execution']['coverage_complete'])
        self.assertEqual(record['source_execution']['verified_data']['computations'],{})
        self.assertEqual(record['source_execution']['verified_data']['flows']['out']['totals']['p']['bytes'],80)
        self.assertFalse((out/'audit-baseline.current.json').exists())
        self.assertTrue((out/'failed.baseline-unvalidated.json').exists())
        import test_audit as audit
        context=audit.context_fixture();context['source_analysis']['ok']=False
        report=audit.report_fixture(context)
        self.assertFalse(audit.validator.validate(report,context)['ok'])
        report['verdict']='insufficient_evidence';report['scope']='Original-source structure is valid; quantitative audit witness failed; no approval.'
        self.assertTrue(audit.validator.validate(report,context)['ok'])

    def test_actual_shape_repair_preserves_valid_baseline_and_receipts(self):
        model,sources=witness();out=snapshot(sources);(out/'audit-baseline.current.json').unlink()
        baseline=baseline_fixture(sources);baseline['analysis']=copy.deepcopy(model)
        baseline['analysis']['computations'][0]['steps'][0]['rhs']=[99]
        original=copy.deepcopy(baseline);seen=[]
        repair={'analysis':model,
                'source_revision':{'reason':'contracted operand dimension corrected from originals','references':[{'file':'spec.md','start':1,'end':2}]}}
        def invoke(name,role,prompt,parser):
            seen.append(prompt)
            if name.endswith('baseline-0'):return wire_reply(baseline,parser)
            self.assertIn('CURRENT STEP: return ONLY <<<BASELINE_ANALYSIS_REPAIR>>>',prompt)
            self.assertNotIn('<<<AUDIT_TOOL_REQUEST>>>',prompt)
            return parser('<<<BASELINE_ANALYSIS_REPAIR>>>\n'+json.dumps(repair))
        record=session.prepare_baseline(out,'source',sources,invoke,1)
        self.assertEqual(baseline,original);self.assertEqual(record['baseline']['obligations'],baseline['obligations'])
        for key in ('scope','ambiguities','limits'):self.assertEqual(record['baseline'][key],baseline[key])
        self.assertEqual(record['analysis_result']['computations']['projection']['totals']['p'],60)
        self.assertEqual(json.loads((out/'source.baseline-0.json').read_text()),baseline)
        self.assertTrue((out/'source.baseline-1.analysis-repair.json').exists());self.assertEqual(len(seen),2)

    def test_stale_hash_or_extra_outer_edit_cannot_be_repaired(self):
        model,sources=witness();baseline=baseline_fixture(sources);baseline['analysis']=copy.deepcopy(model)
        baseline['analysis']['computations'][0]['steps'][0]['rhs']=[99]
        for changed in ({'base_sha256':'stale'}, {'obligations':[]}):
            out=snapshot(sources);(out/'audit-baseline.current.json').unlink()
            request={'analysis':model,'source_revision':{'reason':'fix','references':[{'file':'spec.md','start':1,'end':2}]},**changed}
            with self.assertRaises(session.AuditSessionLimit):
                session.prepare_baseline(out,'bad',sources,wire_provider(side_effect=[baseline,{'analysis_repair':request}]),1)
            self.assertFalse((out/'audit-baseline.current.json').exists())

if __name__=='__main__':unittest.main()
