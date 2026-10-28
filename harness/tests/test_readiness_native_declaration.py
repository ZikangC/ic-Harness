"""Regional declaration handoff checks, with injected standalone verification.

These exercise native adapter validation/publication only. The standalone skill
currently treats every build_file as a plain filelist and cannot produce a real
regional receipt containing RTL_STRUCTURE.json; this fixture does not claim
that end-to-end capability or any actual structural/functional acceptance.
"""
import copy
import json
from pathlib import Path
import unittest
from unittest import mock

from harness.runtime import rtl_readiness as readiness
from harness.tests import test_readiness_regional_integration as regional


class NativeDeclarationTests(unittest.TestCase):
    tearDown = regional.RegionalIntegrationTests.tearDown
    call = regional.RegionalIntegrationTests.call
    session = regional.RegionalIntegrationTests.session
    complete = regional.RegionalIntegrationTests.complete

    def setUp(self):
        regional.RegionalIntegrationTests.setUp(self)
        self.declaration = {
            'version': 1, 'blackboxes': [], 'configurations': [{
                'id': 'delivery', 'top': 'block',
                'sources': ['rtl/obsolete.sv', 'rtl/block.sv'], 'include_dirs': ['rtl'],
                'defines': {'ENABLE': '1', 'SYNTHESIS': '1'}, 'parameters': {'LANES': "8'd2"}}]}
        (self.out / 'work/RTL_STRUCTURE.json').write_text(json.dumps(self.declaration) + '\n')

    def begin(self):
        manifest = json.loads((self.out / 'manifest.json').read_text())
        values = manifest['settings']['readiness'].setdefault('build_files', [])
        if 'RTL_STRUCTURE.json' not in values:
            values.append('RTL_STRUCTURE.json')
        (self.out / 'manifest.json').write_text(json.dumps(manifest))
        return regional.RegionalIntegrationTests.begin(self)

    def candidate(self, *, update_declaration=True):
        campaign, verified, sessions = regional.RegionalIntegrationTests.candidate(self)
        verified['build_files'].append('RTL_STRUCTURE.json')
        if update_declaration:
            declaration = copy.deepcopy(self.declaration)
            declaration['configurations'][0]['sources'] = verified['build']['sources']
            self.write_candidate_declaration(verified, declaration)
        return campaign, verified, sessions

    def write_candidate_declaration(self, verified, declaration):
        path = Path(verified['candidate_tree']) / 'RTL_STRUCTURE.json'
        path.write_text(json.dumps(declaration) + '\n')
        verified['files']['RTL_STRUCTURE.json'] = {'sha256': readiness.sha(path), 'size': path.stat().st_size}
        modified = verified['operations']['modify']
        if 'RTL_STRUCTURE.json' not in modified:
            modified.append('RTL_STRUCTURE.json'); modified.sort()
        verified['changes'] = sorted(name for paths in verified['operations'].values() for name in paths)

    def assert_rejected_without_live_writes(self, campaign, verified, sessions, reason):
        before = readiness.source_inventory(self.out / 'work', ['filelists/design.f', 'RTL_STRUCTURE.json'])
        with mock.patch.object(readiness, 'verify_core', return_value=verified):
            result = self.complete(campaign, **sessions)
        self.assertEqual(result['state'], 'evidence_incomplete')
        self.assertFalse(result['adopted'])
        self.assertEqual(before, readiness.source_inventory(
            self.out / 'work', ['filelists/design.f', 'RTL_STRUCTURE.json']))
        self.assertFalse((self.out / 'readiness/campaign-1.publication.json').exists())
        receipt = json.loads((self.out / result['adoption_path']).read_text())
        self.assertIn(reason, receipt['reason'])

    def test_new_module_caller_filelist_and_native_declaration_publish_together(self):
        campaign, verified, sessions = self.candidate()
        declaration_path = Path(verified['candidate_tree']) / 'RTL_STRUCTURE.json'
        expected_bytes = declaration_path.read_bytes()
        with mock.patch.object(readiness, 'verify_core', return_value=verified):
            result = self.complete(campaign, **sessions)
        self.assertEqual(result['state'], 'adopted')
        self.assertTrue((self.rtl / 'region/lane.sv').is_file())
        self.assertIn('lane u()', (self.rtl / 'block.sv').read_text())
        self.assertFalse((self.rtl / 'obsolete.sv').exists())
        self.assertEqual((self.out / 'work/filelists/design.f').read_text(),
                         'rtl/region/lane.sv\nrtl/block.sv\n')
        self.assertEqual((self.out / 'work/RTL_STRUCTURE.json').read_bytes(), expected_bytes)
        adopted = json.loads(expected_bytes)['configurations'][0]
        self.assertEqual(adopted['defines'], self.declaration['configurations'][0]['defines'])
        self.assertEqual(adopted['parameters'], self.declaration['configurations'][0]['parameters'])
        receipt = json.loads((self.out / result['adoption_path']).read_text())
        self.assertIn('RTL_STRUCTURE.json', receipt['changed_files'])
        self.assertIn('RTL_STRUCTURE.json', receipt['build_files'])
        readiness.integration_updates(self.out, readiness.load(self.out), verified, check_live_baseline=False)

    def test_regional_policy_must_explicitly_include_existing_native_declaration(self):
        manifest = json.loads((self.out / 'manifest.json').read_text())
        manifest['settings']['readiness'].update(region_roots=['rtl'], build_files=[])
        (self.out / 'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'RTL_STRUCTURE.json requires it in build_files'):
            regional.RegionalIntegrationTests.begin(self)
        self.assertFalse((self.out / 'readiness').exists())

    def test_candidate_missing_or_changed_declaration_is_rejected_before_publication(self):
        for fault in ('missing', 'changed'):
            with self.subTest(fault=fault):
                if fault == 'changed':
                    self.tearDown(); self.setUp()
                campaign, verified, sessions = self.candidate()
                path = Path(verified['candidate_tree']) / 'RTL_STRUCTURE.json'
                if fault == 'missing':
                    path.unlink()
                else:
                    path.write_text('{}\n')
                self.assert_rejected_without_live_writes(campaign, verified, sessions, 'RTL_STRUCTURE.json')

    def test_unchanged_old_declaration_with_deleted_source_is_rejected(self):
        campaign, verified, sessions = self.candidate(update_declaration=False)
        self.assertNotIn('RTL_STRUCTURE.json', verified['changes'])
        self.assert_rejected_without_live_writes(campaign, verified, sessions, 'compile configuration is invalid')

    def test_valid_but_mismatched_declaration_cannot_omit_new_module(self):
        campaign, verified, sessions = self.candidate()
        declaration = copy.deepcopy(self.declaration)
        declaration['configurations'][0]['sources'] = ['rtl/block.sv']
        self.write_candidate_declaration(verified, declaration)
        self.assert_rejected_without_live_writes(campaign, verified, sessions, 'no configuration matching')

    def test_top_and_ordered_source_and_include_configuration_must_match(self):
        for field, value in (('top', 'lane'), ('sources', ['rtl/block.sv', 'rtl/region/lane.sv']),
                             ('include_dirs', [])):
            with self.subTest(field=field):
                if field != 'top':
                    self.tearDown(); self.setUp()
                campaign, verified, sessions = self.candidate()
                declaration = json.loads((Path(verified['candidate_tree']) / 'RTL_STRUCTURE.json').read_text())
                declaration['configurations'][0][field] = value
                self.write_candidate_declaration(verified, declaration)
                self.assert_rejected_without_live_writes(campaign, verified, sessions, 'no configuration matching')

    def test_native_normalization_checks_defines_without_silently_dropping_them(self):
        campaign, verified, sessions = self.candidate()
        declaration = json.loads((Path(verified['candidate_tree']) / 'RTL_STRUCTURE.json').read_text())
        declaration['configurations'][0]['defines']['SYNTHESIS'] = '0'
        self.write_candidate_declaration(verified, declaration)
        self.assert_rejected_without_live_writes(campaign, verified, sessions, 'SYNTHESIS must be 1')


if __name__ == '__main__':
    unittest.main()
