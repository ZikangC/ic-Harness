#!/usr/bin/env python3
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))
from optimization_obligation_adapter import match_optimization_sta_obligation


BASE = {
    "obligation_id": "sta:one", "campaign_id": "campaign",
    "target_module": "compute", "rtl_fingerprint": "a" * 64,
    "required_scope": "top_level_partial",
}
REGISTRY = {
    "hard_gate": "pass", "conflicts": [], "current_blackboxes": ["next"],
    "pending_transaction": {
        "transaction_id": "mainline", "kind": "blackbox_expansion",
        "module_id": "next", "partition": "top_level_sta",
    },
}

merged = match_optimization_sta_obligation(BASE, REGISTRY,
                                            current_rtl_fingerprint="a" * 64)
assert merged["status"] == "merge_with_mainline"
assert merged["mainline_transaction_id"] == "mainline"

complete = match_optimization_sta_obligation(
    {**BASE, "required_scope": "top_level_complete"}, REGISTRY)
assert complete["status"] == "unavailable"

ready = match_optimization_sta_obligation(
    {**BASE, "required_scope": "top_level_complete"},
    {"hard_gate": "pass", "current_blackboxes": [], "pending_transaction": None})
assert ready["status"] == "standalone_ready"

stale = match_optimization_sta_obligation(BASE, REGISTRY,
                                           current_rtl_fingerprint="b" * 64)
assert stale["status"] == "hold"

print("optimization obligation adapter tests: PASS")
