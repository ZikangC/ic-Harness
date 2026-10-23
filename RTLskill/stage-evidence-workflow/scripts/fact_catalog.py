#!/usr/bin/env python3
"""Build a bounded Fact Catalog for one immutable current-stage packet."""
from __future__ import annotations

import hashlib
import json
from typing import Dict, Iterable, List, Mapping, Tuple


CATALOG_SCHEMA_VERSION = 2
DEFAULT_MAX_FACTS_PER_SKILL = 16
DEFAULT_MAX_TOTAL_FACTS = 60
DEFAULT_MAX_SELECTED_FACTS = 12
PRIORITY_ORDER = ("gate", "mainline", "decision", "diagnostic")
PRIORITY_RANK = {name: index for index, name in enumerate(PRIORITY_ORDER)}


def _escape(token: object) -> str:
    return str(token).replace("~", "~0").replace("/", "~1")


def _unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def _value_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _fact_id(role: str, pointer: str) -> str:
    # IDs are deterministic for reproducibility, but validity is scoped to the
    # packet_fingerprint and never inferred across packets.
    digest = hashlib.sha256(f"{role}|{pointer}".encode("utf-8")).hexdigest()[:16]
    return f"f_{digest}"


def _iter_leaf_facts(value: object, pointer: str) -> Iterable[Tuple[str, object]]:
    """Walk the real JSON structure without inventing entity-key paths."""
    if isinstance(value, dict):
        if not value:
            yield pointer, value
            return
        for key in sorted(value):
            yield from _iter_leaf_facts(value[key], f"{pointer}/{_escape(key)}")
        return
    if isinstance(value, list):
        if not value or all(not isinstance(item, (dict, list)) for item in value):
            yield pointer, value
            return
        for index, item in enumerate(value):
            yield from _iter_leaf_facts(item, f"{pointer}/{index}")
        return
    yield pointer, value


def _leaf_name(pointer: str) -> str:
    tokens = [_unescape(token) for token in pointer.split("/") if token]
    if not tokens:
        return "value"
    if tokens[-1].isdigit() and len(tokens) > 1:
        return tokens[-2]
    return tokens[-1]


def _semantic_name(role: str, pointer: str) -> str:
    tokens = [_unescape(token) for token in pointer.split("/") if token]
    return ".".join([role.removeprefix("skill_"), *tokens])


def _priority(pointer: str, default: str, required_fields: set[str]) -> str:
    leaf = _leaf_name(pointer)
    lowered = pointer.lower()
    if leaf in required_fields or "/hard_gate/" in lowered or lowered.startswith("/hard_gate/"):
        return "gate"
    if any(token in lowered for token in (
            "/mainline", "/pending", "/current_", "/verification_pending")):
        return "mainline"
    if any(token in lowered for token in (
            "/eligible", "/recommended", "/candidate", "/measurement",
            "/retry", "/phase", "/status")):
        return "decision"
    if any(token in lowered for token in (
            "/error", "/warning", "/diagnostic", "/missing", "/conflict")):
        return "diagnostic"
    return default if default in PRIORITY_RANK else "decision"


def _required_value(fact: Mapping[str, object]) -> object:
    raw_values = fact.get("raw_values")
    if isinstance(raw_values, list) and raw_values:
        return raw_values[0]
    return fact.get("value")


def build_fact_catalog(skill_outputs: Mapping[str, object], *,
                       structured_sources: Mapping[str, object] | None = None,
                       required_facts: Mapping[str, object] | None = None,
                       max_facts_per_skill: int = DEFAULT_MAX_FACTS_PER_SKILL,
                       max_total_facts: int = DEFAULT_MAX_TOTAL_FACTS) -> Dict[str, object]:
    """Create a priority-aware, fair catalog for one packet.

    Skill evidence envelopes and parsed current-run structures enter the same
    bounded namespace. Machine execution contracts are validated and handed
    off separately; they never enter the Evidence Model catalog. Raw forensic
    text never enters either path.
    """
    if max_facts_per_skill < 1 or max_total_facts < 1:
        raise ValueError("fact catalog limits must be positive")
    required_fields = {str(key) for key in (required_facts or {})}
    candidates: Dict[str, List[Dict[str, object]]] = {}
    source_identities: Dict[str, Dict[str, object]] = {}

    for role in sorted(skill_outputs):
        envelope = skill_outputs.get(role)
        if not isinstance(envelope, dict):
            continue
        identity = envelope.get("identity")
        if isinstance(identity, dict):
            source_identities[role] = dict(identity)
        # execution_contract is a machine-control artifact.  It is validated
        # and passed through separately by evidence_workflow.py, but never
        # enters the Evidence Model's catalog or summary context.
        for root, default_priority in (("hard_gate", "gate"),
                                       ("summary_input", "decision")):
            root_value = envelope.get(root)
            if not isinstance(root_value, (dict, list)):
                continue
            for pointer, value in _iter_leaf_facts(root_value, f"/{root}"):
                candidates.setdefault(role, []).append({
                    "source_role": role,
                    "skill": envelope.get("skill"),
                    "source_identity": dict(identity) if isinstance(identity, dict) else {},
                    "source_pointer": pointer,
                    "name": _leaf_name(pointer),
                    "semantic_name": _semantic_name(role, pointer),
                    "value": value,
                    "value_type": _value_type(value),
                    "priority": _priority(pointer, default_priority, required_fields),
                    "required": False,
                })

    for role in sorted(structured_sources or {}):
        value = (structured_sources or {}).get(role)
        if not isinstance(value, (dict, list)):
            continue
        default_priority = "mainline" if role in {"progress_delta", "controller_state"} else "decision"
        if role == "trend_summary":
            default_priority = "diagnostic"
        for pointer, leaf_value in _iter_leaf_facts(value, ""):
            pointer = pointer or "/value"
            candidates.setdefault(role, []).append({
                "source_role": role,
                "skill": None,
                "source_identity": {},
                "source_pointer": pointer,
                "name": _leaf_name(pointer),
                "semantic_name": _semantic_name(role, pointer),
                "value": leaf_value,
                "value_type": _value_type(leaf_value),
                "priority": _priority(pointer, default_priority, required_fields),
                "required": False,
            })

    for field in sorted(required_facts or {}):
        raw_fact = (required_facts or {}).get(field)
        if not isinstance(raw_fact, dict):
            continue
        pointer = f"/required/{_escape(field)}"
        value = _required_value(raw_fact)
        candidates.setdefault("stage_contract", []).append({
            "source_role": "stage_contract",
            "skill": None,
            "source_identity": {},
            "source_pointer": pointer,
            "name": field,
            "semantic_name": field,
            "value": value,
            "value_type": _value_type(value),
            "priority": "gate",
            "required": True,
            "source_roles": list(raw_fact.get("sources", []))
                if isinstance(raw_fact.get("sources"), list) else [],
        })

    for values in candidates.values():
        values.sort(key=lambda fact: (
            PRIORITY_RANK.get(str(fact.get("priority")), 99),
            str(fact.get("source_pointer")),
        ))

    selected: List[Dict[str, object]] = []
    per_role_count = {role: 0 for role in candidates}
    roles = sorted(candidates)
    for priority in PRIORITY_ORDER:
        index = 0
        while len(selected) < max_total_facts:
            added = False
            for role in roles:
                if per_role_count[role] >= max_facts_per_skill:
                    continue
                priority_facts = [fact for fact in candidates[role]
                                  if fact.get("priority") == priority]
                if index >= len(priority_facts):
                    continue
                selected.append(priority_facts[index])
                per_role_count[role] += 1
                added = True
                if len(selected) >= max_total_facts:
                    break
            if not added:
                break
            index += 1

    facts: Dict[str, Dict[str, object]] = {}
    source_fact_ids: Dict[str, List[str]] = {role: [] for role in roles}
    for fact in selected:
        role = str(fact["source_role"])
        fact_id = _fact_id(role, str(fact["source_pointer"]))
        facts[fact_id] = {"fact_id": fact_id, **fact}
        source_fact_ids[role].append(fact_id)
    omitted = {
        role: len(candidates[role]) - per_role_count[role]
        for role in roles if len(candidates[role]) > per_role_count[role]
    }
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "scope": "current_packet",
        "facts": facts,
        "source_fact_ids": source_fact_ids,
        "skill_fact_ids": {role: ids for role, ids in source_fact_ids.items()
                           if role.startswith("skill_")},
        "source_identities": source_identities,
        "required_fact_ids": [fact_id for fact_id, fact in facts.items()
                              if fact.get("required") is True],
        "fact_count": len(facts),
        "limits": {
            "max_facts_per_source": max_facts_per_skill,
            "max_total_facts": max_total_facts,
        },
        "omitted_fact_counts": omitted,
    }


def resolve_fact_refs(catalog: Mapping[str, object], refs: Iterable[object], *,
                      max_selected: int = DEFAULT_MAX_SELECTED_FACTS) -> Tuple[List[Dict[str, object]], List[str]]:
    facts = catalog.get("facts")
    facts = facts if isinstance(facts, dict) else {}
    selected: List[Dict[str, object]] = []
    unknown: List[str] = []
    seen = set()
    for raw_ref in refs:
        ref = str(raw_ref)
        if ref in seen:
            continue
        seen.add(ref)
        fact = facts.get(ref)
        if not isinstance(fact, dict):
            unknown.append(ref)
            continue
        if len(selected) < max_selected:
            selected.append(dict(fact))
    return selected, unknown


def deterministic_fallback_facts(catalog: Mapping[str, object], *,
                                 max_selected: int = DEFAULT_MAX_SELECTED_FACTS) -> List[Dict[str, object]]:
    facts = catalog.get("facts")
    facts = facts if isinstance(facts, dict) else {}
    grouped: Dict[str, Dict[str, List[Dict[str, object]]]] = {}
    for raw_fact in facts.values():
        if not isinstance(raw_fact, dict):
            continue
        fact = dict(raw_fact)
        role = str(fact.get("source_role", ""))
        priority = str(fact.get("priority", "decision"))
        if priority not in PRIORITY_RANK:
            priority = "decision"
        grouped.setdefault(role, {name: [] for name in PRIORITY_ORDER})[priority].append(fact)
    for priorities in grouped.values():
        for values in priorities.values():
            values.sort(key=lambda fact: str(fact.get("source_pointer")))

    selected: List[Dict[str, object]] = []
    roles = sorted(grouped)
    for priority in PRIORITY_ORDER:
        index = 0
        while len(selected) < max_selected:
            added = False
            for role in roles:
                values = grouped[role][priority]
                if index < len(values):
                    selected.append(values[index])
                    added = True
                    if len(selected) >= max_selected:
                        break
            if not added:
                break
            index += 1
    return selected


def catalog_for_prompt(catalog: Mapping[str, object]) -> Dict[str, object]:
    """Return only the bounded fields that the Evidence Model needs."""
    facts = catalog.get("facts")
    facts = facts if isinstance(facts, dict) else {}
    return {
        "schema_version": catalog.get("schema_version"),
        "scope": catalog.get("scope", "current_packet"),
        "facts": [
            {field: facts[key].get(field) for field in (
                "fact_id", "source_role", "name", "semantic_name", "value",
                "value_type", "priority", "required")}
            for key in sorted(facts) if isinstance(facts[key], dict)
        ],
        "required_fact_ids": catalog.get("required_fact_ids", []),
        "fact_count": catalog.get("fact_count", len(facts)),
        "omitted_fact_counts": catalog.get("omitted_fact_counts", {}),
    }


def catalog_fingerprint(catalog: Mapping[str, object]) -> str:
    payload = json.dumps(catalog, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
