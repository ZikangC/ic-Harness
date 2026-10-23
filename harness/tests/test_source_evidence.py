"""Frozen evidence identities and bounded, model-authored audit repairs."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import test_audit as audit
from harness.runtime import audit_session as session
from harness.runtime import source_evidence as source


def source_case():
    sources={'spec.md':'Header\r\nAll writes precede completion.\r\nAll writes precede completion.\r\n',
             'reference.py':'def operation(value):\n    return value + 1\n',
             'architecture.md':'Unrelated proposal text.'}
    baseline={'scope':'source interpretation only','limits':'no semantic proof','ambiguities':[],
              'obligations':[{'id':'completion','category':'completion','requirement':'accepted writes before completion',
                              'references':[{'file':'spec.md','quote':'All writes precede completion.'}]},
                             {'id':'work','category':'computation','requirement':'declared arithmetic',
                              'references':[{'file':'reference.py','quote':'return value + 1'}]}]}
    # Executed scalar witness keeps citation tests independent of proposal data.
    baseline['analysis']=audit.baseline_fixture({k:v for k,v in sources.items() if k in ('spec.md','reference.py')})['analysis']
    baseline['analysis']['workloads'][0]['quantity']=1
    baseline['analysis']['computations'][0]['steps'][0]['shape']=[1]
    return baseline,sources


class SourceEvidenceTests(unittest.TestCase):
    def test_invalid_span_reports_actual_bounds_without_clamping_reference(self):
        baseline,sources=source_case();ref={'file':'spec.md','start':2,'end':5}
        baseline['obligations'][0]['references'][0]=ref;before=copy.deepcopy(baseline)
        report=audit.validator.validate_baseline(baseline,sources)
        self.assertFalse(report['ok']);self.assertEqual(baseline,before)
        diagnostic=report['citation_diagnostics'][0]
        self.assertEqual(diagnostic['line_count'],3)
        self.assertEqual(diagnostic['requested_bounds'],{'start':2,'end':5})
        self.assertEqual(diagnostic['available_bounds'],{'minimum':1,'maximum':3})
        self.assertTrue(diagnostic['context'])
        with self.assertRaisesRegex(ValueError,r'spec.md.*requested=\(2, 5\).*valid=1\.\.3'):
            source.selected_span(ref,sources,input_only=True)
        self.assertEqual(ref,{'file':'spec.md','start':2,'end':5})

    def test_selected_span_binds_exact_duplicate_location_and_current_source_version(self):
        baseline,sources=source_case();reference={'file':'spec.md','start':3,'end':3}
        before=copy.deepcopy(reference);resolved=audit.validator.resolve_citation(reference,sources,(),input_only=True)
        self.assertEqual(reference,before);self.assertEqual(resolved['quote'],'All writes precede completion.')
        self.assertEqual((resolved['start'],resolved['end']),(3,3))
        self.assertEqual(resolved,source.selected_span(reference,sources,input_only=True))
        fixed=copy.deepcopy(baseline);fixed['obligations'][0]['references'][0]=reference
        baseline_before=copy.deepcopy(fixed);checked=audit.validator.validate_baseline(fixed,sources)
        self.assertTrue(checked['ok'],checked['errors']);self.assertEqual(fixed,baseline_before)
        self.assertEqual(checked['citation_bindings'][0]['reference'],reference)
        self.assertEqual(checked['citation_bindings'][0]['resolved'],resolved)
        changed=dict(sources);changed['spec.md']+='New source content.'
        current=audit.validator.resolve_citation(reference,changed,(),input_only=True)
        self.assertNotEqual(current['evidence_id'],resolved['evidence_id'])
        with self.assertRaises(ValueError):audit.validator.resolve_citation({'evidence_id':resolved['evidence_id']},changed,[resolved],input_only=True)

    def test_span_rejects_invalid_bounds_noninputs_and_location_overrides(self):
        _,sources=source_case()
        cases=[{'file':'spec.md','start':0,'end':1},{'file':'spec.md','start':3,'end':2},
               {'file':'spec.md','start':1,'end':99},{'file':'spec.md','start':1.0,'end':2},
               {'file':'spec.md','start':True,'end':2},{'file':'architecture.md','start':1,'end':1},
               {'file':'/etc/passwd','start':1,'end':1},{'file':'spec.md','start':1,'end':1,'extra':'override'}]
        for reference in cases:
            with self.subTest(reference=reference):
                with self.assertRaises(ValueError):audit.validator.resolve_citation(reference,sources,(),input_only=True)
                with self.assertRaises(ValueError):source.selected_span(reference,sources,input_only=True)
        baseline,_=source_case();baseline['obligations'][0]['references'][0]={'file':'spec.md','start':2,'end':2,'quote':'Misspelled original quote'}
        original=copy.deepcopy(baseline);self.assertFalse(audit.validator.validate_baseline(baseline,sources)['ok'])
        self.assertEqual(baseline,original)

    def test_multiline_span_preserves_crlf_and_raw_reference(self):
        _,sources=source_case();reference={'file':'spec.md','start':2,'end':3}
        resolved=audit.validator.resolve_citation(reference,sources,(),input_only=True)
        self.assertEqual(resolved['quote'],'All writes precede completion.\r\nAll writes precede completion.')
        self.assertIn(resolved['quote'],sources['spec.md'])
        self.assertEqual(audit.validator.resolve_citation({'evidence_id':resolved['evidence_id']},sources,[resolved]),resolved)

    def test_report_span_resolution_preserves_report_and_records_binding(self):
        context=audit.context_fixture();report=audit.report_fixture(context)
        for file,quote in (('spec.md','area 3'),('design.json','"id": "small"')):
            text=context['sources'][file];line=text[:text.index(quote)].count('\n')+1
            span={'file':file,'start':line,'end':line}
            report['checks'][0]['references'][0 if file=='spec.md' else 1]=span
        original=copy.deepcopy(report);checked=audit.validator.validate(report,context)
        self.assertTrue(checked['ok'],checked['errors']);self.assertEqual(report,original)
        located=[row for row in checked['bindings'] if 'resolved' in row]
        self.assertEqual(len(located),2);self.assertIn('evidence_id',located[0]['resolved'])

    def test_fact_spans_execute_literal_and_composite_without_editing_request(self):
        sources={'spec.md':'rows=7\ninner=5\n','reference.py':'return value\n'}
        request={'tool':'recompute','facts':[{'id':'raw_rows','source':{'file':'spec.md','line':1,'number':1},'unit':'items'},
                                            {'id':'raw_inner','source':{'file':'spec.md','line':2,'number':1},'unit':'items'},
                                            {'id':'rows','formula':'raw_rows','unit':'items','citations':[{'file':'spec.md','start':1,'end':1}]},
                                            {'id':'inner','formula':'raw_inner','unit':'items','citations':[{'file':'spec.md','start':2,'end':2}]}],
                 'assumptions':[],'calculations':[{'id':'products','formula':'rows*inner','unit':'operations','basis':'one product per row/inner pair'}]}
        original=copy.deepcopy(request);checked=audit.validator.recompute(request,sources,audit.engine)
        self.assertTrue(checked['ok'],checked['errors']);self.assertEqual(checked['values']['products'],35)
        self.assertEqual(request,original)
        self.assertEqual(len([b for b in checked['citation_bindings'] if 'resolved' in b]),2)
        self.assertEqual(len([b for b in checked['citation_bindings'] if 'selector' in b]),2)
        bad=copy.deepcopy(request);bad['facts'][0]['value']=35
        self.assertFalse(audit.validator.recompute(bad,sources,audit.engine)['ok'])

    def test_reference_repairs_accept_span_or_id_exclusively_and_preserve_status(self):
        baseline,sources=source_case();catalogue=source.catalogue(sources)['citations']
        update={'obligation_id':'completion','reference_index':0,'reference':{'file':'spec.md','start':3,'end':3}}
        request={'reference_updates':[update]}
        repaired=session.repair_baseline(baseline,request,catalogue,sources)
        self.assertEqual(repaired['obligations'][0]['references'][0],update['reference'])
        self.assertTrue(audit.validator.validate_baseline(repaired,sources)['ok'])
        mixed=copy.deepcopy(request);mixed['reference_updates'][0]['evidence_id']=catalogue[0]['evidence_id']
        with self.assertRaises(ValueError):session.repair_baseline(baseline,mixed,catalogue,sources)
        context=audit.context_fixture();report=audit.report_fixture(context);original=copy.deepcopy(report)
        text=context['sources']['spec.md'];line=text[:text.index('area 3')].count('\n')+1
        request={'reference_updates':[{'collection':'checks','row_index':0,'reference_index':0,'reference':{'file':'spec.md','start':line,'end':line}}]}
        repaired=session.repair_report(report,request,context['citations'],context['sources'])
        self.assertEqual(repaired['checks'][0]['status'],report['checks'][0]['status'])
        self.assertEqual(report,original);self.assertTrue(audit.validator.validate(repaired,context)['ok'])

    def test_returned_multiline_span_id_can_be_reused_in_runtime_report(self):
        document,sources=audit.fixture();sources=dict(sources);sources['spec.md']+='\nAdditional independent synthetic clause.\n'
        out=audit.snapshot(sources);budget=audit.engine.evaluate(document,sources,True)
        request=audit.arithmetic_request();text=sources['spec.md'];span={'file':'spec.md','start':1,'end':len(text.splitlines())}
        self.assertGreater(span['end'],1)
        request['facts']=[{'id':'raw_area','source':{'file':'spec.md','line':1,'number':1},'unit':'mm2'},
                          {'id':'source_area','formula':'raw_area','unit':'mm2','citations':[span]}]
        seen=[]
        def invoke(name,role,prompt,parser):
            seen.append(name)
            if name.endswith('step-0'):return {'tool_request':request}
            context=audit.packet(sources,document,budget)
            receipt=json.loads((out/'returned.step-0.tool.result.json').read_text());receipt['request_id']='returned.step-0';context['calculations']=[receipt]
            self.assertTrue(receipt['ok'],receipt['errors'])
            bound=receipt['citation_bindings'][0]['resolved']
            report=audit.report_content_fixture(context);report['verdict']='insufficient_evidence';report['checks'][0]['references'][0]={'evidence_id':bound['evidence_id']}
            return {'final':report}
        review,checked=session.run(out,'returned',sources,document,budget,audit.wire_provider(callback=invoke),1)
        self.assertTrue(checked['ok'],checked['errors']);self.assertEqual(len(seen),2)
        self.assertIn('evidence_id',review['checks'][0]['references'][0])

    def test_stable_catalogue_changes_when_source_version_changes_elsewhere(self):
        _,sources=source_case();before=copy.deepcopy(sources)
        first=source.catalogue(sources,input_only=True)
        self.assertEqual(first,source.catalogue(dict(reversed(list(sources.items()))),input_only=True))
        self.assertEqual({r['file'] for r in first['citations']},{'spec.md','reference.py'})
        for row in first['citations']:
            self.assertIn(row['quote'],sources[row['file']]);self.assertEqual(row['start'],row['end'])
        changed=dict(sources);changed['spec.md']+='New unrelated condition.\r\n'
        second=source.catalogue(changed,input_only=True)
        old=next(r for r in first['citations'] if r['file']=='spec.md' and r['start']==2)
        new=next(r for r in second['citations'] if r['file']=='spec.md' and r['start']==2)
        self.assertEqual(old['quote'],new['quote']);self.assertNotEqual(old['evidence_id'],new['evidence_id'])
        self.assertEqual(sources,before)

    def test_duplicate_excerpt_id_has_exact_bounds_without_rewriting_baseline(self):
        baseline,sources=source_case();citations=source.catalogue(sources,input_only=True)['citations']
        row=next(r for r in citations if r['file']=='spec.md' and r['start']==3)
        fixed=copy.deepcopy(baseline);fixed['obligations'][0]['references'][0]={'evidence_id':row['evidence_id']}
        before=copy.deepcopy(fixed)
        checked=audit.validator.validate_baseline(fixed,sources,citations)
        self.assertTrue(checked['ok'],checked['errors']);self.assertEqual(fixed,before)
        self.assertFalse(audit.validator.validate_baseline(baseline,sources,citations)['ok'])

    def test_forged_stale_mixed_and_noninput_ids_are_rejected(self):
        baseline,sources=source_case();citations=source.catalogue(sources)['citations']
        original=next(r for r in citations if r['file']=='spec.md' and r['start']==2)
        proposal=next(r for r in citations if r['file']=='architecture.md')
        for reference in ({'evidence_id':'invented'},{'evidence_id':proposal['evidence_id']},
                          {'evidence_id':original['evidence_id'],'quote':'overridden'}):
            with self.subTest(reference=reference):
                changed=copy.deepcopy(baseline);changed['obligations'][0]['references'][0]=reference
                self.assertFalse(audit.validator.validate_baseline(changed,sources,citations)['ok'])
        changed=copy.deepcopy(baseline);changed['obligations'][0]['references'][0]={'evidence_id':original['evidence_id']}
        stale=dict(sources);stale['spec.md']+='new clause'
        self.assertFalse(audit.validator.validate_baseline(changed,stale,citations)['ok'])

    def test_checkpoint_and_prompt_never_add_proposal_to_input_catalogue(self):
        _,sources=source_case();out=Path(tempfile.mkdtemp(prefix='source-evidence-'))
        first=source.checkpoint(out,'input',sources,input_only=True)
        self.assertEqual(first,source.checkpoint(out,'input',sources,input_only=True))
        prompt=source.prompt_catalogue(first)
        self.assertNotIn('architecture.md',prompt);self.assertNotIn('Unrelated proposal',prompt)
        self.assertNotIn(first['citations'][0]['evidence_id'],prompt)
        self.assertIn('"start":1,"end":2',prompt)
        self.assertIn('lines=3',prompt)
        path=next(out.glob('input.*.json'))
        damaged=json.loads(path.read_text());damaged['citations'][0]['quote']='wrong'
        path.write_text(json.dumps(damaged))
        with self.assertRaises(ValueError):source.checkpoint(out,'input',sources,input_only=True)

    def test_input_versions_keep_separate_catalogues_and_compact_prompt(self):
        _,sources=source_case();sources['design.json']='\n'.join('UNREPEATED_PAYLOAD '+str(i) for i in range(1500))
        out=Path(tempfile.mkdtemp(prefix='source-evidence-versions-'))
        first=source.checkpoint(out,'input',sources,input_only=True)
        changed=dict(sources);changed['spec.md']+='New source version.'
        second=source.checkpoint(out,'input',changed,input_only=True)
        self.assertNotEqual(first,second);self.assertEqual(len(list(out.glob('input.*.json'))),2)
        full=source.catalogue(sources);prompt=source.prompt_catalogue(full)
        self.assertNotIn('UNREPEATED_PAYLOAD',prompt)
        self.assertNotIn('All writes precede completion.',prompt)
        self.assertLess(len(prompt),2500)
        self.assertIn('FILE design.json lines=1500 source_sha256=',prompt)
        self.assertTrue(any(row['file']=='design.json' for row in full['citations']))

    def test_baseline_repair_freezes_semantics_requires_actual_id_and_base(self):
        baseline,sources=source_case();citations=source.catalogue(sources)['citations']
        chosen=next(r for r in citations if r['file']=='spec.md' and r['start']==2)
        request={'reference_updates':[{'obligation_id':'completion','reference_index':0,'evidence_id':chosen['evidence_id']}]}
        before=copy.deepcopy(baseline);changed=session.repair_baseline(baseline,request,citations)
        self.assertEqual(baseline,before)
        self.assertEqual(changed['obligations'][0]['requirement'],before['obligations'][0]['requirement'])
        self.assertTrue(audit.validator.validate_baseline(changed,sources,citations)['ok'])
        for mutate in (lambda r:r.update(base_sha256='stale'),lambda r:r.update(obligations=[]),
                       lambda r:r['reference_updates'][0].update(reference_index=5),
                       lambda r:r['reference_updates'][0].update(evidence_id='unknown'),
                       lambda r:r['reference_updates'][0].update(evidence_id=next(x['evidence_id'] for x in citations if x['file']=='architecture.md'))):
            bad=copy.deepcopy(request);mutate(bad)
            with self.assertRaises(ValueError):session.repair_baseline(baseline,bad,citations)

    def test_baseline_runtime_supplies_ids_repairs_and_resumes_exact_versions(self):
        baseline,sources=source_case();out=audit.snapshot(sources);(out/'audit-baseline.current.json').unlink()
        chosen=next(r for r in source.catalogue(sources,input_only=True)['citations'] if r['file']=='spec.md' and r['start']==3)
        def invoke(name,role,prompt,parser):
            self.assertNotIn(chosen['evidence_id'],prompt);self.assertNotIn('FILE architecture.md',prompt)
            if name.endswith('baseline-0'):return audit.wire_reply(baseline,parser)
            request={'reference_updates':[{'obligation_id':'completion','reference_index':0,'reference':{'file':'spec.md','start':3,'end':3}}]}
            return parser('<<<BASELINE_REPAIR>>>\n'+json.dumps(request))
        model=Mock(side_effect=invoke);record=session.prepare_baseline(out,'blind',sources,model,1)
        self.assertEqual(model.call_count,2);self.assertEqual(json.loads((out/'blind.baseline-0.json').read_text()),baseline)
        self.assertEqual(json.loads((out/'blind.baseline-1.json').read_text()),record['baseline'])
        self.assertTrue((out/'blind.baseline-1.repair.json').exists())
        unused=Mock(side_effect=AssertionError('cached baseline should not call model'))
        self.assertEqual(record,session.prepare_baseline(out,'again',sources,unused,1))

    def test_report_repairs_replace_existing_rows_and_references_only(self):
        context=audit.context_fixture();report=audit.report_fixture(context);before=copy.deepcopy(report)
        row=copy.deepcopy(report['checks'][0]);row['evidence']='explicit model-authored source/candidate reconstruction'
        cid=next(r['evidence_id'] for r in context['citations'] if r['file']=='spec.md')
        row['references'][0]={'evidence_id':cid}
        request={'checks':[row]}
        repaired=session.repair_report(report,request,context['citations'])
        self.assertEqual(report,before);self.assertEqual(repaired['checks'][0]['evidence'],row['evidence'])
        for key in ('verdict','findings','observations','revision_checks','packet_sha256'):self.assertEqual(repaired[key],before[key])
        for extra in ('verdict','findings','observations','revision_checks','packet_sha256','calculations'):
            with self.assertRaises(ValueError):session.repair_report(report,dict(request,**{extra:[]}),context['citations'])
        bad=copy.deepcopy(request);bad['checks'][0]['topic']='invented'
        with self.assertRaises(ValueError):session.repair_report(report,bad,context['citations'])

    def test_report_repair_cannot_change_or_invent_engineering_status(self):
        context=audit.context_fixture();report=audit.report_fixture(context)
        for collection,old,new in (('checks','fail','pass'),('checks','conditional','pass'),
                                   ('requirements','missing','covered'),('requirements','contradicted','covered'),
                                   ('requirements','omitted','covered')):
            base=copy.deepcopy(report);base[collection][0]['status']=old
            row=copy.deepcopy(base[collection][0]);row['status']=new
            request={collection:[row]}
            with self.subTest(collection=collection,old=old,new=new):
                with self.assertRaisesRegex(ValueError,'engineering status'):session.repair_report(base,request,context['citations'])
        base=copy.deepcopy(report);base['checks'][0].pop('status')
        row=copy.deepcopy(report['checks'][0]);row['status']='pass'
        with self.assertRaisesRegex(ValueError,'engineering status'):
            session.repair_report(base,{'checks':[row]},context['citations'])

    def test_full_baseline_citation_repair_cannot_silently_change_semantics(self):
        baseline,sources=source_case();out=audit.snapshot(sources);(out/'audit-baseline.current.json').unlink()
        chosen=next(r for r in source.catalogue(sources,input_only=True)['citations'] if r['file']=='spec.md' and r['start']==2)
        altered=copy.deepcopy(baseline);altered['obligations'][0]['references'][0]={'evidence_id':chosen['evidence_id']}
        altered['obligations'][0]['requirement']='Different source interpretation'
        invoke=audit.wire_provider(side_effect=[baseline,altered])
        with self.assertRaises(session.AuditSessionLimit):session.prepare_baseline(out,'silent',sources,invoke,1)
        self.assertFalse((out/'audit-baseline.current.json').exists())
        self.assertIn('citation repair cannot rewrite semantics',json.loads((out/'silent.baseline-1.feedback.json').read_text())['diagnostics'][0])

    def test_explicit_baseline_source_revision_is_recorded_with_original_evidence(self):
        baseline,sources=source_case();out=audit.snapshot(sources);(out/'audit-baseline.current.json').unlink()
        chosen=next(r for r in source.catalogue(sources,input_only=True)['citations'] if r['file']=='spec.md' and r['start']==2)
        altered=copy.deepcopy(baseline);altered['obligations'][0]['references'][0]={'evidence_id':chosen['evidence_id']}
        altered['obligations'][0]['requirement']='Explicit model-authored corrected interpretation'
        altered['source_revision']={'reason':'re-read original completion clause','references':[{'evidence_id':chosen['evidence_id']}]}
        invoke=audit.wire_provider(side_effect=[baseline,altered]);record=session.prepare_baseline(out,'revision',sources,invoke,1)
        self.assertEqual(record['baseline'],altered)
        self.assertEqual(json.loads((out/'revision.baseline-0.json').read_text()),baseline)
        bad=copy.deepcopy(altered);bad['source_revision']['references']=[{'file':'architecture.md','quote':sources['architecture.md']}]
        self.assertFalse(audit.validator.validate_baseline(bad,sources,source.catalogue(sources)['citations'])['ok'])

    def test_report_runtime_repairs_format_without_new_arithmetic_or_judgment(self):
        document,sources=audit.fixture();out=audit.snapshot(sources);budget=audit.engine.evaluate(document,sources,True)
        context=audit.packet(sources,document,budget)
        receipt=audit.validator.recompute(audit.arithmetic_request(),context['sources'],audit.engine);receipt['request_id']='restricted.step-0';context['calculations']=[receipt]
        good=audit.report_content_fixture(context);good['verdict']='insufficient_evidence';bad=copy.deepcopy(good);bad['checks'][0]['evidence']=['wrong type']
        repair={'checks':[good['checks'][0]]}
        invoke=audit.wire_provider(side_effect=[{'tool_request':audit.arithmetic_request()},{'final':bad},{'report_repair':repair}])
        reviewed,checked=session.run(out,'restricted',sources,document,budget,invoke,1,max_report_repairs=1)
        self.assertTrue(checked['ok'],checked['errors']);self.assertEqual(reviewed,dict(good,packet_sha256=context['packet_sha256'],baseline_sha256=context['baseline']['baseline_sha256']))
        self.assertEqual(invoke.call_count,3);self.assertTrue((out/'restricted.step-2.repair.json').exists())
        self.assertFalse((out/'restricted.step-2.tool.execution.json').exists())
        resumed=Mock(side_effect=AssertionError('completed report checkpoints should replay'))
        self.assertEqual(session.run(out,'restricted',sources,document,budget,resumed,1,max_report_repairs=1)[0],reviewed)

    def test_local_report_limit_stops_and_cannot_recompute_after_rejection(self):
        document,sources=audit.fixture();context=audit.context_fixture();budget=audit.engine.evaluate(document,sources,True)
        bad=audit.report_content_fixture(context);bad['checks'][0]['evidence']=[]
        for allowance in (0,1):
            out=audit.snapshot();invoke=audit.wire_provider(side_effect=[{'final':bad},{'tool_request':audit.arithmetic_request()}])
            with self.assertRaisesRegex(session.AuditSessionLimit,'report repair limit'):
                session.run(out,'bounded',sources,document,budget,invoke,1,max_report_repairs=allowance)
            self.assertEqual(invoke.call_count,1+allowance)
            self.assertFalse((out/'bounded.step-1.tool.execution.json').exists())

    def test_invalid_status_does_not_hide_unexecuted_observation(self):
        document,sources=audit.fixture();context=audit.context_fixture();budget=audit.engine.evaluate(document,sources,True)
        bad=audit.report_content_fixture(context);bad['requirements'][0]['status']='omitted'
        out=audit.snapshot();invoke=audit.wire_provider(return_value={'final':bad})
        # This report cites a context-fixture calculation that was never run in
        # this session. Illegal status cannot hide that unsafe observation.
        with self.assertRaisesRegex(session.AuditSessionLimit,'unsafe evidence'):
            session.run(out,'status',sources,document,budget,invoke,1,max_report_repairs=3)
        self.assertEqual(invoke.call_count,1)
        self.assertTrue((out/'status.step-0.report-check.result.json').exists())

    def test_failed_recompute_can_be_retried_before_first_report(self):
        document,sources=audit.fixture();out=audit.snapshot(sources);budget=audit.engine.evaluate(document,sources,True)
        bad=audit.arithmetic_request();bad['calculations'][0]['formula']='undefined_input'
        context=audit.packet(sources,document,budget)
        receipt=audit.validator.recompute(audit.arithmetic_request(),context['sources'],audit.engine);receipt['request_id']='retry.step-1';context['calculations']=[receipt]
        good=audit.report_content_fixture(context);good['verdict']='insufficient_evidence'
        invoke=audit.wire_provider(side_effect=[{'tool_request':bad},{'tool_request':audit.arithmetic_request()},{'final':good}])
        review,checked=session.run(out,'retry',sources,document,budget,invoke,2)
        self.assertTrue(checked['ok']);self.assertEqual(review,dict(good,packet_sha256=context['packet_sha256'],baseline_sha256=context['baseline']['baseline_sha256']))
        self.assertFalse(json.loads((out/'retry.step-0.tool.result.json').read_text())['ok'])
        self.assertTrue(json.loads((out/'retry.step-1.tool.result.json').read_text())['ok'])
