#!/usr/bin/env python3
"""Task-02 matching of portable Optimization STA obligations to mainline work."""
from __future__ import annotations

from collections.abc import Mapping


def optimization_obligation_decision_actions(
        match: Mapping[str, object]) -> dict[str, dict[str, object]]:
    """Project one ready obligation match into the immutable Pro action space."""
    if match.get("status") not in {"merge_with_mainline", "standalone_ready"}:
        return {}
    obligation_id = str(match.get("obligation_id") or "").strip()
    if not obligation_id:
        return {}
    return {
        "continue": {
            "purpose": "执行与当前主线兼容的单次顶层 STA，并消费优化验证义务",
            "executor_ref": "project.optimization_external_sta",
            "execution_mode": "verify_shared_top_level_sta",
            "parameter_schema": {"flash_attempts": {"enum": [1]}},
            "source": "optimization_obligation_matcher",
            "source_ref": obligation_id,
        },
    }


def match_optimization_sta_obligation(
        obligation: Mapping[str, object], registry: Mapping[str, object], *,
        current_rtl_fingerprint: str | None = None) -> dict[str, object]:
    """Return a deterministic project scheduling result, never an RTL verdict."""
    obligation_id = str(obligation.get("obligation_id") or "")
    campaign_id = str(obligation.get("campaign_id") or "")
    module = str(obligation.get("target_module") or "")
    required_scope = str(obligation.get("required_scope") or "")
    expected_fingerprint = str(obligation.get("rtl_fingerprint") or "")
    if not all((obligation_id, campaign_id, module, expected_fingerprint)):
        return {"status": "hold", "reason_codes": ["obligation_identity_incomplete"]}
    if required_scope not in {"top_level_partial", "top_level_complete"}:
        return {"status": "hold", "reason_codes": ["unsupported_sta_scope"]}
    if (current_rtl_fingerprint is not None
            and current_rtl_fingerprint != expected_fingerprint):
        return {"status": "hold", "reason_codes": ["optimization_rtl_fingerprint_changed"]}
    if registry.get("hard_gate") not in (None, "pass") or registry.get("conflicts"):
        return {"status": "hold", "reason_codes": ["module_registry_not_trustworthy"]}

    blackboxes = registry.get("current_blackboxes")
    if not isinstance(blackboxes, list):
        hot = registry.get("hot_view")
        blackboxes = ((hot or {}).get("current_blackboxes")
                      if isinstance(hot, Mapping) else [])
    blackboxes = [str(item) for item in blackboxes] if isinstance(blackboxes, list) else []
    pending = registry.get("pending_transaction")
    pending = pending if isinstance(pending, Mapping) else {}
    pending_partition = str(pending.get("partition") or "")
    pending_module = str(pending.get("module_id") or pending.get("module") or "")
    pending_kind = str(pending.get("kind") or "")

    base = {
        "schema_version": 1,
        "obligation_id": obligation_id,
        "campaign_id": campaign_id,
        "optimization_module": module,
        "rtl_fingerprint": expected_fingerprint,
        "required_scope": required_scope,
    }
    if required_scope == "top_level_complete":
        if blackboxes:
            return {**base, "status": "unavailable",
                    "reason_codes": ["complete_top_level_sta_blocked_by_remaining_blackboxes"],
                    "remaining_blackboxes": sorted(blackboxes),
                    "recommended_resolution": "rollback_optional_optimization_and_defer"}
        return {**base, "status": "standalone_ready", "reason_codes": [],
                "executor_ref": "project.top_level_sta",
                "measurement_partition": "top_level_sta"}

    if (pending and pending_partition == "top_level_sta"
            and pending_kind in {"blackbox_expansion", "integration_verification"}):
        return {**base, "status": "merge_with_mainline", "reason_codes": [],
                "mainline_transaction_id": pending.get("transaction_id"),
                "mainline_module": pending_module,
                "executor_ref": "project.mainline_top_level_sta",
                "measurement_partition": "top_level_sta"}
    return {**base, "status": "standalone_ready", "reason_codes": [],
            "executor_ref": "project.top_level_sta",
            "measurement_partition": "top_level_sta"}
