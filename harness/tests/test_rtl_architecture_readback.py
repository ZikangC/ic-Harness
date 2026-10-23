"""Synthetic public OMP result streams; no model, server, DUT or EDA execution."""
import copy
import hashlib
import json
import unittest

from harness.runtime.rtl_readback import verify_readback


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class ArchitectureReadbackTests(unittest.TestCase):
    def setUp(self):
        self.path = 'architecture/revision-2/baseline/architecture.md'
        self.body = 'First commitment.\nSecond commitment.\n'.encode()
        self.packet = {'run_id': 'run-example', 'parent_session_id': 'parent-7',
                       'baseline_path': 'architecture/revision-2/baseline', 'baseline_sha256': 'a' * 64,
                       'handoff_path': 'architecture/revision-2/handoff.json', 'handoff_sha256': 'b' * 64,
                       'required_files': [{'resolved_run_path': self.path, 'sha256': sha(self.body), 'bytes': len(self.body)}]}

    def read(self, call_id, start, end, body=None, path=None, response=None, request=None):
        raw = self.body if body is None else body
        page = {'resolved_run_path': path or self.path, 'path': '@architecture/architecture.md',
                'sha256': sha(raw), 'bytes': len(raw), 'returned_range': {'byte_start': start, 'byte_end': end},
                'content': raw[start:end].decode(), 'eof': end == len(raw),
                'next_cursor': None if end == len(raw) else 'opaque-cursor'}
        if response:
            page.update(response)
        return [{'type': 'tool_execution_start', 'toolCallId': call_id, 'toolName': 'rtl_remote',
                 'args': request or {'action': 'read', 'path': '@architecture/architecture.md'}},
                {'type': 'tool_execution_end', 'toolCallId': call_id, 'toolName': 'rtl_remote', 'isError': False,
                 'result': {'content': [{'type': 'text', 'text': json.dumps(page)}], 'details': {'ignored': True}}}]

    def check(self, events, session=None, packet=None, raw=None):
        if raw is None:
            raw = ''.join(json.dumps(event, ensure_ascii=False) + '\n' for event in events).encode()
        context = {'run_id': 'run-example', 'session_id': 'parent-7', 'role': 'implementation', 'scope': 'work',
                   'completed': True, 'tool_isolation_ok': True, 'events_sha256': sha(raw)}
        context.update(session or {})
        return verify_readback(raw, context, self.packet if packet is None else packet)

    def test_exact_public_pages_cover_all_files_without_emitting_bodies(self):
        second = b'{"decisive": "binding"}\n'
        packet = copy.deepcopy(self.packet)
        packet['handoff_sha256'] = sha(second)
        packet['required_files'].append({'resolved_run_path': packet['handoff_path'], 'sha256': sha(second), 'bytes': len(second)})
        events = self.read('one', 0, 18) + self.read('two', 18, len(self.body))
        events += self.read('handoff', 0, len(second), body=second, path=packet['handoff_path'])
        witness = self.check(events, packet=packet)
        self.assertTrue(witness['complete'], witness)
        self.assertEqual(witness['files'][0]['ranges'], [[0, len(self.body)]])
        self.assertEqual(witness['files'][0]['gaps'], [])
        self.assertEqual(witness['successful_read_pages'], 3)
        encoded = json.dumps(witness)
        self.assertNotIn('First commitment', encoded)
        self.assertNotIn('decisive', encoded)

    def test_final_page_and_arbitrary_cursor_cannot_hide_a_gap(self):
        for events in (self.read('last', 18, len(self.body)),
                       self.read('first', 0, 5) + self.read('last', 18, len(self.body), request={'action': 'read', 'path': '@architecture/architecture.md', 'cursor': 'forged-offset'})):
            with self.subTest(events=len(events)):
                witness = self.check(events)
                self.assertFalse(witness['complete'])
                self.assertTrue(witness['files'][0]['gaps'])

    def test_repeated_first_page_does_not_accumulate_byte_coverage(self):
        witness = self.check(self.read('one', 0, 10) + self.read('two', 0, 10))
        self.assertFalse(witness['complete'])
        self.assertEqual(witness['files'][0]['covered_bytes'], 10)
        self.assertEqual(witness['files'][0]['successful_pages'], 2)

    def test_overlapping_identical_pages_are_allowed_once_all_bytes_are_exposed(self):
        witness = self.check(self.read('one', 0, 24) + self.read('two', 18, len(self.body)))
        self.assertTrue(witness['complete'], witness)
        self.assertEqual(witness['files'][0]['covered_bytes'], len(self.body))

    def test_wrong_version_pages_cannot_complete_expected_packet(self):
        other = self.body[:-1] + b'!'
        witness = self.check(self.read('old-head', 0, 18, body=other) + self.read('new-tail', 18, len(self.body)))
        self.assertFalse(witness['complete'])
        self.assertEqual(witness['files'][0]['gaps'], [[0, 18]])
        self.assertEqual(witness['rejected_read_pages'], 1)

    def test_context_role_not_model_role_controls_parent_eligibility(self):
        for change in ({'role': 'architecture'}, {'role': 'review'}, {'role': 'rtl_audit'},
                       {'scope': 'architecture/revision-2/candidate'}, {'session_id': 'different-parent'},
                       {'run_id': 'different-run'}, {'completed': False}, {'tool_isolation_ok': False}):
            with self.subTest(change=change):
                self.assertFalse(self.check(self.read('one', 0, len(self.body)), session=change)['complete'])

    def test_trusted_event_digest_and_optional_event_identity_reject_stream_mixing(self):
        events = self.read('one', 0, len(self.body))
        self.assertFalse(self.check(events, session={'events_sha256': 'f' * 64})['complete'])
        self.assertFalse(self.check(events, session={'events_sha256': None})['complete'])
        events[0]['session_id'] = 'child-2'
        self.assertFalse(self.check(events)['complete'])

    def test_details_arguments_and_assistant_self_report_are_not_content_evidence(self):
        events = self.read('one', 0, len(self.body))
        events[1]['result']['details'] = json.loads(events[1]['result']['content'][0]['text'])
        events[1]['result'].pop('content')
        events[0]['args']['content'] = self.body.decode()
        events += [{'type': 'message_end', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'I read all the architecture files.'}]}}]
        witness = self.check(events)
        self.assertFalse(witness['complete'])
        self.assertEqual(witness['successful_read_pages'], 0)

    def test_error_or_nonread_tool_results_do_not_count(self):
        for mutate in ('error', 'result_error', 'write', 'other_tool'):
            events = self.read('one', 0, len(self.body))
            if mutate == 'error':
                events[1]['isError'] = True
            elif mutate == 'result_error':
                events[1]['result']['isError'] = True
            elif mutate == 'write':
                events[0]['args']['action'] = 'write'
            else:
                for event in events:
                    event['toolName'] = 'rtl_revise_architecture'
            with self.subTest(mutate=mutate):
                self.assertFalse(self.check(events)['complete'])

    def test_duplicate_unpaired_and_unfinished_calls_are_incomplete(self):
        full = self.read('one', 0, len(self.body))
        for events in (full + full, [full[1], full[0]], full + [self.read('two', 0, 1)[0]],
                       [dict(full[0], toolCallId='other'), full[1]]):
            with self.subTest(events=len(events)):
                self.assertFalse(self.check(events)['complete'])

    def test_canonical_returned_path_controls_file_binding(self):
        witness = self.check(self.read('one', 0, len(self.body), path='architecture/revision-1/baseline/architecture.md'))
        self.assertFalse(witness['complete'])
        self.assertEqual(witness['ignored_unrequired_read_pages'], 1)
        for bad in ('../baseline/a', '/absolute/a', '@architecture/architecture.md', 'architecture//a'):
            with self.subTest(path=bad):
                self.assertFalse(self.check(self.read('one', 0, len(self.body), path=bad))['complete'])

    def test_utf8_long_line_ranges_are_bytes_and_full_exposed_hash_is_verified(self):
        self.body = ('中🙂e\u0301' * 10000).encode()
        self.packet['required_files'][0].update(sha256=sha(self.body), bytes=len(self.body))
        width = len('中🙂e\u0301'.encode())
        events = []
        for index, start in enumerate(range(0, len(self.body), width * 500)):
            events += self.read(str(index), start, min(start + width * 500, len(self.body)))
        self.assertTrue(self.check(events)['complete'])
        bad = self.read('false-body', 0, len(self.body), response={'content': 'x' * len(self.body)})
        witness = self.check(bad)
        self.assertFalse(witness['complete'])
        self.assertIn('exposed_content_digest_mismatch', witness['reasons'])

    def test_empty_file_requires_actual_successful_read(self):
        self.body = b''
        self.packet['required_files'][0].update(sha256=sha(b''), bytes=0)
        self.assertFalse(self.check([])['complete'])
        self.assertTrue(self.check(self.read('empty', 0, 0))['complete'])

    def test_external_source_read_does_not_poison_complete_packet_or_credit_it(self):
        auxiliary = self.read('external', 0, len(self.body), response={'resolved_run_path': None})
        witness = self.check(auxiliary + self.read('packet', 0, len(self.body)))
        self.assertTrue(witness['complete'], witness)
        self.assertEqual(witness['ignored_unrequired_read_pages'], 1)
        self.assertFalse(self.check(auxiliary)['complete'])

    def test_required_handoff_file_cannot_contradict_packet_handoff_pin(self):
        packet = copy.deepcopy(self.packet)
        packet['required_files'][0]['resolved_run_path'] = packet['handoff_path']
        witness = self.check(self.read('bad', 0, len(self.body), path=packet['handoff_path']), packet=packet)
        self.assertFalse(witness['complete'])
        self.assertIn('required_handoff_pin_mismatch', witness['reasons'])

    def test_jsonl_preserves_literal_unicode_line_separators_in_public_strings(self):
        self.body = 'raw\u2028separator\u0085and\u2029paragraph\r\n'.encode()
        self.packet['required_files'][0].update(sha256=sha(self.body), bytes=len(self.body))
        events = self.read('one', 0, len(self.body))
        page = json.loads(events[1]['result']['content'][0]['text'])
        events[1]['result']['content'][0]['text'] = json.dumps(page, ensure_ascii=False)
        self.assertTrue(self.check(events)['complete'])

    def test_invalid_integer_range_eof_size_and_missing_canonical_metadata_reject(self):
        cases = [{'returned_range': {'byte_start': False, 'byte_end': len(self.body)}},
                 {'returned_range': {'byte_start': -1, 'byte_end': len(self.body)}},
                 {'returned_range': {'byte_start': 0, 'byte_end': len(self.body) + 1}},
                 {'bytes': True}, {'eof': False}, {'content': 'short'}, {'resolved_run_path': None}]
        for response in cases:
            with self.subTest(response=response):
                self.assertFalse(self.check(self.read('one', 0, len(self.body), response=response))['complete'])

    def test_contradictory_overlap_and_malformed_protocol_fail_closed(self):
        events = self.read('one', 0, 18) + self.read('two', 0, len(self.body), response={'content': 'x' * len(self.body)})
        self.assertFalse(self.check(events)['complete'])
        self.assertIn('conflicting_exposed_bytes', self.check(events)['reasons'])
        for suffix in ('{bad json}\n', '[]\n', '{"type":"tool_execution_progress"}\n'):
            good = ''.join(json.dumps(e) + '\n' for e in self.read('one', 0, len(self.body)))
            self.assertFalse(self.check([], raw=(good + suffix).encode())['complete'])

    def test_invalid_packet_or_missing_session_metadata_is_structured_incomplete(self):
        for packet in (None, {}, dict(self.packet, required_files=[]),
                       dict(self.packet, required_files=self.packet['required_files'] * 2),
                       dict(self.packet, baseline_path='../escape'),
                       dict(self.packet, handoff_sha256='short')):
            with self.subTest(packet=packet):
                self.assertFalse(self.check([], packet=packet if packet is not None else {})['complete'])
        self.assertFalse(verify_readback(b'', None, self.packet)['complete'])


if __name__ == '__main__':
    unittest.main()
