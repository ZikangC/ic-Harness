"""Role routing, resume and non-release outcomes, without model/EDA dependence."""
import copy
import json
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch


from harness.adapters import rtl_audit as flow
from harness.adapters.rtl_repair import implementation_repair


def run_implementation_flow(context, remote, before_freeze=None):
    """Existing implementation workflow opts into its original repair owner."""
    return flow.run(context, remote, before_freeze=before_freeze,
                    repair_handler=implementation_repair)


class Remote:
    def __init__(self):
        self.current = {'review': None}
        self.files = {}
        self.calls = []
        self.number = 0
        self.pending_batches = []
        self.job_results = {}
        self.architecture_texts = {
            'architecture.md': 'Original frozen architecture draft.\n',
            'SOURCE.md': 'Original native provenance.\n',
            'decision.json': '{"fixture_scope":"raw decision"}\n',
            'design.json': '{"fixture_design":"index only"}\n',
            'budgets.json': '{"fixture_budget":"index only"}\n',
            'review.json': '{"fixture_review":"index only"}\n',
        }
        self.architectures = {}

    def evidence(self, name, content):
        self.files['@run/control/' + name] = content

    def call(self, req):
        self.calls.append(req)
        action = req['action']
        if action == 'jobs':
            pending = self.pending_batches.pop(0) if self.pending_batches else []
            return {'jobs': [{'job_id': job, 'status': 'running'} for job in pending]}
        elif action == 'status':
            return self.job_results.get(req['job_id'], {'job_id': req['job_id'], 'status': 'completed', 'exit_code': 0})
        elif action == 'rtl_review_begin':
            self.number += 1
            n = self.number
            self.current = {'review': f'review-{n}', 'audit_scope': f'audit/review-{n}',
                            'candidate_scope': f'rtl/candidates/candidate-{n}/work',
                            'architecture_scope': f'rtl/candidates/candidate-{n}/architecture',
                            'candidate_sha256': f'candidate-{n}', 'decision': None,
                            'repair': None, 'architecture_current': True}
            scope = self.current['architecture_scope']
            entries = {name: {'type': 'file', 'sha256': sha256(text.encode()).hexdigest(), 'mode': 0o644}
                       for name, text in self.architecture_texts.items()}
            identity = {'current': {'path': 'snapshot/architecture', 'review_status': 'accepted_estimate_only'},
                        'inventory': entries}
            identity_sha = sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            self.current['architecture_identity_sha256'] = identity_sha
            files = {name: {'path': '@run/' + scope + '/' + name, 'sha256': entries[name]['sha256'],
                            'bytes': len(text.encode())} for name, text in self.architecture_texts.items()}
            primary = {name: ({'status': 'present', **files[name], 'content': self.architecture_texts[name]}
                              if name in files else {'status': 'missing', 'path': '@run/' + scope + '/' + name})
                       for name in ('architecture.md', 'SOURCE.md', 'decision.json')}
            self.architectures[self.current['review']] = {
                'version': 1, 'review': self.current['review'], 'candidate_sha256': self.current['candidate_sha256'],
                'scope': scope, 'identity': identity, 'identity_sha256': identity_sha,
                'run_current': dict(identity['current']), 'matches_run_current': True,
                'files': files, 'primary': primary}
        elif action == 'rtl_repair_begin':
            self.current['repair'] = {'status': 'active'}
        elif action == 'rtl_repair_end':
            self.current['repair'] = {'status': 'completed'}
        elif action == 'read':
            if req['path'] not in self.files:
                raise RuntimeError('not found')
            return {'text': '\n'.join(f'{i}: {line}' for i, line in
                                     enumerate(self.files[req['path']].splitlines(), 1))}
        elif action != 'rtl_review_status':
            raise AssertionError(action)
        result = dict(self.current)
        if action == 'rtl_review_status' and req.get('include_architecture_context'):
            result['architecture_context'] = copy.deepcopy(self.architectures[self.current['review']])
        return result

    def decide(self, verdict):
        self.current['decision'] = {'verdict': verdict,
                                   'report_path': self.current['audit_scope'] + '/review.json',
                                   'report_sha256': 'actual-report-hash'}


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        p = Path(self.temp.name)
        self.harness = p / 'harness'
        for rel in ['rtl-audit/SKILL.md', 'rtl-implementation/SKILL.md',
                    'rtl-implementation/references/repair.md']:
            f = self.harness / 'skills' / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text('fixture guidance')
        self.task = ('本次交付：重大效率问题未关闭时不得放行。\r\n\n'
                     'Keep the frozen specification and reference unchanged.\n')
        self.context = {'harness_dir': str(self.harness), 'cache': str(p),
                        'project_root': str(p), 'models': {}, 'remote_run_dir': '/server/run',
                        'tools': {}, 'capabilities': {}, 'reference_path': '/server/reference',
                        'input_source_paths': {}, 'task': self.task,
                        'settings': {'task_sha256': sha256(self.task.encode()).hexdigest()}}
        self.remote = Remote()

    def prepare_repair(self, remote):
        remote.call({'action': 'rtl_review_begin'})
        remote.decide('revise')
        remote.evidence('rtl-audit/review-1.audit.json', json.dumps({
            'completed': True, 'tool_isolation_ok': True,
            'candidate_sha256': remote.current['candidate_sha256'],
            'decision_report_sha256': remote.current['decision']['report_sha256']}))

    def test_audit_skill_alone_can_complete_one_review_without_implementation(self):
        for path in (self.harness / 'skills/rtl-implementation').rglob('*'):
            if path.is_file():
                path.unlink()
        def session(prompt, role, *args):
            self.assertEqual(role, 'review')
            self.assertNotIn('MODE: repair', prompt)
            self.remote.decide('pass')
            return {'completed': True, 'tool_isolation_ok': True}
        with patch.object(flow, 'run_session', side_effect=session) as invoke:
            result = flow.review_once(self.context, self.remote)
        self.assertEqual(invoke.call_count, 1)
        self.assertTrue(result['rtl_stage_release'])
        self.assertFalse(any(q['action'].startswith('rtl_repair') for q in self.remote.calls))

    def test_unconfigured_revise_preserves_bound_report_without_repair(self):
        def session(*args):
            self.remote.decide('revise')
            return {'completed': True, 'tool_isolation_ok': True}
        with patch.object(flow, 'run_session', side_effect=session) as invoke:
            result = flow.run(self.context, self.remote)
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(result['status'], 'rtl_review_finished')
        self.assertEqual(result['review_verdict'], 'revise')
        self.assertTrue(result['repair_required'])
        self.assertFalse(result['rtl_stage_release'])
        self.assertEqual(result['report_sha256'], self.remote.current['decision']['report_sha256'])
        self.assertEqual(result['candidate_sha256'], self.remote.current['candidate_sha256'])
        self.assertFalse(any(q['action'].startswith('rtl_repair') for q in self.remote.calls))
        before = copy.deepcopy(self.remote.current)
        with patch.object(flow, 'run_session', side_effect=AssertionError('duplicate audit')):
            self.assertEqual(flow.review_once(self.context, self.remote), result)
        self.assertEqual(self.remote.current, before)

    def test_explicit_handler_selects_repair_guidance_and_context(self):
        for actor in ('implementation-owner', 'readiness-owner'):
            with self.subTest(actor=actor):
                remote = Remote()
                selected = []
                guidance = self.harness / 'skills' / actor / 'SKILL.md'
                guidance.parent.mkdir(parents=True)
                guidance.write_text(actor + ' exact repair guidance')
                def prepare(context, supplied_remote, review):
                    self.assertIs(supplied_remote, remote)
                    selected.append(review['review'])
                    return flow.RepairSession(guidance.read_text(), context_updates={'repair_skill': actor})
                handler = flow.RepairHandler(actor, prepare)
                roles = []
                def session(prompt, role, models, root, context_path, *args):
                    roles.append(role)
                    if role == 'review':
                        remote.decide('revise' if remote.number == 1 else 'pass')
                    else:
                        self.assertEqual(prompt, guidance.read_text())
                        self.assertEqual(json.loads(context_path.read_text())['repair_skill'], actor)
                    return {'completed': True, 'tool_isolation_ok': True}
                with patch.object(flow, 'run_session', side_effect=session):
                    result = flow.run(self.context, remote, repair_handler=handler)
                self.assertEqual(selected, ['review-1'])
                self.assertEqual(roles, ['review', 'implementation', 'review'])
                self.assertTrue(result['rtl_stage_release'])
                receipt = json.loads(remote.files['@run/control/rtl-audit/review-1.repair.json'])
                self.assertEqual(receipt['repair_actor'], actor)

    def test_non_revision_outcomes_never_dispatch_configured_repair(self):
        for verdict in ('insufficient_evidence', 'blocked'):
            with self.subTest(verdict=verdict):
                remote = Remote()
                prepare = unittest.mock.Mock(side_effect=AssertionError('not a repair request'))
                def session(*args):
                    remote.decide(verdict)
                    return {'completed': True, 'tool_isolation_ok': True}
                with patch.object(flow, 'run_session', side_effect=session) as invoke:
                    result = flow.run(self.context, remote,
                                      repair_handler=flow.RepairHandler('fixture-owner', prepare))
                self.assertEqual(invoke.call_count, 1)
                self.assertEqual(result['review_verdict'], verdict)
                self.assertFalse(result['rtl_stage_release'])
                prepare.assert_not_called()
                self.assertFalse(any(q['action'].startswith('rtl_repair') for q in remote.calls))

    def test_changed_candidate_or_report_cannot_reuse_completed_review_receipt(self):
        for fault in ('candidate', 'report'):
            with self.subTest(fault=fault):
                remote = Remote()
                def session(*args):
                    remote.decide('pass')
                    return {'completed': True, 'tool_isolation_ok': True}
                with patch.object(flow, 'run_session', side_effect=session):
                    original = flow.review_once(self.context, remote)
                if fault == 'candidate':
                    remote.current['candidate_sha256'] = 'different-candidate'
                else:
                    remote.current['decision']['report_sha256'] = 'different-report'
                with patch.object(flow, 'run_session', side_effect=session) as invoke:
                    current = flow.review_once(self.context, remote)
                self.assertEqual(invoke.call_count, 1)
                self.assertEqual(original['review'], 'review-1')
                self.assertEqual(current['review'], 'review-2')
                self.assertTrue(current['rtl_stage_release'])

    def test_repair_round_budget_survives_resume_and_retains_latest_revise(self):
        roles = []
        def session(prompt, role, *args):
            roles.append(role)
            if role == 'review':
                self.remote.decide('revise')
            return {'completed': True, 'tool_isolation_ok': True}
        with patch.object(flow, 'run_session', side_effect=session):
            first = flow.run(self.context, self.remote, repair_handler=implementation_repair, max_repairs=1)
        self.assertEqual(roles, ['review', 'implementation', 'review'])
        self.assertEqual(first['status'], 'rtl_repair_budget_exhausted')
        self.assertEqual(first['review'], 'review-2')
        self.assertEqual(first['review_verdict'], 'revise')
        self.assertFalse(first['rtl_stage_release'])
        before = copy.deepcopy(self.remote.files)
        with patch.object(flow, 'run_session', side_effect=AssertionError('budget reset on resume')):
            resumed = flow.run(self.context, self.remote, repair_handler=implementation_repair, max_repairs=1)
        self.assertEqual(first, resumed)
        self.assertEqual(before, self.remote.files)
        self.assertEqual(sum(q['action'] == 'rtl_repair_begin' for q in self.remote.calls), 1)

    def test_failed_attempt_reuses_reserved_round_without_resetting_limit(self):
        def failed(prompt, role, *args):
            if role == 'review':
                self.remote.decide('revise')
            return {'completed': role == 'review', 'tool_isolation_ok': True}
        with patch.object(flow, 'run_session', side_effect=failed):
            first = flow.run(self.context, self.remote, repair_handler=implementation_repair, max_repairs=1)
        self.assertEqual(first['status'], 'rtl_repair_session_failure')
        roles = []
        def recovered(prompt, role, *args):
            roles.append(role)
            if role == 'review':
                self.remote.decide('pass')
            return {'completed': True, 'tool_isolation_ok': True}
        with patch.object(flow, 'run_session', side_effect=recovered):
            second = flow.run(self.context, self.remote, repair_handler=implementation_repair, max_repairs=1)
        self.assertEqual(roles, ['implementation', 'review'])
        self.assertTrue(second['rtl_stage_release'])
        ledgers = [json.loads(text) for path, text in self.remote.files.items() if '/repair-budget-' in path]
        self.assertEqual(len(ledgers), 1)
        self.assertEqual(set(ledgers[0]['rounds']), {'review-1'})
        self.assertEqual(ledgers[0]['limit'], 1)

    def test_changed_round_binding_or_budget_limit_is_rejected(self):
        self.prepare_repair(self.remote)
        review = self.remote.current
        self.assertTrue(flow._reserve_repair(self.remote, review, 'fixture-owner', 1))
        with self.assertRaisesRegex(ValueError, 'budget binding mismatch'):
            flow._reserve_repair(self.remote, review, 'fixture-owner', 2)
        for field in ('candidate_sha256', 'report_sha256'):
            changed = copy.deepcopy(review)
            if field == 'candidate_sha256':
                changed[field] = 'changed-candidate'
            else:
                changed['decision'][field] = 'changed-report'
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'stale candidate/report'):
                flow._reserve_repair(self.remote, changed, 'fixture-owner', 1)

    def test_other_executor_receipt_cannot_be_reused_for_repair(self):
        self.prepare_repair(self.remote)
        self.remote.current['repair'] = {'status': 'active'}
        self.remote.evidence('rtl-audit/review-1.repair.json', json.dumps({
            'completed': True, 'tool_isolation_ok': True, 'repair_actor': 'another-owner',
            'candidate_sha256': self.remote.current['candidate_sha256'],
            'decision_report_sha256': self.remote.current['decision']['report_sha256']}))
        with patch.object(flow, 'run_session', side_effect=AssertionError('wrong actor resumed')):
            result = flow.run(self.context, self.remote, repair_handler=implementation_repair, max_repairs=1)
        self.assertEqual(result['status'], 'rtl_repair_actor_mismatch')
        self.assertFalse(result['rtl_stage_release'])
        self.assertFalse(any(q['action'] == 'rtl_repair_begin' for q in self.remote.calls))

    def test_budget_exhausted_executor_cannot_complete_repair(self):
        def session(prompt, role, *args):
            if role == 'review':
                self.remote.decide('revise')
            return {'completed': True, 'tool_isolation_ok': True,
                    'budget_exhausted': role == 'implementation'}
        with patch.object(flow, 'run_session', side_effect=session):
            result = flow.run(self.context, self.remote, repair_handler=implementation_repair, max_repairs=1)
        self.assertEqual(result['status'], 'rtl_repair_session_failure')
        self.assertFalse(result['rtl_stage_release'])
        self.assertFalse(any(q['action'] == 'rtl_repair_end' for q in self.remote.calls))
        receipt = json.loads(self.remote.files['@run/control/rtl-audit/review-1.repair.json'])
        self.assertTrue(receipt['budget_exhausted'])

    def test_ended_original_parent_readback_gap_preserves_review_and_stops_repair(self):
        self.prepare_repair(self.remote)
        self.remote.current['parent_readback'] = {
            'required': True, 'complete': False, 'session_ended': True,
            'parent_session_id': 'original-parent', 'receipt_sha256': 'fixed-incomplete-proof'}
        original = copy.deepcopy(self.remote.current['decision'])
        with patch.object(flow, 'run_session') as invoke:
            result = run_implementation_flow(self.context, self.remote)
        self.assertEqual(result['status'], 'rtl_parent_readback_incomplete')
        self.assertEqual(result['review_verdict'], 'revise')
        self.assertFalse(result['rtl_stage_release'])
        self.assertEqual(self.remote.current['decision'], original)
        self.assertEqual(result['parent_readback']['parent_session_id'], 'original-parent')
        self.assertFalse(any(c['action'] in ('rtl_repair_begin','rtl_review_begin')
                             for c in self.remote.calls[1:]))
        invoke.assert_not_called()

    def test_full_frozen_architecture_sources_reach_each_fresh_role(self):
        originals = {'architecture.md': '原始草稿 Ω\r\n' + 'long-line-α' * 7000 + '\r\n',
                     'SOURCE.md': '未经摘要的来源\r\n\r\n',
                     'decision.json': '{"原生决定":"保留原文"}\r\n'}
        self.remote.architecture_texts.update(originals)
        roles = []
        def session(prompt, role, *args):
            roles.append(role)
            for name, text in originals.items():
                self.assertEqual(prompt.count(text), 1)
                native = '@run/' + self.remote.current['architecture_scope'] + '/' + name
                self.assertIn(native, prompt)
            for name in ('design.json', 'budgets.json', 'review.json'):
                self.assertIn('@run/' + self.remote.current['architecture_scope'] + '/' + name, prompt)
                self.assertNotIn(self.remote.architecture_texts[name], prompt)
            if role == 'review':
                self.remote.decide('revise' if self.remote.number == 1 else 'pass')
            return {'completed': True, 'tool_isolation_ok': True}
        with patch.object(flow, 'run_session', side_effect=session):
            result = run_implementation_flow(self.context, self.remote)
        self.assertEqual(roles, ['review', 'implementation', 'review'])
        self.assertTrue(result['rtl_stage_release'])
        requests = [call for call in self.remote.calls if call.get('include_architecture_context')]
        self.assertEqual([request['review'] for request in requests], ['review-1', 'review-1', 'review-2'])

    def test_incomplete_or_stale_architecture_context_cannot_start_either_role(self):
        faults = ('missing_context', 'wrong_candidate', 'wrong_scope', 'wrong_identity',
                  'missing_draft', 'truncated_draft', 'changed_provenance',
                  'missing_index_entry', 'changed_index_hash', 'false_missing_source')
        for branch in ('audit', 'repair'):
            for fault in faults:
                with self.subTest(branch=branch, fault=fault):
                    remote = Remote()
                    if branch == 'repair':
                        self.prepare_repair(remote)
                    original_call = remote.call
                    def call(req):
                        result = original_call(req)
                        if not req.get('include_architecture_context'):
                            return result
                        value = result['architecture_context']
                        if fault == 'missing_context': result.pop('architecture_context')
                        elif fault == 'wrong_candidate': value['candidate_sha256'] = 'another-candidate'
                        elif fault == 'wrong_scope': value['scope'] = 'another-scope'
                        elif fault == 'wrong_identity': value['identity_sha256'] = '0' * 64
                        elif fault == 'missing_draft': value['primary'].pop('architecture.md')
                        elif fault == 'truncated_draft': value['primary']['architecture.md']['content'] = value['primary']['architecture.md']['content'][:-1]
                        elif fault == 'changed_provenance': value['primary']['SOURCE.md']['content'] += 'changed'
                        elif fault == 'missing_index_entry': value['files'].pop('budgets.json')
                        elif fault == 'changed_index_hash': value['files']['design.json']['sha256'] = '0' * 64
                        elif fault == 'false_missing_source': value['primary']['SOURCE.md'] = {'status': 'missing', 'path': value['files']['SOURCE.md']['path']}
                        return result
                    with patch.object(remote, 'call', side_effect=call), patch.object(flow, 'run_session') as invoke:
                        with self.assertRaises(ValueError):
                            run_implementation_flow(self.context, remote)
                    invoke.assert_not_called()

    def test_architecture_transport_failure_is_not_missing_optional_context(self):
        for branch in ('audit', 'repair'):
            with self.subTest(branch=branch):
                remote = Remote()
                if branch == 'repair':
                    self.prepare_repair(remote)
                original_call = remote.call
                def call(req):
                    if req.get('include_architecture_context'):
                        raise ConnectionError('fixture transport failure')
                    return original_call(req)
                with patch.object(remote, 'call', side_effect=call), patch.object(flow, 'run_session') as invoke:
                    with self.assertRaises(ConnectionError):
                        run_implementation_flow(self.context, remote)
                invoke.assert_not_called()

    def test_undeclared_optional_sources_are_explicit_not_approval(self):
        self.remote.architecture_texts.pop('SOURCE.md')
        self.remote.architecture_texts.pop('decision.json')
        def session(prompt, *args):
            for name in ('SOURCE.md', 'decision.json'):
                self.assertIn('MISSING IN FROZEN PACKET: @run/' + self.remote.current['architecture_scope'] + '/' + name, prompt)
            self.remote.decide('insufficient_evidence')
            return {'completed': True, 'tool_isolation_ok': True}
        with patch.object(flow, 'run_session', side_effect=session):
            result = run_implementation_flow(self.context, self.remote)
        self.assertFalse(result['rtl_stage_release'])

    def test_original_delivery_task_reaches_fresh_audit_and_repair_prompts(self):
        prompts = []
        def session(prompt, role, models, root, context_path, *args):
            child = json.loads(context_path.read_text())
            prompts.append((role, prompt))
            # Compare the original text, including Unicode, CRLF, blank lines
            # and its trailing newline, rather than matching guidance wording.
            self.assertEqual(prompt.count(self.task), 1)
            self.assertEqual(child['task'].encode(), self.task.encode())
            self.assertEqual(sha256(child['task'].encode()).hexdigest(),
                             child['settings']['task_sha256'])
            if role == 'review':
                self.remote.decide('revise' if self.remote.number == 1 else 'pass')
            return {'completed': True, 'tool_isolation_ok': True}
        with patch.object(flow, 'run_session', side_effect=session):
            result = run_implementation_flow(self.context, self.remote)
        self.assertEqual([role for role, _ in prompts],
                         ['review', 'implementation', 'review'])
        self.assertTrue(result['rtl_stage_release'])

    def test_missing_or_mismatched_delivery_task_cannot_start_a_model(self):
        for branch in ('audit', 'repair'):
            for fault in ('missing_task', 'changed_task', 'missing_hash', 'changed_hash'):
                with self.subTest(branch=branch, fault=fault):
                    remote = Remote()
                    context = dict(self.context, settings=dict(self.context['settings']))
                    if branch == 'repair':
                        remote.call({'action': 'rtl_review_begin'})
                        remote.decide('revise')
                        remote.evidence('rtl-audit/review-1.audit.json', json.dumps({
                            'completed': True, 'tool_isolation_ok': True,
                            'candidate_sha256': remote.current['candidate_sha256'],
                            'decision_report_sha256': remote.current['decision']['report_sha256']}))
                    if fault == 'missing_task':
                        context.pop('task')
                    elif fault == 'changed_task':
                        context['task'] = self.task + 'Changed after freezing.\n'
                    elif fault == 'missing_hash':
                        context['settings'].pop('task_sha256')
                    else:
                        context['settings']['task_sha256'] = '0' * 64
                    with patch.object(flow, 'run_session') as invoke:
                        with self.assertRaises(ValueError):
                            run_implementation_flow(context, remote)
                    invoke.assert_not_called()

    def test_repair_is_implementation_and_review_gets_fresh_context(self):
        roles, contexts = [], []
        def session(prompt, role, models, root, context_path, *args):
            context = json.loads(context_path.read_text())
            contexts.append(context)
            roles.append(role)
            if context['role'] == 'rtl_audit':
                if self.remote.number == 2:
                    self.assertTrue(any(q['action'] == 'status' and q['job_id'] == 'repair-job' for q in self.remote.calls))
                self.remote.decide('revise' if self.remote.number == 1 else 'pass')
            else:
                self.assertEqual(context['scope'], 'work')
                self.assertIn('@run/audit/review-1/review.json', prompt)
                self.remote.pending_batches = [['repair-job'], []]
            return {'completed': True, 'tool_isolation_ok': True, 'exit_code': 0}
        with patch('harness.adapters.rtl_remote.time.sleep'), patch.object(flow, 'run_session', side_effect=session):
            result = run_implementation_flow(self.context, self.remote)
        self.assertEqual(roles, ['review', 'implementation', 'review'])
        self.assertEqual([c['role'] for c in contexts], ['rtl_audit', 'implementation', 'rtl_audit'])
        self.assertNotEqual(contexts[0]['scope'], contexts[-1]['scope'])
        self.assertTrue(result['rtl_stage_release'])
        self.assertEqual(result['candidate_sha256'], 'candidate-2')
        self.assertEqual([q['action'] for q in self.remote.calls].count('rtl_repair_end'), 1)

    def test_insufficient_evidence_finishes_without_repair_or_release(self):
        def session(*args):
            self.remote.decide('insufficient_evidence')
            return {'completed': True, 'tool_isolation_ok': True}
        with patch.object(flow, 'run_session', side_effect=session) as invoke:
            result = run_implementation_flow(self.context, self.remote)
        self.assertEqual(invoke.call_count, 1)
        self.assertFalse(result['rtl_stage_release'])
        self.assertEqual(result['review_verdict'], 'insufficient_evidence')
        self.assertFalse(any(q['action'] == 'rtl_repair_begin' for q in self.remote.calls))

    def test_normal_model_exit_without_decision_is_incomplete(self):
        with patch.object(flow, 'run_session', return_value={'completed': True, 'tool_isolation_ok': True}) as invoke:
            result = run_implementation_flow(self.context, self.remote)
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(result['status'], 'rtl_audit_incomplete')
        self.assertFalse(result['rtl_stage_release'])

    def test_failed_session_cannot_publish_release_on_resume(self):
        def session(*args):
            self.remote.decide('pass')
            return {'completed': False, 'tool_isolation_ok': True}
        with patch.object(flow, 'run_session', side_effect=session):
            self.assertFalse(run_implementation_flow(self.context, self.remote)['rtl_stage_release'])
        def recheck(*args):
            self.remote.decide('blocked')
            return {'completed': True, 'tool_isolation_ok': True}
        with patch.object(flow, 'run_session', side_effect=recheck) as invoke:
            result = run_implementation_flow(self.context, self.remote)
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(result['review'], 'review-2')
        self.assertEqual(result['review_verdict'], 'blocked')
        self.assertFalse(result['rtl_stage_release'])

    def test_completed_review_resume_reuses_bound_decision_without_new_model(self):
        def session(*args):
            self.remote.decide('pass')
            return {'completed': True, 'tool_isolation_ok': True}
        with patch.object(flow, 'run_session', side_effect=session):
            first = run_implementation_flow(self.context, self.remote)
        with patch.object(flow, 'run_session', side_effect=AssertionError('duplicate review')):
            second = run_implementation_flow(self.context, self.remote)
        self.assertEqual(first, second)

    def test_previous_completed_attempt_cannot_cover_crash_after_new_decision(self):
        with patch.object(flow, 'run_session', return_value={'completed': True, 'tool_isolation_ok': True}):
            self.assertEqual(run_implementation_flow(self.context, self.remote)['status'], 'rtl_audit_incomplete')
        def interrupted(*args):
            self.remote.decide('pass')
            raise RuntimeError('crash after publication before receipt')
        with patch.object(flow, 'run_session', side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, 'crash'):
                run_implementation_flow(self.context, self.remote)
        def recheck(*args):
            self.remote.decide('blocked')
            return {'completed': True, 'tool_isolation_ok': True}
        with patch.object(flow, 'run_session', side_effect=recheck):
            result = run_implementation_flow(self.context, self.remote)
        self.assertFalse(result['rtl_stage_release'])
        self.assertEqual(result['review'], 'review-2')

    def test_blocked_resume_starts_new_review_without_overwriting_old_scope(self):
        def blocked(*args):
            self.remote.decide('blocked')
            return {'completed': True, 'tool_isolation_ok': True}
        with patch.object(flow, 'run_session', side_effect=blocked):
            first = run_implementation_flow(self.context, self.remote)
        def recovered(*args):
            self.remote.decide('pass')
            return {'completed': True, 'tool_isolation_ok': True}
        with patch.object(flow, 'run_session', side_effect=recovered) as invoke:
            second = run_implementation_flow(self.context, self.remote)
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(first['review'], 'review-1')
        self.assertEqual(second['review'], 'review-2')
        self.assertTrue(second['rtl_stage_release'])

    def test_optional_readiness_callback_runs_after_completed_repair_before_new_freeze(self):
        order = []
        def session(prompt, role, *args):
            order.append(role)
            if role == 'review':
                self.remote.decide('revise' if self.remote.number == 1 else 'blocked')
            return {'completed': True, 'tool_isolation_ok': True}
        def before_freeze():
            self.assertEqual(self.remote.current['repair']['status'], 'completed')
            self.assertEqual(self.remote.number, 1)
            order.append('readiness')
        with patch.object(flow, 'run_session', side_effect=session):
            result = run_implementation_flow(self.context, self.remote, before_freeze=before_freeze)
        self.assertEqual(order, ['review', 'implementation', 'readiness', 'review'])
        self.assertEqual(result['review'], 'review-2')
        self.assertFalse(result['rtl_stage_release'])

    def test_missing_optional_audit_is_not_a_release(self):
        (self.harness / 'skills/rtl-audit/SKILL.md').unlink()
        result = run_implementation_flow(self.context, self.remote)
        self.assertEqual(result['review_verdict'], 'not_run')
        self.assertFalse(result['rtl_stage_release'])
        self.assertEqual(self.remote.calls, [])

    def test_generation_jobs_finish_before_first_candidate_or_audit(self):
        self.remote.pending_batches = [['generation-job'], []]
        def session(*args):
            self.assertTrue(any(q['action'] == 'status' and q['job_id'] == 'generation-job' for q in self.remote.calls))
            self.remote.decide('blocked')
            return {'completed': True, 'tool_isolation_ok': True}
        with patch('harness.adapters.rtl_remote.time.sleep'), patch.object(flow, 'run_session', side_effect=session):
            result = run_implementation_flow(self.context, self.remote)
        actions = [q['action'] for q in self.remote.calls]
        self.assertLess(actions.index('status'), actions.index('rtl_review_begin'))
        self.assertFalse(result['rtl_stage_release'])
        self.assertNotIn('submit', actions)
        self.assertNotIn('cancel', actions)

    def test_audit_pending_results_get_judgment_in_same_candidate_context(self):
        scopes = []
        def session(prompt, role, models, root, context_path, *args):
            scopes.append(json.loads(context_path.read_text())['scope'])
            if len(scopes) == 1:
                self.remote.pending_batches = [['audit-job'], []]
                return {'completed': True, 'tool_isolation_ok': True}
            self.assertTrue(any(q['action'] == 'status' and q['job_id'] == 'audit-job' for q in self.remote.calls))
            self.remote.decide('insufficient_evidence')
            return {'completed': True, 'tool_isolation_ok': True}
        with patch('harness.adapters.rtl_remote.time.sleep'), patch.object(flow, 'run_session', side_effect=session):
            result = run_implementation_flow(self.context, self.remote)
        self.assertEqual(scopes, ['audit/review-1', 'audit/review-1'])
        self.assertEqual(result['review_verdict'], 'insufficient_evidence')
        self.assertFalse(result['rtl_stage_release'])

    def test_interrupted_repair_drain_reuses_receipt_and_failed_job_is_not_pass(self):
        roles = []
        def initial(prompt, role, *args):
            roles.append(role)
            if role == 'review':
                self.remote.decide('revise')
            else:
                self.remote.pending_batches = [['repair-job'], []]
                self.remote.job_results['repair-job'] = {'job_id': 'repair-job', 'status': 'completed', 'exit_code': -15, 'timed_out': True}
            return {'completed': True, 'tool_isolation_ok': True}
        with patch('harness.adapters.rtl_remote.time.sleep', side_effect=KeyboardInterrupt), patch.object(flow, 'run_session', side_effect=initial):
            with self.assertRaises(KeyboardInterrupt):
                run_implementation_flow(self.context, self.remote)
        self.assertEqual(roles, ['review', 'implementation'])
        self.assertEqual(self.remote.current['repair']['status'], 'active')
        self.assertFalse(any(q['action'] == 'rtl_repair_end' for q in self.remote.calls))
        # The real worker may still be running when the client resumes.
        self.remote.pending_batches = [['repair-job'], []]
        def resumed(prompt, role, *args):
            roles.append(role)
            self.assertEqual(role, 'review')
            self.assertEqual(self.remote.number, 2)
            self.remote.decide('blocked')
            return {'completed': True, 'tool_isolation_ok': True}
        with patch('harness.adapters.rtl_remote.time.sleep'), patch.object(flow, 'run_session', side_effect=resumed):
            result = run_implementation_flow(self.context, self.remote)
        self.assertEqual(roles, ['review', 'implementation', 'review'])
        self.assertEqual(result['review_verdict'], 'blocked')
        self.assertFalse(result['rtl_stage_release'])


if __name__ == '__main__':
    unittest.main()
