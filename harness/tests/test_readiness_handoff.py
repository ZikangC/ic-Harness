"""Offline explicit handoff provenance and failure guards; no EDA or provider."""
import copy
import json
import unittest
from unittest.mock import patch

from harness.runtime import rtl_readiness as readiness
from harness.runtime import rtl_readback_store as store
from harness.runtime.rtl_worker import dispatch
from harness.tests import test_readiness_finalization as finalization


class HandoffTests(unittest.TestCase):
    tearDown = finalization.FinalizationTests.tearDown
    begin = finalization.FinalizationTests.begin
    session = finalization.FinalizationTests.session
    candidate = finalization.FinalizationTests.candidate
    watchdog_session = finalization.FinalizationTests.watchdog_session
    finalization = finalization.FinalizationTests.finalization

    def setUp(self):
        finalization.FinalizationTests.setUp(self)
        self.active = None

    def call(self, **request):
        if request.get('action') == 'submit' and self.active:
            request.update(session_id=self.active['actor']['session_id'],
                           session_actor_sha256=self.active['actor_sha256'])
        return dispatch(self.root, self.runid, request)

    def prepare(self):
        campaign, verified, sessions = self.candidate()
        name = 'active-readiness'
        self.call(action='put_evidence', path='control/' + name + '.prompt.txt', content='Bounded explicit handoff fixture')
        self.active = self.call(action='rtl_session_begin', session_id=name, role='rtl_readiness',
                                scope=campaign['work_scope'], model_role='implementation')
        job_id, verified = self.finalization(campaign, verified)
        request = dict(session_id=name, actor_sha256=self.active['actor_sha256'],
                       campaign=campaign['campaign'], job_id=job_id)
        return campaign, verified, sessions, request

    def evidence(self, campaign, request, response):
        result = dict(role='implementation', completed=False, handoff_complete=True, exit_code=143,
                      stop_reason='toolUse', termination_reason='verified_handoff', tool_isolation_ok=True,
                      budget_exhausted=False, budget_reason=None, assistant_turns=5, elapsed_seconds=3.,
                      readiness_limits={key: campaign['budgets'][key] for key in ('assistant_turns', 'session_seconds')},
                      handoff_receipt_sha256=response['receipt_sha256'], handoff_job_id=request['job_id'])
        events = [dict(type='message_end', message={'role': 'assistant', 'stopReason': 'toolUse',
                      'content': [{'type': 'text', 'text': 'Not a completion witness.'}]}),
                  dict(type='tool_execution_start', toolCallId='handoff-call', toolName='rtl_remote',
                       args={'action': 'handoff', 'job_id': request['job_id']}),
                  dict(type='tool_execution_end', toolCallId='handoff-call', toolName='rtl_remote', isError=False,
                       result={'content': [{'type': 'text', 'text': json.dumps(response)}]})]
        return result, events

    def terminal_files(self, request, result, events):
        name = request['session_id']
        for suffix, value in [('result.json', json.dumps(result)),
                              ('isolation.json', json.dumps({'blocked': [], 'checks': [{'active_tools': ['rtl_remote']}]})),
                              ('events.jsonl', ''.join(json.dumps(e) + '\n' for e in events))]:
            self.call(action='put_evidence', path='control/' + name + '.' + suffix, content=value)

    def test_explicit_bound_handoff_adopts_after_separate_natural_review(self):
        campaign, verified, sessions, request = self.prepare()
        with patch.object(readiness, 'verify_finalization', return_value=verified):
            response = self.call(action='rtl_readiness_handoff', **request)
            result, events = self.evidence(campaign, request, response)
            self.terminal_files(request, result, events)
            ended = self.call(action='rtl_session_end', session_id=request['session_id'])
            self.assertFalse(ended['completed'])
            self.assertTrue(ended['handed_off'])
            self.assertIn(response['receipt_path'], ended['source_pins'])
            actor_status = readiness._actor_status(self.out, request['session_id'], 'rtl_readiness', campaign['work_scope'])
            self.assertTrue(actor_status['handed_off'])
            self.assertFalse(actor_status['completed'])
            completed = self.call(action='rtl_readiness_complete', campaign=campaign['campaign'],
                                  finalization_job_id=request['job_id'],
                                  implementation_session_id=request['session_id'],
                                  review_session_id=sessions['review_session_id'])
            self.assertTrue(completed['adopted'])
            self.assertEqual(self.call(action='rtl_readiness_handoff_status', **request), response)
            self.assertTrue(readiness._actor_status(self.out, request['session_id'], 'rtl_readiness',
                                                   campaign['work_scope'])['handed_off'])

    def test_commit_needs_registered_active_actor_idle_baseline_and_exact_bound_job(self):
        campaign, verified, _, request = self.prepare()
        for changed in ({'session_id': 'unknown'}, {'actor_sha256': 'wrong'}, {'campaign': 'campaign-2'},
                        {'job_id': '0'}, {'job_id': '../escape'}):
            with self.subTest(changed=changed), patch.object(readiness, 'verify_finalization', return_value=verified):
                with self.assertRaises((ValueError, FileNotFoundError)):
                    self.call(action='rtl_readiness_handoff', **(request | changed))
        job_path = self.out / 'jobs' / request['job_id'] / 'request.json'
        original = json.loads(job_path.read_text())
        for changed in ({'session_id': 'another'}, {'session_actor_sha256': 'wrong'}, {'session_id': None}):
            job_path.write_text(json.dumps(original | changed))
            with self.subTest(changed=changed), patch.object(readiness, 'verify_finalization', return_value=verified):
                with self.assertRaisesRegex(ValueError, 'different session'):
                    self.call(action='rtl_readiness_handoff', **request)
        job_path.write_text(json.dumps(original))
        baseline = self.rtl / 'block.sv'; original_baseline = baseline.read_bytes()
        baseline.write_text('changed live baseline')
        with self.assertRaisesRegex(ValueError, 'baseline changed'):
            self.call(action='rtl_readiness_handoff', **request)
        baseline.write_bytes(original_baseline)
        queued = self.call(action='submit', scope=campaign['work_scope'], argv=['python', '-c', 'pass'], timeout_seconds=1)
        with patch.object(readiness, 'verify_finalization', return_value=verified), self.assertRaisesRegex(ValueError, 'unfinished jobs'):
            self.call(action='rtl_readiness_handoff', **request)
        (self.out / 'jobs' / queued['job_id'] / 'result.json').write_text(json.dumps({'exit_code': 0}))
        campaign_path = readiness.metadata_path(self.out, campaign['campaign'])
        value = json.loads(campaign_path.read_text()); value['state'] = 'evidence_incomplete'
        campaign_path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'not active'):
            self.call(action='rtl_readiness_handoff', **request)

    def test_probe_only_positive_is_rejected_before_actor_handoff_and_can_be_corrected(self):
        campaign, verified, _, request = self.prepare()
        probe = copy.deepcopy(verified)
        name = 'probes/probe_merge.sv'
        tree = self.out / campaign['work_scope'] / 'probe/delivery/candidate'
        target = tree / name; target.parent.mkdir(parents=True)
        target.write_text('module probe_merge; wire x = 1; endmodule\n')
        probe.update(allowed_paths=[name], changes=[name],
                     baseline_files={name: verified['baseline_files']['rtl/block.sv']},
                     files={name: {'sha256': readiness.sha(target), 'size': target.stat().st_size}})
        with patch.object(readiness, 'verify_finalization', return_value=probe), \
                self.assertRaisesRegex(ValueError, 'probe-only evidence cannot be integrated'):
            self.call(action='rtl_readiness_handoff', **request)
        control = self.out / 'control/sessions'
        self.assertFalse((control / (request['session_id'] + '.handoff.json')).exists())
        self.assertFalse((control / (request['session_id'] + '.end.json')).exists())
        self.assertEqual(readiness.load(self.out)['state'], 'active')
        self.assertEqual(readiness.source_inventory(self.out / 'work'), readiness.load(self.out)['source_files'])
        # Rejection leaves the same actor active; a valid original-source bundle
        # can be handed off without a replacement actor or forced completion.
        with patch.object(readiness, 'verify_finalization', return_value=verified):
            self.assertTrue(self.call(action='rtl_readiness_handoff', **request)['accepted'])

    def test_positive_handoff_reuses_original_path_hash_change_and_candidate_byte_guards(self):
        campaign, verified, _, request = self.prepare()
        cases = []
        stale = copy.deepcopy(verified)
        stale['baseline_files']['rtl/block.sv']['sha256'] = '0' * 64
        cases.append((stale, 'matching original baseline'))
        wrong_allowed = copy.deepcopy(verified)
        wrong_allowed['allowed_paths'].append('tests/equivalence.py')
        cases.append((wrong_allowed, 'matching original baseline'))
        outside = copy.deepcopy(verified)
        outside['changes'] = ['rtl/other.sv']
        cases.append((outside, 'change set is invalid'))
        changed = copy.deepcopy(verified)
        changed['files']['rtl/block.sv']['sha256'] = '0' * 64
        cases.append((changed, 'candidate RTL changed'))
        for candidate, message in cases:
            with self.subTest(message=message), patch.object(readiness, 'verify_finalization', return_value=candidate), \
                    self.assertRaisesRegex(ValueError, message):
                self.call(action='rtl_readiness_handoff', **request)
            self.assertFalse((self.out / 'control/sessions' / (request['session_id'] + '.handoff.json')).exists())

    def test_probe_only_negative_finalization_keeps_existing_handoff_behavior(self):
        campaign, verified, _, request = self.prepare()
        delivery = self.out / campaign['work_scope'] / 'probe/delivery'
        negative = finalization.bind_comparison(delivery, dict(verified, decision='keep_baseline',
            allowed_paths=['probes/probe_merge.sv'], changes=['probes/probe_merge.sv'],
            baseline_files={}, files={}))
        with patch.object(readiness, 'verify_finalization', return_value=negative):
            response = self.call(action='rtl_readiness_handoff', **request)
            self.assertTrue(response['accepted'])
            self.assertEqual(self.call(action='rtl_readiness_handoff_status', **request), response)

    def test_actor_bound_submission_cannot_impersonate_another_actor(self):
        campaign, _, _, request = self.prepare()
        self.active['actor_sha256'] = 'wrong'
        with self.assertRaisesRegex(ValueError, 'actor binding'):
            self.call(action='submit', scope=campaign['work_scope'], argv=['python', '-c', 'pass'], timeout_seconds=1)
        self.active['actor_sha256'] = request['actor_sha256']
        self.active['actor']['session_id'] = 'readiness-review'
        with self.assertRaisesRegex(ValueError, 'actor binding'):
            self.call(action='submit', scope=campaign['work_scope'], argv=['python', '-c', 'pass'], timeout_seconds=1)

    def test_ambiguous_finalizations_cannot_be_handed_off_even_with_explicit_job(self):
        campaign, verified, _, request = self.prepare()
        second_job, second = self.finalization(campaign, verified, delivery='probe/other',
                                               comparison_updates={'baseline_jobs': ['x', 'y', 'z']})
        first = dict(verified)
        with patch.object(readiness, 'verify_finalization', side_effect=[first, second]):
            with self.assertRaisesRegex(ValueError, 'multiple distinct'):
                self.call(action='rtl_readiness_handoff', **request)
        self.assertNotEqual(request['job_id'], second_job)

    def test_naturally_ended_actor_cannot_commit_a_late_handoff(self):
        campaign, verified, _, request = self.prepare()
        self.terminal_files(request, dict(role='implementation', completed=True, exit_code=0,
                                         stop_reason='stop', tool_isolation_ok=True), [])
        self.call(action='rtl_session_end', session_id=request['session_id'])
        with patch.object(readiness, 'verify_finalization', return_value=verified), self.assertRaisesRegex(ValueError, 'already ended'):
            self.call(action='rtl_readiness_handoff', **request)
        with self.assertRaisesRegex(ValueError, 'actor binding'):
            self.call(action='submit', scope=campaign['work_scope'], argv=['python', '-c', 'pass'], timeout_seconds=1)

    def test_receipt_and_fresh_bundle_tampering_fail_status(self):
        campaign, verified, _, request = self.prepare()
        with patch.object(readiness, 'verify_finalization', return_value=verified):
            response = self.call(action='rtl_readiness_handoff', **request)
            self.assertEqual(response, self.call(action='rtl_readiness_handoff', **request))
            receipt = self.out / response['receipt_path']
            original = receipt.read_bytes(); value = json.loads(original); value['job_id'] = '0'
            receipt.write_bytes(store._encoded(value))
            with self.assertRaisesRegex(ValueError, 'receipt or source binding'):
                self.call(action='rtl_readiness_handoff_status', **request)
            receipt.write_bytes(original)
            report = self.out / campaign['work_scope'] / 'probe/delivery/finalization.json'
            report.write_text('{"kind":"changed"}')
            with self.assertRaisesRegex(ValueError, 'receipt or source binding'):
                self.call(action='rtl_readiness_handoff_status', **request)
        with patch.object(readiness, 'verify_finalization', side_effect=ValueError('failed fresh proof')):
            with self.assertRaisesRegex(ValueError, 'not uniquely verified'):
                self.call(action='rtl_readiness_handoff_status', **request)

    def test_terminal_requires_real_pair_correct_result_and_budget(self):
        cases = ('no_pair', 'prose_pair', 'different_call', 'different_job', 'error', 'wrong_public',
                 'wrong_sha', 'exhausted', 'turn_limit', 'wall_limit', 'raised_limit', 'natural_claim')
        for case in cases:
            with self.subTest(case=case):
                self.tearDown(); self.setUp()
                campaign, verified, _, request = self.prepare()
                with patch.object(readiness, 'verify_finalization', return_value=verified):
                    response = self.call(action='rtl_readiness_handoff', **request)
                    result, events = self.evidence(campaign, request, response)
                    if case == 'no_pair': events = events[:1]
                    elif case == 'prose_pair': events = [dict(type='message_end', message={'role': 'assistant', 'content': events})]
                    elif case == 'different_call': events[-1]['toolCallId'] = 'other'
                    elif case == 'different_job': events[-2]['args']['job_id'] = '0'
                    elif case == 'error': events[-1]['isError'] = True
                    elif case == 'wrong_public': events[-1]['result']['content'][0]['text'] = '{}'
                    elif case == 'wrong_sha': result['handoff_receipt_sha256'] = 'wrong'
                    elif case == 'exhausted': result['budget_exhausted'] = True
                    elif case == 'turn_limit': result['assistant_turns'] = result['readiness_limits']['assistant_turns']
                    elif case == 'wall_limit': result['elapsed_seconds'] = result['readiness_limits']['session_seconds']
                    elif case == 'raised_limit': result['readiness_limits']['assistant_turns'] += 1
                    elif case == 'natural_claim': result['completed'] = True
                    self.terminal_files(request, result, events)
                    with self.assertRaises(ValueError):
                        self.call(action='rtl_session_end', session_id=request['session_id'])

    def test_old_budget_exhaustion_is_never_salvaged_by_artifacts(self):
        campaign, verified, _, request = self.prepare()
        old = self.watchdog_session(campaign)
        actor, path = store._actor(self.out, old)
        with patch.object(readiness, 'verify_finalization', return_value=verified):
            with self.assertRaisesRegex(ValueError, 'already ended'):
                self.call(action='rtl_readiness_handoff', **(request | {'session_id': old, 'actor_sha256': readiness.sha(path)}))
            with self.assertRaisesRegex(ValueError, 'incomplete'):
                readiness._require_actor(self.out, old, 'rtl_readiness', campaign['work_scope'])
        status = readiness._actor_status(self.out, old, 'rtl_readiness', campaign['work_scope'])
        self.assertFalse(status['completed']); self.assertFalse(status['handed_off'])
        self.assertTrue(status['budget_exhausted'])

    def test_ended_handoff_source_result_event_and_receipt_edits_invalidate_actor(self):
        for changed in ('result', 'events', 'receipt', 'job'):
            with self.subTest(changed=changed):
                self.tearDown(); self.setUp()
                campaign, verified, _, request = self.prepare()
                with patch.object(readiness, 'verify_finalization', return_value=verified):
                    response = self.call(action='rtl_readiness_handoff', **request)
                    result, events = self.evidence(campaign, request, response)
                    self.terminal_files(request, result, events)
                    self.call(action='rtl_session_end', session_id=request['session_id'])
                    if changed == 'result':
                        target = self.out / 'control' / (request['session_id'] + '.result.json')
                    elif changed == 'events':
                        target = self.out / 'control' / (request['session_id'] + '.events.jsonl')
                    elif changed == 'receipt': target = self.out / response['receipt_path']
                    else: target = self.out / 'jobs' / request['job_id'] / 'result.json'
                    target.write_bytes(target.read_bytes() + b'\n')
                    self.assertFalse(readiness._actor_status(self.out, request['session_id'], 'rtl_readiness',
                                                            campaign['work_scope'])['available'])
                    with self.assertRaises(ValueError):
                        readiness._require_actor(self.out, request['session_id'], 'rtl_readiness', campaign['work_scope'])

    def test_review_actor_cannot_use_controlled_handoff_completion(self):
        campaign = self.begin(); name = 'active-review'
        self.call(action='put_evidence', path='control/' + name + '.prompt.txt', content='Independent review fixture')
        self.call(action='rtl_session_begin', session_id=name, role='readiness_review',
                  scope=campaign['audit_scope'], model_role='review')
        result = dict(role='review', completed=False, handoff_complete=True, exit_code=143,
                      stop_reason='toolUse', tool_isolation_ok=True)
        self.terminal_files({'session_id': name}, result, [])
        with self.assertRaisesRegex(ValueError, 'only readiness implementation'):
            self.call(action='rtl_session_end', session_id=name)


if __name__ == '__main__':
    unittest.main()
