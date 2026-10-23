"""Budget rejection reports the admitted jobs and actual remaining time."""
import json
import unittest

from harness.runtime import rtl_readiness as readiness
from harness.tests import test_rtl_readiness as fixtures


class SubmitBudgetMessageTests(unittest.TestCase):
    setUp = fixtures.ReadinessTests.setUp
    tearDown = fixtures.ReadinessTests.tearDown
    call = fixtures.ReadinessTests.call
    begin = fixtures.ReadinessTests.begin

    def limits(self, *, max_jobs=10, tool_seconds=150):
        path = self.out / 'manifest.json'
        manifest = json.loads(path.read_text())
        manifest['settings']['readiness'].update(max_jobs=max_jobs, tool_seconds=tool_seconds, max_job_seconds=60)
        path.write_text(json.dumps(manifest))

    def existing(self, campaign, elapsed):
        job = self.out / 'jobs' / ('existing-' + str(len(list((self.out / 'jobs').glob('*')))))
        job.mkdir(parents=True)
        (job / 'request.json').write_text(json.dumps({'scope': campaign['work_scope'], 'timeout_seconds': 60}))
        (job / 'result.json').write_text(json.dumps({'elapsed_seconds': elapsed,
                                                   'isolation': {'cleanup_complete': True}}))

    def test_time_rejection_distinguishes_two_remaining_jobs_and_preserves_exact_time(self):
        self.limits(); campaign = self.begin()
        for elapsed in (1.240070072002709, 9.413437500945292, 1.2282386189326644, 13.964360206038691,
                        1.1810796089703217, 1.3900621170178056, 43.31480858300347, 25.180403548059985):
            self.existing(campaign, elapsed)
        before = readiness.budget_usage(self.out, readiness.load(self.out))
        self.assertEqual(before['max_jobs'], 2)
        self.assertAlmostEqual(before['tool_seconds'], 53.08753974502906)
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, r'exhausted \(tool_seconds\)') as error:
                self.call(action='submit', scope=campaign['work_scope'], argv=['python', '-c', 'pass'], timeout_seconds=55)
            self.assertIn('requested_timeout_seconds=55', str(error.exception))
            remaining = json.loads(str(error.exception).split('; remaining_budgets=', 1)[1])
            self.assertEqual(remaining, before)
        self.assertEqual(readiness.budget_usage(self.out, readiness.load(self.out)), before)
        self.assertEqual(len(list((self.out / 'jobs').glob('*/request.json'))), 8)

    def test_job_count_rejection_reports_remaining_time_without_creating_a_job(self):
        self.limits(max_jobs=1); campaign = self.begin(); self.existing(campaign, 1)
        with self.assertRaisesRegex(ValueError, r'exhausted \(job_count\)') as error:
            self.call(action='submit', scope=campaign['work_scope'], argv=['python', '-c', 'pass'], timeout_seconds=1)
        remaining = json.loads(str(error.exception).split('; remaining_budgets=', 1)[1])
        self.assertEqual(remaining['max_jobs'], 0)
        self.assertEqual(remaining['tool_seconds'], 149)
        self.assertEqual(len(list((self.out / 'jobs').glob('*/request.json'))), 1)


if __name__ == '__main__':
    unittest.main()
