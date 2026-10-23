"""Generic protection invariants; no RTL, EDA or model execution."""
from collections import Counter
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from harness.runtime import rtl_worker as worker


class GuardReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)
        self.paths = {
            "candidate_scope": "rtl/candidates/candidate-1/work",
            "architecture_scope": "rtl/candidates/candidate-1/architecture",
            "audit_scope": "audit/review-1",
        }
        for relative in self.paths.values():
            p = self.out / relative
            p.mkdir(parents=True)
            (p / "payload.txt").write_text("original\n")
        inventories = {key: worker.closure_inventory(self.out / rel)
                       for key, rel in self.paths.items()}
        self.review = dict(self.paths, review="review-1",
                           work_inventory=inventories["candidate_scope"],
                           architecture_identity={"inventory": inventories["architecture_scope"]},
                           decision={"verdict": "revise"},
                           decision_audit_inventory=inventories["audit_scope"])
        self.review["candidate_sha256"] = worker.digest({
            "work": self.review["work_inventory"],
            "architecture": self.review["architecture_identity"],
        })
        (self.out / "rtl/reviews").mkdir(parents=True)
        self.metadata = self.out / "rtl/reviews/review-1.json"
        self.metadata.write_text(json.dumps(self.review))
        self.expected = [
            {"path": self.paths["candidate_scope"], "inventory": inventories["candidate_scope"]},
            {"path": self.paths["architecture_scope"], "inventory": inventories["architecture_scope"]},
            {"path": self.paths["audit_scope"], "inventory": inventories["audit_scope"]},
        ]

    def change_bytes_without_size_or_mtime(self, key):
        p = self.out / self.paths[key] / "payload.txt"
        old = p.read_bytes()
        times = p.stat()
        p.write_bytes(b"changed!\n")
        self.assertEqual(p.stat().st_size, len(old))
        os.utime(p, ns=(times.st_atime_ns, times.st_mtime_ns))
        return p, old, times

    def test_one_fresh_fingerprint_per_protected_tree(self):
        with patch.object(worker, "closure_inventory", wraps=worker.closure_inventory) as inventory:
            guards = worker.integrity_guards(self.out)
        self.assertEqual(guards, self.expected)
        self.assertEqual(Counter(call.args[0].resolve() for call in inventory.call_args_list),
                         Counter((self.out / rel).resolve() for rel in self.paths.values()))

    def test_next_call_rejects_same_size_mtime_content_changes(self):
        self.assertEqual(worker.integrity_guards(self.out), self.expected)
        for key in self.paths:
            with self.subTest(scope=key):
                p, old, times = self.change_bytes_without_size_or_mtime(key)
                try:
                    with self.assertRaises(ValueError):
                        worker.integrity_guards(self.out)
                finally:
                    p.write_bytes(old)
                    os.utime(p, ns=(times.st_atime_ns, times.st_mtime_ns))
        self.assertEqual(worker.integrity_guards(self.out), self.expected)

    def test_later_tampering_is_not_accepted_by_job_guards(self):
        for key in self.paths:
            with self.subTest(scope=key):
                guards = worker.integrity_guards(self.out)
                p, old, times = self.change_bytes_without_size_or_mtime(key)
                try:
                    with self.assertRaisesRegex(ValueError, "protected RTL tree changed"):
                        worker.check_guards(self.out, guards)
                finally:
                    p.write_bytes(old)
                    os.utime(p, ns=(times.st_atime_ns, times.st_mtime_ns))

    def test_change_after_validation_cannot_become_a_new_guard_baseline(self):
        original = worker.check_candidate

        def check_then_change(*args, **kwargs):
            result = original(*args, **kwargs)
            self.change_bytes_without_size_or_mtime("candidate_scope")
            return result

        with patch.object(worker, "check_candidate", side_effect=check_then_change):
            guards = worker.integrity_guards(self.out)
        with self.assertRaisesRegex(ValueError, "protected RTL tree changed"):
            worker.check_guards(self.out, guards)

    def test_active_repair_protects_its_audit_tree(self):
        review = dict(self.review)
        review.pop("decision")
        review.pop("decision_audit_inventory")
        review["repair"] = {"status": "active", "audit_inventory": self.expected[-1]["inventory"]}
        self.metadata.write_text(json.dumps(review))
        self.assertEqual(worker.integrity_guards(self.out), self.expected)
        self.change_bytes_without_size_or_mtime("audit_scope")
        with self.assertRaisesRegex(ValueError, "audit tests changed"):
            worker.integrity_guards(self.out)


if __name__ == "__main__":
    unittest.main()
