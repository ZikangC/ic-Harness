"""Regional add/delete/build pins survive seed resume and real native review freeze."""
import hashlib
import json
import unittest

from harness.runtime.rtl_readiness_seed import seed
from harness.tests.test_readiness_seed_import import FormalRepairSeedImport


class RegionalSeedImport(FormalRepairSeedImport):
    def region(self):
        self.formal_seed()
        source = self.out / 'work/rtl/top.sv'
        source.unlink()
        new = self.out / 'work/rtl/new.sv'
        new.write_text('module top; wire regional; endmodule\n')
        source_list = self.out / 'work/sources.f'
        source_list.write_text('rtl/new.sv\n')
        manifest = json.loads((self.out / 'manifest.json').read_text())
        manifest['settings']['readiness'].update(region_roots=['rtl'], build_files=['sources.f'])
        (self.out / 'manifest.json').write_text(json.dumps(manifest))
        receipt = {'adopted': True,
                   'changed_files': {n: hashlib.sha256((self.out/'work'/n).read_bytes()).hexdigest()
                                     for n in ('rtl/new.sv', 'sources.f')},
                   'operations': {'add': ['rtl/new.sv', 'sources.f'], 'modify': [], 'delete': ['rtl/top.sv']}}
        p = self.out / 'readiness/campaign-1.adoption.json'
        p.parent.mkdir(exist_ok=True)
        p.write_text(json.dumps(receipt))
        return new, source_list

    def test_region_deletion_addition_and_build_pins_validate_before_and_after_review(self):
        new, source_list = self.region()
        seed(self.out, {'validate_only': True})
        self.transition('rtl_review_begin')
        seed(self.out, {'validate_only': True})
        self.assertFalse((self.out/'work/rtl/top.sv').exists())

    def test_deleted_seed_resurrection_and_added_source_tamper_are_rejected(self):
        new, source_list = self.region()
        old = self.out / 'work/rtl/top.sv'
        old.write_text('module top; wire original; endmodule\n')
        with self.assertRaisesRegex(ValueError, 'rtl/top.sv'):
            seed(self.out, {'validate_only': True})
        old.unlink()
        new.write_text('tamper')
        with self.assertRaisesRegex(ValueError, 'rtl/new.sv'):
            seed(self.out, {'validate_only': True})

    def test_adopted_project_filelist_remains_pinned(self):
        new, source_list = self.region()
        source_list.write_text('rtl/missing.sv\n')
        with self.assertRaisesRegex(ValueError, 'sources.f'):
            seed(self.out, {'validate_only': True})

    def test_campaign_ten_pins_follow_numeric_adoption_order(self):
        new, source_list = self.region()
        for number in (2, 9, 10):
            new.write_text('module top; wire version_' + str(number) + '; endmodule\n')
            value = {'adopted': True,
                     'changed_files': {'rtl/new.sv': hashlib.sha256(new.read_bytes()).hexdigest()},
                     'operations': {'add': [], 'modify': ['rtl/new.sv'], 'delete': []}}
            p = self.out / ('readiness/campaign-' + str(number) + '.adoption.json')
            p.write_text(json.dumps(value))
        seed(self.out, {'validate_only': True})

    def test_campaign_ten_can_readd_a_previously_deleted_adopted_module(self):
        new, source_list = self.region()
        deleted = {'adopted': True, 'changed_files': {},
                   'operations': {'add': [], 'modify': [], 'delete': ['rtl/new.sv']}}
        (self.out/'readiness/campaign-2.adoption.json').write_text(json.dumps(deleted))
        new.write_text('module top; wire reintroduced; endmodule\n')
        added = {'adopted': True,
                 'changed_files': {'rtl/new.sv': hashlib.sha256(new.read_bytes()).hexdigest()},
                 'operations': {'add': ['rtl/new.sv'], 'modify': [], 'delete': []}}
        (self.out/'readiness/campaign-10.adoption.json').write_text(json.dumps(added))
        seed(self.out, {'validate_only': True})


if __name__ == '__main__':
    unittest.main()
