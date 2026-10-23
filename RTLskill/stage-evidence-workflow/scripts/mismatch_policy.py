#!/usr/bin/env python3
"""Classify source-integrity failures separately from summary-quality failures."""
from __future__ import annotations

from typing import Dict, Iterable, Mapping


INTEGRITY_PASS = "pass"
INTEGRITY_HOLD = "hold"
INTEGRITY_INFRA = "infra_uncertain"

SUMMARY_PASS = "pass"
SUMMARY_RETRY = "retry_required"
SUMMARY_DEGRADED = "degraded"
SUMMARY_UNAVAILABLE = "unavailable"


def integrity_state(packet: Mapping[str, object], *, attempt: int,
                    extra_errors: Iterable[Mapping[str, object]] = ()) -> str:
    if list(extra_errors):
        return INTEGRITY_HOLD
    status = str(packet.get("input_status", "no_input"))
    if status == "ready_for_model":
        return INTEGRITY_PASS
    if status == "infra_uncertain":
        return INTEGRITY_INFRA
    if status in {"incomplete", "conflict", "no_input"}:
        return INTEGRITY_HOLD
    return INTEGRITY_HOLD


def summary_state(errors: Iterable[Mapping[str, object]], *, model_status: str,
                  attempt: int, model_available: bool) -> str:
    error_list = list(errors)
    if not model_available:
        return SUMMARY_RETRY if attempt == 0 else SUMMARY_UNAVAILABLE
    if not error_list and model_status == "consistent":
        return SUMMARY_PASS
    return SUMMARY_RETRY if attempt == 0 else SUMMARY_DEGRADED


def review_state(integrity: str, summary: str, *, attempt: int) -> str:
    if integrity == INTEGRITY_INFRA:
        return "INFRA_UNCERTAIN"
    if integrity == INTEGRITY_HOLD:
        return "RECHECK_REQUIRED" if attempt == 0 else "BLOCKED_EVIDENCE"
    if summary == SUMMARY_PASS:
        return "CONSISTENT"
    if summary == SUMMARY_RETRY:
        return "MODEL_REVIEW_REQUIRED"
    return "DEGRADED_SUMMARY"


def next_action(integrity: str, summary: str, *, attempt: int) -> str:
    if integrity == INTEGRITY_INFRA:
        return "stop_as_infra_uncertain"
    if integrity == INTEGRITY_HOLD:
        return "reread_exact_artifacts" if attempt == 0 else "stop_without_PPA"
    if summary == SUMMARY_RETRY:
        return "review_same_run_once"
    if summary in {SUMMARY_DEGRADED, SUMMARY_UNAVAILABLE}:
        return "use_deterministic_fallback"
    return "allow_controller_gate"


def adapter_report(*, integrity: str, summary: str,
                   errors: Iterable[Mapping[str, object]]) -> Dict[str, object]:
    return {
        "integrity_state": integrity,
        "summary_state": summary,
        "recoverable_representation_error": (
            integrity == INTEGRITY_PASS and summary in {SUMMARY_RETRY, SUMMARY_DEGRADED}
        ),
        "error_kinds": sorted({str(error.get("kind", "unknown")) for error in errors}),
    }
