"""Native whole-region publication and protected baseline checks, without EDA."""
import json
from pathlib import Path
import unittest
from unittest import mock

from harness.runtime import rtl_readiness as readiness
from harness.tests import test_rtl_readiness as original


class RegionalIntegrationTests(unittest.TestCase):
    setUp = original.ReadinessTests.setUp
    tearDown = original.ReadinessTests.tearDown
    call = original.ReadinessTests.call
    begin = original.ReadinessTests.begin
    session = original.ReadinessTests.session
    complete = original.ReadinessTests.complete

    def candidate(self):
        (self.rtl / 'obsolete.sv').write_text('module obsolete; endmodule\n')
        filelist = self.out / 'work/filelists/design.f'
        filelist.parent.mkdir()
        filelist.write_text('rtl/obsolete.sv\nrtl/block.sv\n')
        manifest = json.loads((self.out / 'manifest.json').read_text())
        manifest['settings']['readiness'].update(region_roots=['rtl/region'], build_files=['filelists/design.f'])
        (self.out / 'manifest.json').write_text(json.dumps(manifest))
        campaign = self.begin()
        work = self.out / campaign['work_scope']
        workspace = work / 'probe'
        tree = workspace / 'delivery/candidate'
        source = readiness.load(self.out)['source_files']
        baseline = {name: {k: value[k] for k in ('sha256', 'size')} for name, value in source.items()}
        for name in baseline:
            if name == 'rtl/obsolete.sv':
                continue
            target = tree / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((self.out / 'work' / name).read_bytes())
        (tree / 'rtl/block.sv').write_text('module block; lane u(); endmodule\n')
        (tree / 'rtl/region').mkdir()
        (tree / 'rtl/region/lane.sv').write_text('module lane; endmodule\n')
        (tree / 'filelists/design.f').write_text('rtl/region/lane.sv\nrtl/block.sv\n')
        files = {str(p.relative_to(tree)): {'sha256': readiness.sha(p), 'size': p.stat().st_size}
                 for p in tree.rglob('*') if p.is_file()}
        operations = dict(add=['rtl/region/lane.sv'], modify=['filelists/design.f', 'rtl/block.sv'], delete=['rtl/obsolete.sv'])
        verified = dict(decision='candidate_for_integration', comparison_identity='comparison-pin',
                        baseline_identity='baseline-pin', variant_identity='variant-pin', workspace_id='workspace-pin',
                        workspace=str(workspace), delivery=str(tree.parent), candidate_tree=str(tree),
                        files=files, baseline_files=baseline, allowed_paths=['rtl/block.sv', 'rtl/obsolete.sv'],
                        changes=sorted(name for paths in operations.values() for name in paths), operations=operations,
                        region_roots=['rtl/region'], build_files=['filelists/design.f'],
                        baseline_build=dict(top='block', sources=['rtl/obsolete.sv', 'rtl/block.sv'], include_dirs=['rtl']),
                        build=dict(top='block', sources=['rtl/region/lane.sv', 'rtl/block.sv'], include_dirs=['rtl']))
        (work / 'result.json').write_text(json.dumps(dict(decision='candidate_for_integration', reason='Regional evidence fixture',
            workspace='probe', comparison='probe/comparison.json', delivery='probe/delivery')))
        review = {key: verified[key] for key in ('comparison_identity', 'baseline_identity', 'variant_identity')}
        review.update(verdict='accept', reason='Independent region fixture review')
        (self.out / campaign['audit_scope'] / 'review.json').write_text(json.dumps(review))
        sessions = dict(implementation_session_id=self.session(campaign), review_session_id=self.session(campaign, review=True))
        return campaign, verified, sessions

    def test_add_modify_delete_and_project_filelist_publish_as_one_candidate(self):
        campaign, verified, sessions = self.candidate()
        test_bytes = (self.out / 'work/tests/equivalence.py').read_bytes()
        with mock.patch.object(readiness, 'verify_core', return_value=verified):
            result = self.complete(campaign, **sessions)
        self.assertEqual(result['state'], 'adopted')
        self.assertFalse((self.rtl / 'obsolete.sv').exists())
        self.assertTrue((self.rtl / 'region/lane.sv').is_file())
        self.assertEqual((self.out / 'work/filelists/design.f').read_text(), 'rtl/region/lane.sv\nrtl/block.sv\n')
        self.assertEqual((self.out / 'work/tests/equivalence.py').read_bytes(), test_bytes)
        receipt = json.loads((self.out / result['adoption_path']).read_text())
        self.assertEqual(receipt['operations'], verified['operations'])
        self.assertEqual(receipt['deleted_files'], ['rtl/obsolete.sv'])
        self.assertEqual(set(receipt['changed_files']), {'rtl/block.sv', 'rtl/region/lane.sv', 'filelists/design.f'})
        self.assertEqual(receipt['build'], verified['build'])
        self.assertTrue(result['adopted_source_build_sha256'])
        self.assertTrue(self.begin()['reused'])
        # Handoff verification remains valid against adopted work, including removed originals.
        readiness.integration_updates(self.out, readiness.load(self.out), verified, check_live_baseline=False)

    def test_filelist_baseline_drift_is_detected_before_any_publication(self):
        campaign, verified, sessions = self.candidate()
        old = (self.rtl / 'block.sv').read_bytes()
        (self.out / 'work/filelists/design.f').write_text('rtl/block.sv\n')
        with mock.patch.object(readiness, 'verify_core', return_value=verified) as verify:
            result = self.complete(campaign, **sessions)
        self.assertEqual(result['state'], 'evidence_incomplete')
        verify.assert_not_called()
        self.assertEqual((self.rtl / 'block.sv').read_bytes(), old)
        self.assertTrue((self.rtl / 'obsolete.sv').exists())
        self.assertFalse((self.rtl / 'region/lane.sv').exists())

    def test_outside_region_or_frozen_asset_addition_cannot_publish(self):
        for path in ('rtl/unowned.sv', 'tests/new.sv'):
            with self.subTest(path=path):
                self.tearDown(); self.setUp()
                campaign, verified, sessions = self.candidate()
                source = Path(verified['candidate_tree']) / 'rtl/region/lane.sv'
                target = Path(verified['candidate_tree']) / path
                target.parent.mkdir(parents=True, exist_ok=True)
                source.replace(target)
                verified['files'][path] = verified['files'].pop('rtl/region/lane.sv')
                verified['operations']['add'] = [path]
                verified['changes'] = sorted(name for names in verified['operations'].values() for name in names)
                with mock.patch.object(readiness, 'verify_core', return_value=verified):
                    result = self.complete(campaign, **sessions)
                self.assertEqual(result['state'], 'evidence_incomplete')
                self.assertIn('wire x = 0', (self.rtl / 'block.sv').read_text())
                self.assertTrue((self.rtl / 'obsolete.sv').exists())

    def test_failed_independent_review_preserves_entire_region(self):
        campaign, verified, sessions = self.candidate()
        review = self.out / campaign['audit_scope'] / 'review.json'
        value = json.loads(review.read_text()); value['verdict'] = 'revise'; review.write_text(json.dumps(value))
        before = readiness.source_inventory(self.out / 'work', ['filelists/design.f'])
        with mock.patch.object(readiness, 'verify_core', return_value=verified):
            result = self.complete(campaign, **sessions)
        self.assertEqual(result['state'], 'evidence_incomplete')
        self.assertEqual(before, readiness.source_inventory(self.out / 'work', ['filelists/design.f']))

    def test_publish_error_rolls_back_additions_deletions_and_build_file(self):
        campaign, verified, sessions = self.candidate()
        before = readiness.source_inventory(self.out / 'work', ['filelists/design.f'])
        replace = Path.replace
        failed = False
        def one_failure(source, target):
            nonlocal failed
            if Path(target) == self.rtl / 'block.sv' and source.name.startswith('update-') and not failed:
                failed = True
                raise OSError('injected publication failure')
            return replace(source, target)
        with mock.patch.object(readiness, 'verify_core', return_value=verified), mock.patch.object(Path, 'replace', one_failure):
            result = self.complete(campaign, **sessions)
        self.assertTrue(failed)
        self.assertEqual(result['state'], 'evidence_incomplete')
        self.assertEqual(before, readiness.source_inventory(self.out / 'work', ['filelists/design.f']))
        journal = json.loads((self.out / 'readiness/campaign-1.publication.json').read_text())
        self.assertEqual(journal['state'], 'rolled_back')
        self.assertFalse((self.rtl / 'region').exists())

    def test_changed_build_without_declared_project_filelist_is_not_adopted(self):
        campaign, verified, _ = self.candidate()
        campaign = readiness.load(self.out)
        campaign['build_files'] = []; verified['build_files'] = []
        for value in (verified['files'], verified['baseline_files']):
            value.pop('filelists/design.f')
        verified['operations']['modify'].remove('filelists/design.f')
        verified['changes'].remove('filelists/design.f')
        with self.assertRaisesRegex(ValueError, 'declared project filelist'):
            readiness.integration_updates(self.out, campaign, verified)

    def test_include_search_change_requires_real_project_build_configuration(self):
        campaign, verified, sessions = self.candidate()
        before = readiness.source_inventory(self.out / 'work', ['filelists/design.f'])
        verified['build']['include_dirs'] = ['rtl', 'rtl/region']
        with mock.patch.object(readiness, 'verify_core', return_value=verified):
            result = self.complete(campaign, **sessions)
        self.assertEqual(result['state'], 'evidence_incomplete')
        self.assertEqual(before, readiness.source_inventory(self.out / 'work', ['filelists/design.f']))
        receipt = json.loads((self.out / result['adoption_path']).read_text())
        self.assertIn('actual project build configuration adapter', receipt['reason'])
        self.assertFalse((self.out / 'readiness/campaign-1.publication.json').exists())

    def test_build_file_cannot_be_test_or_frozen_input_and_policy_is_explicit(self):
        for name in ('tests/equivalence.py', 'snapshot/spec.md'):
            with self.subTest(path=name):
                manifest = json.loads((self.out / 'manifest.json').read_text())
                manifest['settings']['readiness'].update(region_roots=['rtl'], build_files=[name])
                (self.out / 'manifest.json').write_text(json.dumps(manifest))
                with self.assertRaisesRegex(ValueError, 'filelist outside frozen'):
                    self.begin()


if __name__ == '__main__':
    unittest.main()
