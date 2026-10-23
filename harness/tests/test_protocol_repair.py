"""Adversarial transport tests: syntax repair must never rewrite a design claim."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from harness.adapters import omp
from harness.prompts.architecture import parse_payload
from harness.runtime import audit_session, design_session
from harness.runtime.call_budget import CallBudget


class ProtocolRepairTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.out = Path(self.temporary.name)
        guard = self.out / 'snapshot/harness/adapters/omp_isolation.js'
        guard.parent.mkdir(parents=True)
        guard.write_text('// Mock transport never executes this extension.\n')
        self.config = {
            'roles': {'architecture': 'design-model', 'review': 'audit-model', 'evidence': 'format-model'},
            'runtime': {'command_template': ['mock-omp', '-p', '--cwd', '{project_root}', '--model', '{model}', '{prompt}']},
        }
        self.charges = Mock()

    def invoke(self, raw, repaired=None, parser=None, role='architecture', extra_turn=False, prompt='Frozen inputs and method.'):
        responses = iter([raw] + ([] if repaired is None else [repaired]))
        dispatches = []
        self.dispatches = dispatches
        self.deliveries=[]

        def mock_run(argv, **kwargs):
            answer = next(responses)
            dispatches.append(argv)
            self.deliveries.append(kwargs.get('input'))
            receipt = {'checks': [{'active_tool_names': []}], 'blocked': []}
            events = []
            if extra_turn and len(dispatches) == 1:
                receipt['blocked'] = [{'id': 'blocked-call'}]
                events.extend([
                    {'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': 'toolUse',
                        'content': [{'type': 'toolCall', 'id': 'blocked-call', 'name': 'unavailable_tool'}]}},
                    {'type': 'message_end', 'message': {'role': 'toolResult', 'toolCallId': 'blocked-call', 'isError': True}},
                ])
            events.extend([
                {'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': 'stop',
                    'content': [{'type': 'thinking', 'thinking': 'PRIVATE_THINKING_SENTINEL'}, {'type': 'text', 'text': answer}]}},
                {'type': 'agent_end'},
            ])
            Path(kwargs['env']['HARNESS_TOOL_ISOLATION_RECEIPT']).write_text(json.dumps(receipt))
            return SimpleNamespace(returncode=0, stdout='\n'.join(map(json.dumps, events)), stderr='PRIVATE_STDERR_SENTINEL')

        with patch.object(omp.subprocess, 'run', side_effect=mock_run):
            value = omp.call_model(self.out, 'response', role, prompt, self.config,
                                   self.out, 30, parser=parser, before_call=self.charges)
        return value, dispatches

    def test_large_multibyte_prompt_uses_lossless_native_stdin_with_bounded_argv(self):
        prompt='完整冻结输入与审计规则 €🙂\n' * 70000
        result,dispatches=self.invoke('{"ok":true}',role='review',prompt=prompt)
        self.assertEqual(result,{'ok':True});self.assertEqual(len(dispatches),1)
        argv=dispatches[0]
        self.assertLess(sum(len(value.encode('utf-8'))+1 for value in argv),4096)
        self.assertNotIn(prompt,argv);self.assertFalse(any(value.startswith('@') for value in argv))
        self.assertEqual(self.deliveries,['Task instructions:\n'+prompt])
        self.assertEqual((self.out/'response.call-1.prompt.txt').read_bytes(),prompt.encode('utf-8'))
        metadata=json.loads((self.out/'response.call-1.call.json').read_text())
        message=self.deliveries[0].encode('utf-8')
        self.assertEqual(metadata['prompt_sha256'],hashlib.sha256(prompt.encode('utf-8')).hexdigest())
        self.assertEqual(metadata['delivery']['kind'],'native_stdin')
        self.assertEqual(metadata['delivery']['message_sha256'],hashlib.sha256(message).hexdigest())
        self.assertEqual(metadata['delivery']['message_bytes'],len(message));self.assertGreater(len(message),1000000)
        self.assertEqual(metadata['role'],'review');self.assertEqual(metadata['model'],'audit-model')
        self.assertEqual(argv[argv.index('--model')+1],'audit-model');self.assertEqual(argv[argv.index('--cwd')+1],str(self.out))
        for flag in ('-p','--no-tools','--no-extensions','--no-skills','--no-rules','--no-session','--no-title','--no-lsp'):
            self.assertIn(flag,argv)
        self.assertEqual(argv[argv.index('--extension')+1],str((self.out/'snapshot/harness/adapters/omp_isolation.js').resolve()))
        self.assertEqual(argv[argv.index('--thinking')+1],'off');self.assertEqual(argv[argv.index('--mode')+1],'json')
        self.assertEqual(self.charges.call_count,1)

    def test_embedded_or_multiple_prompt_placeholders_fail_without_dispatch(self):
        templates=[['mock-omp','--model','{model}','prefix {prompt}'],
                   ['mock-omp','{prompt}','{prompt}'],['mock-omp','--model','{model}']]
        for template in templates:
            with self.subTest(template=template):
                self.config['runtime']['command_template']=template
                with patch.object(omp.subprocess,'run') as dispatch,self.assertRaisesRegex(ValueError,'standalone'):
                    omp.call_model(self.out,'invalid','review','complete prompt',self.config,self.out,30)
                dispatch.assert_not_called()

    def test_compact_design_json_and_readable_markdown_remain_lossless_through_transport(self):
        data={'evaluated_design_sha256':'a'*64}
        markdown='# Selected variant β\n\nThe contract keeps its meaning and verification obligations.\n\n- Preserve the first dependency.\n- Check the second boundary.\n\n```text\nphase A\n  dependency -> phase B\n```\n\nConditions remain explicit.'
        compact=json.dumps(data,separators=(',',':'))
        raw='<<<ARCHITECTURE_DATA>>>\n'+compact+'\n<<<ARCHITECTURE_MD>>>\n'+markdown
        value,dispatches=self.invoke(raw,parser=design_session.parse_turn)
        self.assertEqual(value,{'final':dict(data,architecture_md=markdown)})
        self.assertEqual(len(dispatches),1);self.assertEqual(self.charges.call_count,1)
        self.assertEqual((self.out/'response.call-1.response.txt').read_text(),raw)
        self.assertFalse(list(self.out.glob('*.normalization.json')))
        system=dispatches[0][dispatches[0].index('--system-prompt')+1]
        self.assertIn('Serialize JSON compactly',system);self.assertIn('preserving all required fields, values and meaning',system)
        self.assertIn('Keep Markdown readable',system)
        saved='\n'.join(path.read_text() for path in self.out.glob('*') if path.is_file())
        self.assertNotIn('PRIVATE_THINKING_SENTINEL',saved);self.assertNotIn('PRIVATE_STDERR_SENTINEL',saved)

    def invoke_assistant_events(self,messages,parser,budget):
        def mock_run(argv,**kwargs):
            Path(kwargs['env']['HARNESS_TOOL_ISOLATION_RECEIPT']).write_text(json.dumps({'checks':[{'active_tool_names':[]}],'blocked':[]}))
            events=[{'type':'message_end','message':message} for message in messages]+[{'type':'agent_end'}]
            return SimpleNamespace(returncode=0,stdout='\n'.join(map(json.dumps,events)),stderr='PRIVATE_STDERR_SENTINEL')
        with patch.object(omp.subprocess,'run',side_effect=mock_run) as dispatch:
            try:
                return omp.call_model(self.out,'terminal','review','Frozen input.',self.config,self.out,30,parser=parser,
                    before_call=lambda:budget.reserve('review'),record_observed=lambda count:budget.record_observed(count,'review'))
            finally:self.assertEqual(dispatch.call_count,1)

    def test_zero_exit_terminal_error_never_parses_partial_or_previous_answer(self):
        budget=CallBudget(self.out,{'max_model_calls':10,'total_active_seconds':60});parser=Mock(return_value={'accepted':True})
        for index,partial in enumerate(('', '{"accepted":true}'),1):
            with self.subTest(partial=bool(partial)):
                messages=[{'role':'assistant','stopReason':'stop','content':[{'type':'text','text':'{"earlier":true}'}]},
                    {'role':'assistant','stopReason':'error','errorMessage':'context window exceeded PRIVATE_PROVIDER_ERROR_SENTINEL credential=PRIVATE_CREDENTIAL_SENTINEL',
                     'content':[{'type':'thinking','thinking':'PRIVATE_THINKING_SENTINEL'},{'type':'text','text':partial}]}]
                with self.assertRaisesRegex(omp.OMPTransportError,'class=context') as failure:
                    self.invoke_assistant_events(messages,parser,budget)
                self.assertIsInstance(failure.exception,RuntimeError);self.assertNotIsInstance(failure.exception,ValueError)
                parser.assert_not_called();self.assertEqual(budget.calls,2*index)
                receipt=json.loads((self.out/f'terminal.call-{index}.call.json').read_text())
                self.assertEqual(receipt['exit_code'],0);self.assertEqual(receipt['error_class'],'context')
                self.assertEqual(receipt['observed_assistant_turns'],2)
                self.assertEqual(json.loads((self.out/'limits.json').read_text())['by_purpose']['review'],2*index)
                self.assertFalse(list(self.out.glob('*.normalization.json')))
                safe=str(failure.exception)+'\n'+'\n'.join(path.read_text() for path in self.out.glob('*') if path.is_file())
                for secret in ('PRIVATE_PROVIDER_ERROR_SENTINEL','PRIVATE_CREDENTIAL_SENTINEL','PRIVATE_THINKING_SENTINEL','PRIVATE_STDERR_SENTINEL'):
                    self.assertNotIn(secret,safe)
                with self.assertRaises(omp.OMPTransportError):omp.final_text('\n'.join(json.dumps({'type':'message_end','message':message}) for message in messages))

    def test_prior_provider_error_can_recover_to_one_complete_answer_with_all_turns_charged(self):
        budget=CallBudget(self.out,{'max_model_calls':10,'total_active_seconds':60})
        messages=[{'role':'assistant','stopReason':'error','errorMessage':'context PRIVATE_PROVIDER_ERROR_SENTINEL',
                   'content':[{'type':'thinking','thinking':'PRIVATE_THINKING_SENTINEL'},{'type':'text','text':'unaccepted partial'}]},
                  {'role':'assistant','stopReason':'stop','content':[{'type':'text','text':'{"recovered":true}'}]}]
        parser=Mock(side_effect=json.loads);result=self.invoke_assistant_events(messages,parser,budget)
        self.assertEqual(result,{'recovered':True});parser.assert_called_once_with('{"recovered":true}')
        self.assertEqual(budget.calls,2);self.assertEqual(json.loads((self.out/'limits.json').read_text())['by_purpose']['review'],2)
        summary=json.loads((self.out/'terminal.call-1.transport.json').read_text())['assistant_messages']
        self.assertEqual([row['stop_reason'] for row in summary],['error','stop']);self.assertEqual(summary[0]['error_class'],'context')
        self.assertEqual((self.out/'terminal.call-1.response.txt').read_text(),'{"recovered":true}')
        self.assertFalse(list(self.out.glob('*.normalization.json')))
        safe='\n'.join(path.read_text() for path in self.out.glob('*') if path.is_file())
        for secret in ('PRIVATE_PROVIDER_ERROR_SENTINEL','PRIVATE_THINKING_SENTINEL','PRIVATE_STDERR_SENTINEL'):
            self.assertNotIn(secret,safe)

    def test_terminal_abort_and_length_cannot_parse_thinking_partial_or_old_answer(self):
        budget=CallBudget(self.out,{'max_model_calls':10,'total_active_seconds':60});parser=Mock(return_value={'accepted':True})
        cases=[('aborted','error',''),('aborted','stop','{"accepted":true}'),
               ('length','error','{"accepted":true}'),('length','stop','{"accepted":true}')]
        for index,(reason,prior,partial) in enumerate(cases,1):
            with self.subTest(reason=reason,prior=prior,partial=bool(partial)):
                earlier={'role':'assistant','stopReason':prior,'content':[{'type':'text','text':'{"earlier":true}'}] if prior=='stop' else [],
                         'errorMessage':'PRIVATE_PROVIDER_ERROR_SENTINEL credential=PRIVATE_CREDENTIAL_SENTINEL'}
                content=[{'type':'thinking','thinking':'PRIVATE_THINKING_SENTINEL'}]
                if partial:content.append({'type':'text','text':partial})
                messages=[earlier,{'role':'assistant','stopReason':reason,'content':content}]
                with self.assertRaisesRegex(omp.OMPTransportError,'class='+reason):
                    self.invoke_assistant_events(messages,parser,budget)
                parser.assert_not_called();self.assertEqual(budget.calls,2*index)
                receipt=json.loads((self.out/f'terminal.call-{index}.call.json').read_text())
                self.assertEqual(receipt['exit_code'],0);self.assertEqual(receipt['error_class'],reason)
                self.assertEqual(receipt['observed_assistant_turns'],2)
                summary=json.loads((self.out/f'terminal.call-{index}.transport.json').read_text())['assistant_messages']
                self.assertEqual(summary[-1]['error_class'],reason)
                if prior=='error':self.assertEqual(summary[0]['error_class'],'unspecified')
                if not partial:self.assertEqual(summary[-1]['content_types'],['thinking'])
                self.assertFalse(list(self.out.glob('*.normalization.json')))
                stream='\n'.join(json.dumps(event) for event in [{'type':'message_end','message':message} for message in messages]+[{'type':'agent_end'}])
                with self.assertRaises(omp.OMPTransportError):omp.final_text(stream)
                safe='\n'.join(path.read_text() for path in self.out.glob('*') if path.is_file())
                for secret in ('PRIVATE_PROVIDER_ERROR_SENTINEL','PRIVATE_CREDENTIAL_SENTINEL','PRIVATE_THINKING_SENTINEL','PRIVATE_STDERR_SENTINEL'):
                    self.assertNotIn(secret,safe)

    def assert_saved_original_and_normalization(self, raw):
        original = self.out / 'response.call-1.response.txt'
        self.assertEqual(original.read_text(), raw)
        records = list(self.out.glob('*.normalization.json'))
        self.assertEqual(len(records), 1)
        record = json.loads(records[0].read_text())
        self.assertEqual(record['source_sha256'], hashlib.sha256(raw.encode()).hexdigest())
        self.assertTrue(record['values_order_nesting_preserved'])
        normalized_files = list(self.out.glob('*.normalized.response.txt'))
        self.assertEqual(len(normalized_files), 1)
        normalized = normalized_files[0].read_text()
        self.assertEqual(record['normalized_sha256'], hashlib.sha256(normalized.encode()).hexdigest())
        all_text = '\n'.join(path.read_text() for path in self.out.glob('*') if path.is_file())
        self.assertNotIn('PRIVATE_STDERR_SENTINEL', all_text)
        self.assertNotIn('PRIVATE_THINKING_SENTINEL', all_text)

    def test_custom_budget_parser_repairs_once_and_charges_transport(self):
        raw = '<<<BUDGET_REQUEST>>>\n{"tool":"evaluate_design" "document":{"selected":"a","candidates":[]}}'
        fixed = '{"tool":"evaluate_design","document":{"selected":"a","candidates":[]}}'
        value, dispatches = self.invoke(raw, fixed, design_session.parse_turn)
        self.assertEqual(value, {'tool_request': json.loads(fixed)})
        self.assertEqual(len(dispatches), 2)
        self.assertEqual(self.charges.call_count, 2)
        metadata = [json.loads(path.read_text()) for path in sorted(self.out.glob('*.call.json'))]
        self.assertEqual(sorted(m['role'] for m in metadata), ['architecture', 'evidence'])
        self.assert_saved_original_and_normalization(raw)

    def test_repair_uses_remaining_original_deadline(self):
        raw='<<<WORK_MODEL>>>\n{"items":[] "workloads":[]}'
        with patch.object(omp.time,'monotonic',side_effect=[100,110,110]):
            value,dispatches=self.invoke(raw,'{"items":[],"workloads":[]}',lambda text:parse_payload(text.split('<<<WORK_MODEL>>>',1)[-1]))
        repairs=[json.loads(p.read_text()) for p in self.out.glob('*-serialization*.call.json')]
        self.assertEqual(repairs[0]['timeout_seconds'],20);self.assertEqual(self.charges.call_count,2)

    def test_work_model_source_entry_uses_its_original_parser(self):
        raw = '<<<WORK_MODEL>>>\n{"workloads":[] "items":[]}'
        value, dispatches = self.invoke(raw, '{"workloads":[],"items":[]}',
            lambda text: parse_payload(text.split('<<<WORK_MODEL>>>', 1)[-1]))
        self.assertEqual(value, {'workloads': [], 'items': []})
        self.assertEqual(len(dispatches), 2)
        self.assert_saved_original_and_normalization(raw)

    def test_audit_baseline_source_entry_repairs_with_review_role(self):
        raw = '<<<AUDIT_BASELINE>>>\n{"scope":"source-only" "obligations":[]}'
        value, dispatches = self.invoke(raw, '{"scope":"source-only","obligations":[]}',
            lambda text: parse_payload(text.split('<<<AUDIT_BASELINE>>>', 1)[-1]), role='review')
        self.assertEqual(value, {'scope': 'source-only', 'obligations': []})
        self.assertEqual(len(dispatches), 2)
        self.assert_saved_original_and_normalization(raw)

    def test_audit_tool_request_repairs_and_preserves_classification(self):
        raw = '<<<AUDIT_TOOL_REQUEST>>>\n{"tool":"lookup_source" "queries":[]}'
        value, dispatches = self.invoke(raw, '{"tool":"lookup_source","queries":[]}', audit_session.parse_turn, role='review')
        self.assertEqual(value, {'tool_request': {'tool': 'lookup_source', 'queries': []}})
        self.assertEqual(len(dispatches), 2)

    def test_audit_report_repairs_and_preserves_classification(self):
        raw = '<<<AUDIT_REPORT>>>\n{"verdict":"revise" "findings":[]}'
        value, dispatches = self.invoke(raw, '{"verdict":"revise","findings":[]}', audit_session.parse_turn, role='review')
        self.assertEqual(value, {'final': {'verdict': 'revise', 'findings': []}})
        self.assertEqual(len(dispatches), 2)
        self.assert_saved_original_and_normalization(raw)

    def test_audit_reference_array_repair_preserves_selectors_and_protocol(self):
        raw='<<<AUDIT_REPAIR>>>\n{"checks":[] "reference_replacements":[{"collection":"checks","topic":"abi","references":[{"file":"spec.md","start":2,"end":3},{"file":"architecture.md","start":7,"end":7}]}]}'
        fixed='{"checks":[],"reference_replacements":[{"collection":"checks","topic":"abi","references":[{"file":"spec.md","start":2,"end":3},{"file":"architecture.md","start":7,"end":7}]}]}'
        value,dispatches=self.invoke(raw,fixed,audit_session.parse_turn,role='review')
        self.assertEqual(value,{'report_repair':json.loads(fixed)})
        self.assertEqual(len(dispatches),2);self.assertEqual(self.charges.call_count,2)
        self.assert_saved_original_and_normalization(raw)

    def test_plain_json_repair_keeps_exact_hex_value(self):
        raw = '{"address":0x123456789abcdef "ratio":1e-8}'
        fixed = '{"address":81985529216486895,"ratio":1e-8}'
        value, dispatches = self.invoke(raw, fixed)
        self.assertEqual(value['address'], int('123456789abcdef', 16))
        self.assertEqual(len(dispatches), 2)

    def test_string_control_character_can_be_escaped_without_changing_value(self):
        raw = '<<<WORK_MODEL>>>\n{"semantics":"first\nsecond" "items":[]}'
        fixed = '{"semantics":"first\\nsecond","items":[]}'
        value, dispatches = self.invoke(raw, fixed, lambda text: parse_payload(text.split('<<<WORK_MODEL>>>', 1)[-1]))
        self.assertEqual(value['semantics'], 'first\nsecond')
        self.assertEqual(len(dispatches), 2)

    def test_extra_native_turn_and_format_repair_are_both_charged(self):
        raw = '<<<AUDIT_REPORT>>>\n{"verdict":"revise" "findings":[]}'
        value, dispatches = self.invoke(raw, '{"verdict":"revise","findings":[]}', audit_session.parse_turn,
                                        role='review', extra_turn=True)
        self.assertEqual(value['final']['verdict'], 'revise')
        self.assertEqual(len(dispatches), 2)
        self.assertEqual(self.charges.call_count, 3)

    def test_valid_payload_schema_failure_does_not_trigger_syntax_repair(self):
        raw = '<<<BUDGET_REQUEST>>>\n{"tool":"not_a_supported_tool","document":{}}'
        with self.assertRaises(ValueError):
            self.invoke(raw, parser=design_session.parse_turn)
        self.assertEqual(self.charges.call_count, 1)
        self.assertFalse(list(self.out.glob('*.normalization.json')))

    def test_truncated_or_multiframe_or_non_json_data_cannot_be_completed(self):
        malformed = [
            '<<<BUDGET_REQUEST>>>\n{"tool":"evaluate_design","document":',
            '<<<WORK_MODEL>>>\n{"items":[}',
            '<<<WORK_MODEL>>>\n{"items":[] oops}',
            '<<<WORK_MODEL>>>\n{"items":1+2}',
            'narration\n<<<WORK_MODEL>>>\n{"items":[] "workloads":[]}',
            '<<<WORK_MODEL>>>\n{"items":[] "workloads":[]}\nextra narration',
            '<<<WORK_MODEL>>>\n{"items":[] "workloads":[]}\n{"second":true}',
            '<<<WORK_MODEL>>>\n{"items":[] "workloads":[]}\n<<<WORK_MODEL>>>\n{}',
            '<tool_call>evaluate_design</tool_call>\n<<<WORK_MODEL>>>\n{"items":[] "workloads":[]}',
            '<<<ARCHITECTURE_DATA>>>\n{"evaluated_design_sha256":"abc" "extra":0}\n<<<ARCHITECTURE_MD>>>\n# Design',
        ]
        for raw in malformed:
            with self.subTest(raw=raw):
                self.charges.reset_mock()
                parser = (lambda text: parse_payload(text.split('<<<WORK_MODEL>>>', 1)[-1])) if '<<<WORK_MODEL>>>' in raw else design_session.parse_turn
                with self.assertRaises(ValueError):
                    self.invoke(raw, parser=parser)
                self.assertEqual(self.charges.call_count, 1)
                self.assertEqual(len(self.dispatches), 1)

    def test_content_field_order_and_nesting_changes_are_rejected(self):
        raw = '<<<WORK_MODEL>>>\n{"id":"multiply" "operations":"rows*columns","values":[1,2],"nested":{"width":16}}'
        alternatives = [
            '{"id":"add","operations":"rows*columns","values":[1,2],"nested":{"width":16}}',
            '{"id":"multiply","operations":"rows+columns","values":[1,2],"nested":{"width":16}}',
            '{"operations":"rows*columns","id":"multiply","values":[1,2],"nested":{"width":16}}',
            '{"id":"multiply","operations":"rows*columns","values":[2,1],"nested":{"width":16}}',
            '{"id":"multiply","operations":"rows*columns","values":[1,2],"nested":{"width":32}}',
            '{"id":"multiply","operations":"rows*columns","values":[1,2],"nested":[{"width":16}]}',
            '{"id":"multiply","operations":"rows*columns","values":[1,2]}',
            '{"id":"multiply","operations":"rows*columns","values":[1,2],"nested":{"width":16},"new":true}',
        ]
        for fixed in alternatives:
            with self.subTest(fixed=fixed):
                self.charges.reset_mock()
                with self.assertRaises(ValueError):
                    self.invoke(raw, fixed, lambda text: parse_payload(text.split('<<<WORK_MODEL>>>', 1)[-1]))
                self.assertEqual(self.charges.call_count, 2)
                self.assertEqual((self.out / 'response.call-1.response.txt').read_text(), raw)

    def test_missing_value_is_not_an_allowed_punctuation_repair(self):
        raw = '<<<WORK_MODEL>>>\n{"id":"work" "repetitions":}'
        with self.assertRaises(ValueError):
            self.invoke(raw, '{"id":"work","repetitions":1}', lambda text: parse_payload(text.split('<<<WORK_MODEL>>>', 1)[-1]))
        self.assertEqual(self.charges.call_count, 1)
        record = json.loads((self.out / 'response.call-1.normalization.json').read_text())
        self.assertFalse(record['accepted'])
        self.assertFalse(record['repair_called'])
        self.assertEqual(record['error_class'], 'ineligible_payload')

    def test_syntax_repair_does_not_override_the_protocol_validator(self):
        raw = '<<<BUDGET_REQUEST>>>\n{"tool":"unsupported" "document":{}}'
        with self.assertRaises(ValueError):
            self.invoke(raw, '{"tool":"unsupported","document":{}}', design_session.parse_turn)
        self.assertEqual(self.charges.call_count, 2)
        record = json.loads((self.out / 'response.call-1.normalization.json').read_text())
        self.assertTrue(record['values_order_nesting_preserved'])
        self.assertFalse(record['accepted'])
        self.assertEqual(record['error_class'], 'protocol_rejected')
        self.assertEqual((self.out / 'response.call-1.response.txt').read_text(), raw)

    def test_repair_dispatch_respects_the_existing_total_call_budget(self):
        raw = '<<<BUDGET_REQUEST>>>\n{"tool":"evaluate_design" "document":{}}'
        self.charges.side_effect = [None, RuntimeError('existing total model-call budget exhausted')]
        with self.assertRaisesRegex(RuntimeError, 'model-call budget exhausted'):
            self.invoke(raw, '{"tool":"evaluate_design","document":{}}', design_session.parse_turn)
        self.assertEqual(self.charges.call_count, 2)
        self.assertEqual(len(self.dispatches), 1)
        self.assertEqual(len(list(self.out.glob('*.call.json'))), 1)
        self.assertEqual((self.out / 'response.call-1.response.txt').read_text(), raw)
        record = json.loads((self.out / 'response.call-1.normalization.json').read_text())
        self.assertFalse(record['accepted'])
        self.assertEqual(record['error_class'], 'repair_failed')
        self.assertEqual(record['error_type'], 'RuntimeError')

    def test_invalid_repair_output_is_not_repaired_a_second_time(self):
        raw = '<<<BUDGET_REQUEST>>>\n{"tool":"evaluate_design" "document":{}}'
        with self.assertRaises(ValueError):
            self.invoke(raw, '{"tool":"evaluate_design","document":', design_session.parse_turn)
        self.assertEqual(self.charges.call_count, 2)
        self.assertEqual(len(self.dispatches), 2)
        record = json.loads((self.out / 'response.call-1.normalization.json').read_text())
        self.assertFalse(record['accepted'])
        self.assertEqual(record['error_class'], 'repair_failed')
        self.assertEqual(record['error_type'], 'JSONDecodeError')

    def test_complete_json_with_duplicate_keys_or_nonfinite_numbers_is_not_repaired(self):
        for payload in ('{"items":[],"items":[]}', '{"value":NaN}', '{"value":Infinity}', '{"value":1e999}'):
            with self.subTest(payload=payload):
                self.charges.reset_mock()
                with self.assertRaises(ValueError):
                    self.invoke('<<<WORK_MODEL>>>\n' + payload,
                                parser=lambda text: parse_payload(text.split('<<<WORK_MODEL>>>', 1)[-1]))
                self.assertEqual(self.charges.call_count, 1)
                self.assertEqual(len(self.dispatches), 1)
        self.assertFalse(list(self.out.glob('*.normalization.json')))

    def test_complete_json_fence_and_protocol_marker_survive_syntax_repair(self):
        raw = '<<<WORK_MODEL>>>\n```json\n{"workloads":[] "items":[]}\n```'
        value, dispatches = self.invoke(raw, '{"workloads":[],"items":[]}',
            lambda text: parse_payload(text.split('<<<WORK_MODEL>>>', 1)[-1]))
        self.assertEqual(value, {'workloads': [], 'items': []})
        self.assertEqual(len(dispatches), 2)
        self.assert_saved_original_and_normalization(raw)
        normalized = (self.out / 'response.call-1.normalized.response.txt').read_text()
        self.assertTrue(normalized.startswith('<<<WORK_MODEL>>>\n```json\n'))
        self.assertTrue(normalized.endswith('\n```'))

    def test_evidence_role_never_recursively_repairs_itself(self):
        raw = '{"a":1 "b":2}'
        with self.assertRaises(ValueError):
            self.invoke(raw, role='evidence')
        self.assertEqual(self.charges.call_count, 1)

    def test_repair_response_cannot_change_protocol_kind(self):
        raw = '<<<AUDIT_REPORT>>>\n{"verdict":"revise" "findings":[]}'
        fixed = '<<<AUDIT_TOOL_REQUEST>>>\n{"tool":"lookup_source","queries":[]}'
        with self.assertRaises(ValueError):
            self.invoke(raw, fixed, audit_session.parse_turn, role='review')
        self.assertEqual(self.charges.call_count, 2)

    def test_token_scanner_rejects_unknown_characters_instead_of_skipping(self):
        for raw in ('{"x":1oops}', '{"x":1+2}', '{"x":1;2}', 'prefix {"x":1}', '{"x":1} suffix',
                    '{"x":1}{"y":2}', '{"x":NaN}', '{"x":Infinity}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                omp.serialization_tokens(raw)

    def test_numeric_tokens_compare_exactly_beyond_float_precision(self):
        self.assertNotEqual(omp.serialization_tokens('{"n":9007199254740993.0}'),
                            omp.serialization_tokens('{"n":9007199254740992.0}'))
        self.assertNotEqual(omp.serialization_tokens('{"n":1.0000000000000000001}'),
                            omp.serialization_tokens('{"n":1.0000000000000000002}'))
        self.assertEqual(omp.serialization_tokens('{"n":0x10}'), omp.serialization_tokens('{"n":16}'))
        self.assertEqual(omp.serialization_tokens('{"n":1e-8}'), omp.serialization_tokens('{"n":0.00000001}'))


if __name__ == '__main__':
    unittest.main()
