"""Strict report framing and native outcome transport; no model or EDA runs."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from harness.adapters import readiness_diagnosis as adapter
from harness.adapters.rtl_remote import Remote
from harness.runtime import rtl_diagnosis as native
from harness.runtime.rtl_diagnosis_report import parse_report
from harness.tests import test_readiness_diagnosis_native as native_fixtures


REPORT = {'advice': 'small_experiment', 'summary': '只读诊断已完成；测试小型局部实验。',
          'findings': [{'observation': 'Observed cost', 'value': 123456789012345678901234567890}],
          'experiments': [], 'limitations': ['No mapped area or STA.']}


class ReportFramingTests(unittest.TestCase):
    def test_plain_whole_fence_and_short_prose_fence_preserve_exact_engineering_values(self):
        raw = json.dumps(REPORT, ensure_ascii=False)
        for text in (raw, '\n```json\n' + raw + '\n```\n', '```\n' + raw + '\n```',
                     '已完成只读诊断，结论如下。\n\n```json\n' + raw + '\n```',
                     'A short introduction.\r\n```json\r\n' + raw + '\r\n```\r\nA short end.'):
            with self.subTest(text=text[:30]):
                self.assertEqual(parse_report(text), REPORT)
                self.assertEqual(adapter._report(text), REPORT)

    def test_adapter_and_native_actor_use_the_same_parser(self):
        text = 'Read-only investigation completed.\n```json\n' + json.dumps(REPORT) + '\n```'
        with tempfile.TemporaryDirectory() as name:
            out = Path(name); (out / 'control').mkdir()
            (out / 'control/actor.result.json').write_text(json.dumps({'final_text': text}))
            self.assertEqual(adapter._report(text), native._actor_report(out, 'actor'))

    def test_multiple_objects_or_fences_and_unframed_prose_remain_errors(self):
        raw = json.dumps(REPORT)
        fence = '```json\n' + raw + '\n```'
        for text in (raw + '\n' + raw, fence + '\n' + fence, raw + '\n' + fence,
                     '{}\n' + fence, fence + '\n{}', 'Here is a report: ' + raw,
                     '```json\n' + raw, '```python\n' + raw + '\n```',
                     '```json\n' + raw + '\n' + raw + '\n```', fence + '\nextra ```json',
                     'x' * 4097 + '\n' + fence):
            with self.subTest(text=text[:35]), self.assertRaises(ValueError):
                parse_report(text)

    def test_non_finite_numbers_duplicate_keys_and_syntax_repair_are_rejected(self):
        for raw in ('{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}', '{"x":1e400}',
                    '{"x":1,"x":2}', '{"nested":{"x":1,"x":2}}',
                    '{"x":1,}', '{"x":1 "y":2}', '[]'):
            for text in (raw, 'A report.\n```json\n' + raw + '\n```'):
                with self.subTest(text=text), self.assertRaises(ValueError):
                    parse_report(text)

    def test_json_string_containing_backticks_is_preserved(self):
        value = REPORT | {'quoted_syntax': 'A literal ``` marker, {object}, [array].'}
        self.assertEqual(parse_report('Report.\n```json\n' + json.dumps(value) + '\n```'), value)


class NativeReportOutcomeTests(unittest.TestCase):
    setUp = native_fixtures.DiagnosisNativeTests.setUp
    tearDown = native_fixtures.DiagnosisNativeTests.tearDown
    call = native_fixtures.DiagnosisNativeTests.call
    prepare = native_fixtures.DiagnosisNativeTests.prepare

    def actor(self, text, observation):
        label = 'report-framing-actor'
        self.call(action='put_evidence', path='control/' + label + '.prompt.txt', content='Bounded readonly diagnosis')
        self.call(action='rtl_session_begin', session_id=label, role='readiness_diagnosis', scope='work',
                  model_role='implementation', diagnosis_observation_id=observation['observation_id'],
                  diagnosis_fingerprint=observation['fingerprint'])
        for suffix, value in [('result.json', {'role': 'implementation', 'completed': True,
                    'exit_code': 0, 'stop_reason': 'stop', 'tool_isolation_ok': True, 'final_text': text}),
                ('isolation.json', {'checks': [{'active_tools': ['rtl_remote']}], 'blocked': []}),
                ('events.jsonl', '')]:
            self.call(action='put_evidence', path='control/' + label + '.' + suffix,
                      content=value if isinstance(value, str) else json.dumps(value))
        self.call(action='rtl_session_end', session_id=label)
        return label

    def test_single_fenced_report_with_prose_is_bound_to_original_actor_and_saved(self):
        observation = self.prepare()
        text = '已完成只读调查。\n```json\n' + json.dumps(REPORT) + '\n```'
        session = self.actor(text, observation)
        result = self.call(action='rtl_diagnosis_complete', observation_id=observation['observation_id'],
                           session_id=session, report=adapter._report(text))
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['report'], REPORT)
        self.assertEqual(json.loads((self.out / result['report_path']).read_text()), REPORT)
        self.assertNotIn('error', result)

    def test_ambiguous_actor_report_stays_incomplete_without_reserved_transport_error(self):
        observation = self.prepare()
        text = '```json\n' + json.dumps(REPORT) + '\n```\n```json\n{}\n```'
        session = self.actor(text, observation)
        result = self.call(action='rtl_diagnosis_complete', observation_id=observation['observation_id'],
                           session_id=session, report=REPORT)
        self.assertEqual(result['state'], 'incomplete')
        self.assertIn('diagnosis_error', result)
        self.assertNotIn('error', result)
        self.assertNotIn('report', result)
        state = json.loads((self.out / Path(result['index_path']).parent / 'state.json').read_text())
        self.assertIn('error', state)
        self.assertEqual(state['state'], 'incomplete')
        self.assertFalse((self.out / Path(result['index_path']).parent / 'report.json').exists())
        env = {'remote': {'container_workspace_root': '/work', 'project_name': 'fixture',
                         'connect_timeout_seconds': 8, 'ssh_host': 'fixture', 'container': 'fixture'},
               'eda': {'python': '/fixture/python'}}
        remote = Remote(env, 'run')
        with patch('harness.adapters.rtl_remote.subprocess.run', return_value=SimpleNamespace(
                stdout=json.dumps(result), returncode=0)):
            self.assertEqual(remote._worker_call({'action': 'rtl_diagnosis_complete'}), result)


if __name__ == '__main__':
    unittest.main()
