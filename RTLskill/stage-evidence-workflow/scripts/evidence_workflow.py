#!/usr/bin/env python3
"""Run or inspect one current-stage evidence consolidation workflow.

The script deliberately separates deterministic facts from model judgment. It
reads only explicitly supplied current-stage files; it never scans a project
history, starts EDA, or uses SSH.  The unified ``run`` command invokes only a
caller-supplied argv template, so model transport remains project-configurable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from fact_catalog import (DEFAULT_MAX_FACTS_PER_SKILL, DEFAULT_MAX_SELECTED_FACTS,
                          DEFAULT_MAX_TOTAL_FACTS,
                          build_fact_catalog, catalog_fingerprint,
                          catalog_for_prompt, deterministic_fallback_facts,
                          resolve_fact_refs)
from mismatch_policy import (INTEGRITY_PASS, SUMMARY_PASS, adapter_report,
                             integrity_state, next_action, review_state,
                             summary_state)


WORKFLOW = "stage-evidence-workflow"
SCHEMA_VERSION = 3
DEFAULT_MAX_ARTIFACT_BYTES = 262144
DEFAULT_PROMPT_MAX_CHARS = 180000
REQUIRED_MODEL_KEYS = {"schema_version", "packet_fingerprint", "stage", "run_id",
                       "status", "evidence_groups", "summary"}
MODEL_STATUSES = {"consistent", "conflict", "incomplete", "unavailable"}
DISALLOWED_ROLES = {
    "history", "progress", "progress_history", "pro_progress", "old_experiments",
    "task_rules", "policy", "pro_prompt",
}
CONTEXT_ROLES = {
    "progress_delta", "sta_closure_audit", "controller_state", "stage_context", "trend_summary",
    "run_evidence",
}
SKILL_OUTPUT_ROLE_PREFIX = "skill_"

def _write_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _canonical(value: object) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if value is None:
        return "null"
    text = str(value).strip()
    if not text:
        return ""
    # Feedback facts often append an explanation after the machine value.
    return text.split(None, 1)[0]


def _packet_fingerprint(packet: Mapping[str, object]) -> str:
    fields = {
        key: packet.get(key) for key in (
            "schema_version", "workflow", "stage", "run_id", "contract",
            "input_status", "deterministic_facts", "missing", "conflicts",
            "artifacts", "history_included", "history_mode", "scope",
            "skill_outputs", "skill_output_errors", "fact_catalog",
        )
    }
    payload = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _diagnostic_excerpt(text: str, *, max_lines: int = 20, max_chars: int = 6000) -> str:
    pattern = re.compile(
        r"error|fatal|failed|failure|timeout|timed out|warning|unlinked|blackbox|traceback",
        re.IGNORECASE,
    )
    lines = [line[:500] for line in text.splitlines() if pattern.search(line)]
    excerpt = "\n".join(lines[:max_lines])
    return excerpt[:max_chars]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _text_window(path: Path, limit: int) -> Tuple[str, bool, int]:
    size = path.stat().st_size
    if size <= limit:
        return path.read_text(encoding="utf-8", errors="replace"), False, size
    head = max(1, limit // 2)
    tail = max(1, limit - head)
    with path.open("rb") as source:
        first = source.read(head).decode("utf-8", errors="replace")
        source.seek(max(0, size - tail))
        last = source.read(tail).decode("utf-8", errors="replace")
    return first + "\n... EVIDENCE_INPUT_TRUNCATED ...\n" + last, True, head + tail


def _artifact_record(role: str, path: Path, max_bytes: int) -> Dict[str, object]:
    try:
        if not path.is_file():
            return {"role": role, "path": path.as_posix(), "status": "missing"}
        text, truncated, included = _text_window(path, max_bytes)
        return {
            "role": role,
            "path": path.as_posix(),
            "status": "truncated" if truncated else "readable",
            "size_bytes": path.stat().st_size,
            "included_bytes": included,
            "sha256": _sha256(path),
            "text": text,
        }
    except OSError as error:
        return {"role": role, "path": path.as_posix(), "status": "unreadable",
                "error": f"{type(error).__name__}: {error}"}


def _parse_feedback(text: str) -> Dict[str, List[str]]:
    values: Dict[str, List[str]] = {}
    for line in text.splitlines():
        if "=" not in line or line.startswith("---"):
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if re.fullmatch(r"[a-z][a-z0-9_]*", key):
            values.setdefault(key, []).append(value.strip())
    return values


def _add_fact(facts: Dict[str, List[Dict[str, object]]], key: str, value: object, role: str) -> None:
    if isinstance(value, (dict, list)):
        return
    facts.setdefault(key, []).append({"value": _canonical(value), "raw": value, "source_role": role})


def _json_facts(value: Mapping[str, object], role: str) -> Dict[str, List[Dict[str, object]]]:
    facts: Dict[str, List[Dict[str, object]]] = {}
    for key, item in value.items():
        if key == "sta_closure_audit" and isinstance(item, dict):
            for nested_key, nested_value in item.items():
                _add_fact(facts, str(nested_key), nested_value, role)
        else:
            _add_fact(facts, str(key), item, role)
    return facts


def _merge_facts(target: Dict[str, List[Dict[str, object]]], source: Dict[str, List[Dict[str, object]]]) -> None:
    for key, values in source.items():
        target.setdefault(key, []).extend(values)


def _contract(stage: str, custom: Optional[Mapping[str, object]]) -> Dict[str, object]:
    if custom is not None:
        result = dict(custom)
        declared_stage = result.get("stage")
        if declared_stage is not None and declared_stage != stage:
            raise ValueError(
                f"evidence contract stage {declared_stage!r} does not match requested stage {stage!r}")
        result["stage"] = stage
        result.setdefault("objective", f"organize evidence for {stage}")
        result.setdefault("required_fields", [])
        return result
    # Generic library use may omit a contract, but then no project-specific
    # field can become a hard integrity requirement. Production controllers
    # should always supply their own explicit stage contract.
    return {
        "stage": stage,
        "objective": f"organize evidence for {stage}",
        "required_fields": [],
    }


def _skill_output_envelope(role: str, text: str) -> Tuple[Optional[Dict[str, object]], Optional[Dict[str, object]]]:
    """Validate one portable, current-stage Skill output envelope."""
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        return None, {"role": role, "kind": "skill_output_invalid_json", "detail": str(error)}
    if not isinstance(value, dict):
        return None, {"role": role, "kind": "skill_output_not_object"}
    required = {"schema_version", "skill", "scope", "identity", "hard_gate", "summary_input"}
    missing = sorted(required - set(value))
    if missing:
        return None, {"role": role, "kind": "skill_output_missing_fields", "fields": missing}
    if value.get("schema_version") != 1:
        return None, {"role": role, "kind": "skill_output_schema_unsupported"}
    if value.get("scope") != "current_stage" or value.get("history_included") is True:
        return None, {"role": role, "kind": "skill_output_scope_invalid"}
    if not isinstance(value.get("skill"), str) or not value.get("skill"):
        return None, {"role": role, "kind": "skill_output_name_invalid"}
    for key in ("identity", "hard_gate", "summary_input"):
        if not isinstance(value.get(key), dict):
            return None, {"role": role, "kind": "skill_output_field_invalid", "field": key}
    execution_contract = value.get("execution_contract")
    if execution_contract is not None:
        error = _execution_contract_error(execution_contract)
        if error is not None:
            return None, {"role": role, "kind": "skill_execution_contract_invalid", "detail": error}
    return {key: value.get(key) for key in (
        "schema_version", "skill", "scope", "history_included",
        "identity", "hard_gate", "summary_input", "execution_contract",
    )}, None


def _execution_contract_error(value: object) -> Optional[str]:
    """Validate transport shape without interpreting domain action names."""
    if not isinstance(value, dict):
        return "execution_contract must be an object"
    required = {
        "schema_version", "contract_kind", "enforcement", "current_phase",
        "applies_to_actions", "allowed_actions", "forbidden_actions",
        "hard_constraints", "recommended_actions", "strategic_questions",
        "retry_budget", "resume_context",
    }
    missing = sorted(required - set(value))
    if missing:
        return f"missing fields: {missing}"
    if value.get("schema_version") != 1:
        return "unsupported execution contract schema_version"
    if value.get("enforcement") not in {"hard", "advisory"}:
        return "enforcement must be hard or advisory"
    for key in ("applies_to_actions", "allowed_actions", "forbidden_actions", "strategic_questions"):
        items = value.get(key)
        if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
            return f"{key} must be an array of strings"
    if set(value.get("allowed_actions", [])).intersection(value.get("forbidden_actions", [])):
        return "allowed_actions and forbidden_actions overlap"
    for key in ("hard_constraints", "retry_budget", "resume_context"):
        if not isinstance(value.get(key), dict):
            return f"{key} must be an object"
    recommended = value.get("recommended_actions")
    if not isinstance(recommended, list) or not all(isinstance(item, dict) for item in recommended):
        return "recommended_actions must be an array of objects"
    if any(not isinstance(item.get("action"), str) for item in recommended):
        return "every recommended action must name an action"
    if not isinstance(value.get("action_parameters", {}), dict):
        return "action_parameters must be an object"
    if not isinstance(value.get("execution_profiles", {}), dict):
        return "execution_profiles must be an object"
    return None


def _validated_execution_contracts(packet: Mapping[str, object]) -> Dict[str, object]:
    """Resolve source-authored contracts independently from model selection."""
    outputs = packet.get("skill_outputs")
    outputs = outputs if isinstance(outputs, dict) else {}
    result: Dict[str, object] = {}
    for role, envelope in outputs.items():
        if not isinstance(envelope, dict) or not isinstance(envelope.get("execution_contract"), dict):
            continue
        result[str(role)] = {
            "skill": envelope.get("skill"),
            "source_identity": envelope.get("identity", {}),
            "packet_fingerprint": packet.get("packet_fingerprint"),
            "contract": envelope["execution_contract"],
        }
    return result


def _required_fields(contract: Mapping[str, object]) -> List[str]:
    raw = contract.get("required_fields", [])
    return [str(item) for item in raw] if isinstance(raw, list) else []


def prepare(*, stage: str, run_id: str, artifacts: Mapping[str, Path],
            contract: Optional[Mapping[str, object]] = None,
            max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
            prompt_max_chars: int = DEFAULT_PROMPT_MAX_CHARS,
            max_facts_per_skill: int = DEFAULT_MAX_FACTS_PER_SKILL,
            max_total_facts: int = DEFAULT_MAX_TOTAL_FACTS) -> Dict[str, object]:
    if not stage.strip() or not run_id.strip():
        raise ValueError("stage and run_id are required")
    if max_artifact_bytes < 1024 or prompt_max_chars < 4096:
        raise ValueError("input limits are too small")
    stage_contract = _contract(stage, contract)
    records = [_artifact_record(role, path, max_artifact_bytes)
               for role, path in sorted(artifacts.items())]
    facts: Dict[str, List[Dict[str, object]]] = {}
    skill_outputs: Dict[str, Dict[str, object]] = {}
    skill_output_errors: List[Dict[str, object]] = []
    structured_sources: Dict[str, object] = {}
    diagnostic_excerpts: Dict[str, str] = {}
    expected_identities = stage_contract.get("expected_skill_identities", {})
    expected_identities = expected_identities if isinstance(expected_identities, dict) else {}
    for record in records:
        role = str(record["role"])
        text = record.get("text")
        if not isinstance(text, str) or record.get("status") not in {"readable", "truncated"}:
            continue
        excerpt = _diagnostic_excerpt(text)
        if excerpt:
            diagnostic_excerpts[role] = excerpt
        if role.startswith(SKILL_OUTPUT_ROLE_PREFIX):
            if record.get("status") == "truncated":
                skill_output_errors.append({"role": role, "kind": "skill_output_truncated"})
            else:
                envelope, error = _skill_output_envelope(role, text)
                if error is not None:
                    skill_output_errors.append(error)
                elif envelope is not None:
                    skill_outputs[role] = envelope
                    expected = expected_identities.get(role)
                    identity = envelope.get("identity")
                    if isinstance(expected, dict) and (
                            not isinstance(identity, dict)
                            or any(identity.get(key) != value for key, value in expected.items())):
                        skill_output_errors.append({
                            "role": role, "kind": "skill_output_identity_mismatch",
                            "expected": expected, "actual": identity,
                        })
            continue
        parsed: object = None
        if record.get("status") == "readable":
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
        if isinstance(parsed, (dict, list)):
            structured_sources[role] = parsed
        if (role.endswith("result") or role == "result") and isinstance(parsed, dict):
            _merge_facts(facts, _json_facts(parsed, role))
        if role.endswith("feedback") or role in {"feedback", "sta_log_excerpt"}:
            feedback_values = _parse_feedback(text)
            structured_sources[role] = {
                key: values[0] if len(values) == 1 else values
                for key, values in feedback_values.items()
            }
            for key, values in feedback_values.items():
                for value in values:
                    _add_fact(facts, key, value, role)

    required_skill_roles = stage_contract.get("required_skill_roles", [])
    if isinstance(required_skill_roles, list):
        for role in required_skill_roles:
            if isinstance(role, str) and role not in skill_outputs:
                skill_output_errors.append({"role": role, "kind": "required_skill_output_missing"})

    required = _required_fields(stage_contract)
    alternatives = stage_contract.get("any_of", [])
    satisfied_alternative_fields: set[str] = set()
    if isinstance(alternatives, list):
        for group in alternatives:
            if isinstance(group, list) and any(facts.get(str(field)) for field in group):
                satisfied_alternative_fields.update(str(field) for field in group)
    deterministic: Dict[str, object] = {}
    missing: List[str] = []
    conflicts: List[Dict[str, object]] = []
    for field in required:
        values = facts.get(field, [])
        unique: Dict[str, List[Dict[str, object]]] = {}
        for item in values:
            unique.setdefault(str(item["value"]), []).append(item)
        if not unique:
            if field in satisfied_alternative_fields:
                continue
            missing.append(field)
            continue
        if len(unique) > 1:
            conflicts.append({"field": field, "values": sorted(unique), "sources": values})
            continue
        only = next(iter(unique))
        deterministic[field] = {"value": only,
                                "sources": sorted({str(item["source_role"]) for item in unique[only]}),
                                "raw_values": [item["raw"] for item in unique[only]]}

    bad_records = [record for record in records if record.get("status") in {"missing", "unreadable", "truncated"}]
    if not records:
        input_status = "no_input"
    elif any(record.get("status") in {"missing", "unreadable"} for record in records):
        input_status = "infra_uncertain" if any(record.get("status") == "unreadable" for record in records) else "no_input"
    elif bad_records or missing or conflicts or skill_output_errors:
        input_status = "incomplete" if bad_records or missing else "conflict"
    else:
        input_status = "ready_for_model"
    artifact_roles = sorted({str(record["role"]) for record in records})
    context_roles = [str(record["role"]) for record in records
                     if str(record["role"]) in CONTEXT_ROLES
                     or str(record["role"]).startswith(SKILL_OUTPUT_ROLE_PREFIX)]
    history_mode = "trend" if "trend_summary" in context_roles else "none"
    fact_catalog = build_fact_catalog(
        skill_outputs, structured_sources=structured_sources,
        required_facts=deterministic, max_facts_per_skill=max_facts_per_skill,
        max_total_facts=max_total_facts)
    required_catalog_ids = fact_catalog.get("required_fact_ids", [])
    source_fact_ids = fact_catalog.get("source_fact_ids", {})
    minimum_selection = (len(required_catalog_ids)
                         + sum(1 for role in skill_outputs
                               if isinstance(source_fact_ids, dict)
                               and source_fact_ids.get(role)))
    if len(required_catalog_ids) != len(deterministic):
        skill_output_errors.append({
            "kind": "required_facts_omitted_by_catalog_budget",
            "required": len(deterministic), "catalogued": len(required_catalog_ids),
        })
    if minimum_selection > DEFAULT_MAX_SELECTED_FACTS:
        skill_output_errors.append({
            "kind": "fact_selection_budget_unsatisfiable",
            "minimum": minimum_selection, "limit": DEFAULT_MAX_SELECTED_FACTS,
        })
    if skill_output_errors and input_status == "ready_for_model":
        input_status = "conflict"
    packet = {
        "schema_version": SCHEMA_VERSION,
        "workflow": WORKFLOW,
        "stage": stage,
        "run_id": run_id,
        "objective": stage_contract.get("objective", ""),
        "contract": stage_contract,
        "input_status": input_status,
        "deterministic_state": "ready_for_model" if input_status == "ready_for_model" else input_status,
        "required_fields": required,
        "deterministic_facts": deterministic,
        "missing": missing,
        "conflicts": conflicts,
        "artifacts": [{key: value for key, value in record.items() if key != "text"} for record in records],
        "allowed_source_roles": artifact_roles,
        "history_included": history_mode != "none",
        "history_mode": history_mode,
        "scope": "current_stage_with_trend" if history_mode == "trend" else "current_stage",
        "context_roles": context_roles,
        "skill_outputs": skill_outputs,
        "skill_output_errors": skill_output_errors,
        "fact_catalog": fact_catalog,
        "fact_catalog_fingerprint": catalog_fingerprint(fact_catalog),
        "model_input": {
            "artifact_roles": [str(record["role"]) for record in records],
            "prompt_max_chars": prompt_max_chars,
            "truncated_artifacts": [str(record["role"]) for record in records if record.get("status") == "truncated"],
        },
        "_diagnostic_excerpts": diagnostic_excerpts,
    }
    packet["packet_fingerprint"] = _packet_fingerprint(packet)
    return packet


def render_prompt(packet: Mapping[str, object]) -> str:
    excerpts = packet.get("_diagnostic_excerpts", {})
    excerpt_sections = []
    for role, text in excerpts.items() if isinstance(excerpts, dict) else []:
        excerpt_sections.append(f"### DIAGNOSTIC EXCERPT role={role}\n{text}")
    skill_outputs = packet.get("skill_outputs", {})
    skill_outputs = skill_outputs if isinstance(skill_outputs, dict) else {}
    fact_catalog = packet.get("fact_catalog", {})
    fact_catalog = fact_catalog if isinstance(fact_catalog, dict) else {}
    history_mode = str(packet.get("history_mode", "none"))
    history_included = bool(packet.get("history_included", False))
    prompt = f"""You are the current-stage evidence analyst for an RTL/EDA pipeline.
Analyze exactly one focused stage and one explicit run handoff. Do not run commands,
modify files, import history, apply task-wide policy, or choose an optimization action.
Return one JSON object only.

Stage: {packet.get('stage')}
Run id: {packet.get('run_id')}
Packet fingerprint: {packet.get('packet_fingerprint')}
Objective: {packet.get('objective')}
Scope: {packet.get('scope', 'current_stage')}
History mode: {history_mode}
History included: {str(history_included).lower()}
Required fields: {json.dumps(packet.get('required_fields', []), ensure_ascii=False)}
Missing fields:
{json.dumps(packet.get('missing', []), ensure_ascii=False)}
Deterministic conflicts:
{json.dumps(packet.get('conflicts', []), ensure_ascii=False, indent=2)}

Required Skill roles. The validator owns their identities; do not copy identity
or generation into your output:
{json.dumps(sorted(skill_outputs), ensure_ascii=False)}

Bounded current-stage Fact Catalog. Facts were extracted deterministically from
the current run and Skill envelopes. Select only fact_id values shown here.
Do not create paths, copy values, infer types, or reinterpret a hard gate.
Select at most {DEFAULT_MAX_SELECTED_FACTS} distinct fact_refs across all topics.
Every required_fact_id and every listed Skill role with available facts must be
covered by at least one selected ref:
{json.dumps(catalog_for_prompt(fact_catalog), ensure_ascii=False, indent=2)}

Optional bounded diagnostic excerpts are only explanatory context. They are not
new facts and cannot be cited instead of a fact_ref:
{chr(10).join(excerpt_sections)}

Return this schema:
{{
  "schema_version": 3,
  "packet_fingerprint": "{packet.get('packet_fingerprint')}",
  "stage": "{packet.get('stage')}",
  "run_id": "{packet.get('run_id')}",
  "status": "consistent|conflict|incomplete|unavailable",
  "evidence_groups": [
    {{
      "topic": "short evidence topic",
      "fact_refs": ["only current packet fact_id values"],
      "summary": "short evidence-only explanation"
    }}
  ],
  "summary": "short evidence-only summary"
}}
Do not emit action, decision, recommendation, allowed_actions, or any RTL/PPA
authorization field. Field order is irrelevant; duplicate fact_refs are ignored.
"""
    limit = int((packet.get("model_input") or {}).get("prompt_max_chars", DEFAULT_PROMPT_MAX_CHARS))
    if len(prompt) > limit:
        return prompt[:limit] + "\nEVIDENCE_PROMPT_TRUNCATED=true\n"
    return prompt


FORBIDDEN_POLICY_FIELDS = {
    "action", "actions", "recommended_action", "recommendation", "decision",
    "allowed_actions", "authorization", "flash_directive",
}


def _forbidden_fields(value: object) -> List[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in FORBIDDEN_POLICY_FIELDS:
                found.add(str(key))
            found.update(_forbidden_fields(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_forbidden_fields(item))
    return sorted(found)


def _load_model_output(value: object) -> Dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("model output must be a JSON object")
    missing = REQUIRED_MODEL_KEYS - set(value)
    if missing:
        raise ValueError(f"model output missing keys: {sorted(missing)}")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported model output schema_version")
    if value.get("status") not in MODEL_STATUSES:
        raise ValueError("model output status is invalid")
    groups = value.get("evidence_groups")
    if not isinstance(groups, list):
        raise ValueError("model evidence_groups must be an array")
    for index, group in enumerate(groups):
        if not isinstance(group, dict):
            raise ValueError(f"evidence_groups[{index}] must be an object")
        if not isinstance(group.get("topic"), str) or not group["topic"].strip() \
                or len(group["topic"]) > 160:
            raise ValueError(f"evidence_groups[{index}].topic is invalid")
        refs = group.get("fact_refs")
        if not isinstance(refs, list) or not all(isinstance(item, str) for item in refs):
            raise ValueError(f"evidence_groups[{index}].fact_refs is invalid")
        if not isinstance(group.get("summary"), str) or len(group["summary"]) > 1000:
            raise ValueError(f"evidence_groups[{index}].summary is invalid")
    if not isinstance(value.get("packet_fingerprint"), str):
        raise ValueError("model packet_fingerprint must be a string")
    if not isinstance(value.get("summary"), str) or len(value["summary"]) > 2000:
        raise ValueError("model summary must be a string of at most 2000 characters")
    return value


def _public_fact(fact: Mapping[str, object], *, strategic: bool = False) -> Dict[str, object]:
    fields = ("name", "value", "source_role", "priority") if strategic else (
        "fact_id", "name", "semantic_name", "value", "value_type", "source_role",
        "source_pointer", "priority", "required", "source_roles", "skill",
        "source_identity",
    )
    return {field: fact.get(field) for field in fields if field in fact}


def _validated_groups(catalog: Mapping[str, object], groups: Iterable[Mapping[str, object]]) -> List[Dict[str, object]]:
    result: List[Dict[str, object]] = []
    for group in groups:
        refs = group.get("fact_refs", [])
        facts, _ = resolve_fact_refs(catalog, refs if isinstance(refs, list) else [])
        result.append({
            "topic": group.get("topic"),
            "facts": [_public_fact(fact) for fact in facts],
            "summary": group.get("summary", ""),
        })
    return result


def _fallback_groups(catalog: Mapping[str, object]) -> List[Dict[str, object]]:
    selected = deterministic_fallback_facts(catalog)
    grouped: Dict[str, List[Dict[str, object]]] = {}
    for fact in selected:
        grouped.setdefault(str(fact.get("source_role", "current_stage")), []).append(fact)
    return [{
        "topic": role,
        "facts": [_public_fact(fact) for fact in facts],
        "summary": "由当前 packet 的确定性事实生成。",
    } for role, facts in grouped.items()]


def _strategic_view(groups: Iterable[Mapping[str, object]], summary_text: str,
                    summary_source: str) -> Dict[str, object]:
    compact_groups = []
    for group in groups:
        facts = group.get("facts", [])
        compact_groups.append({
            "topic": group.get("topic"),
            "facts": [_public_fact(fact, strategic=True) for fact in facts
                      if isinstance(fact, dict)],
            "summary": group.get("summary", ""),
        })
    return {"evidence_groups": compact_groups, "summary": summary_text,
            "summary_source": summary_source}


def _retry_context(packet: Mapping[str, object], errors: Iterable[Mapping[str, object]]) -> Dict[str, object]:
    catalog = packet.get("fact_catalog", {})
    catalog = catalog if isinstance(catalog, dict) else {}
    facts = catalog.get("facts", {})
    facts = facts if isinstance(facts, dict) else {}
    skill_outputs = packet.get("skill_outputs", {})
    skill_outputs = skill_outputs if isinstance(skill_outputs, dict) else {}
    return {
        "packet_fingerprint": packet.get("packet_fingerprint"),
        "previous_validation_errors": [
            {"code": str(error.get("kind", "UNKNOWN")).upper(),
             **{key: value for key, value in error.items() if key != "kind"}}
            for error in errors
        ],
        "allowed_fact_refs": sorted(facts),
        "required_fact_refs": list(catalog.get("required_fact_ids", []))
            if isinstance(catalog.get("required_fact_ids"), list) else [],
        "required_skill_roles": sorted(skill_outputs),
    }


def validate_model_output(packet: Mapping[str, object], model_output: Mapping[str, object], *, attempt: int = 0) -> Dict[str, object]:
    if attempt < 0:
        raise ValueError("attempt must be nonnegative")
    errors: List[Dict[str, object]] = []
    warnings: List[Dict[str, object]] = []
    source_errors: List[Dict[str, object]] = []
    packet_fingerprint = packet.get("packet_fingerprint")
    if not isinstance(packet_fingerprint, str) or packet_fingerprint != _packet_fingerprint(packet):
        source_errors.append({"kind": "packet_fingerprint_invalid"})
    try:
        model = _load_model_output(model_output)
    except ValueError as error:
        errors.append({"kind": "invalid_model_output", "detail": str(error)})
        model = {}

    catalog = packet.get("fact_catalog", {})
    catalog = catalog if isinstance(catalog, dict) else {}
    selected_refs: List[str] = []
    model_groups: List[Mapping[str, object]] = []
    if model:
        if model.get("packet_fingerprint") != packet_fingerprint:
            errors.append({"kind": "model_packet_fingerprint_mismatch",
                           "expected": packet_fingerprint,
                           "actual": model.get("packet_fingerprint")})
        if model.get("stage") != packet.get("stage") or model.get("run_id") != packet.get("run_id"):
            errors.append({"kind": "model_stage_run_mismatch"})
        forbidden = _forbidden_fields(model)
        if forbidden:
            errors.append({"kind": "model_policy_violation", "fields": forbidden})
        model_groups = [group for group in model.get("evidence_groups", [])
                        if isinstance(group, dict)]
        seen_refs: set[str] = set()
        for group in model_groups:
            for fact_ref in group.get("fact_refs", []):
                if fact_ref in seen_refs:
                    warnings.append({"kind": "duplicate_fact_ref_ignored", "fact_id": fact_ref})
                    continue
                seen_refs.add(fact_ref)
                selected_refs.append(fact_ref)
        if len(selected_refs) > DEFAULT_MAX_SELECTED_FACTS:
            errors.append({"kind": "selected_fact_limit_exceeded",
                           "selected": len(selected_refs),
                           "limit": DEFAULT_MAX_SELECTED_FACTS})
        _, unknown_refs = resolve_fact_refs(catalog, selected_refs,
                                            max_selected=max(len(selected_refs), 1))
        for fact_ref in unknown_refs:
            errors.append({"kind": "unknown_fact_ref", "fact_ref": fact_ref})
        selected_set = set(selected_refs)
        required_refs = {str(item) for item in catalog.get("required_fact_ids", [])}
        for fact_ref in sorted(required_refs - selected_set):
            errors.append({"kind": "missing_required_fact_ref", "fact_ref": fact_ref})
        source_fact_ids = catalog.get("source_fact_ids", {})
        source_fact_ids = source_fact_ids if isinstance(source_fact_ids, dict) else {}
        skill_outputs = packet.get("skill_outputs", {})
        skill_outputs = skill_outputs if isinstance(skill_outputs, dict) else {}
        for role in sorted(skill_outputs):
            role_refs = {str(item) for item in source_fact_ids.get(role, [])}
            if role_refs and not role_refs.intersection(selected_set):
                errors.append({"kind": "missing_skill_role_coverage", "source_role": role})
        if model.get("status") != "consistent":
            errors.append({"kind": "model_status_not_consistent", "status": model.get("status")})

    integrity = integrity_state(packet, attempt=attempt, extra_errors=source_errors)
    summary = summary_state(errors, model_status=str(model.get("status", "invalid")),
                            attempt=attempt, model_available=bool(model))
    if integrity == INTEGRITY_PASS and summary != SUMMARY_PASS and attempt > 0:
        validated_evidence = _fallback_groups(catalog)
        summary_text = "Evidence Model 摘要不可可靠使用；已使用当前 packet 的确定性事实。"
        summary_source = "deterministic_fallback"
    elif summary == SUMMARY_PASS:
        validated_evidence = _validated_groups(catalog, model_groups)
        summary_text = str(model.get("summary", ""))
        summary_source = "model_retry" if attempt else "model"
    else:
        validated_evidence = []
        summary_text = ""
        summary_source = "pending_model_review"
    state = review_state(integrity, summary, attempt=attempt)
    if packet.get("input_status") == "no_input":
        state = "NO_INPUT"
    hard_gate = "pass" if integrity == INTEGRITY_PASS else "hold"
    integrity_errors = {
        "missing": packet.get("missing", []),
        "conflicts": packet.get("conflicts", []),
        "skill_output_errors": packet.get("skill_output_errors", []),
        "packet_errors": source_errors,
    }
    result: Dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "workflow": WORKFLOW,
        "stage": packet.get("stage"),
        "run_id": packet.get("run_id"),
        "packet_fingerprint": packet_fingerprint,
        "review_state": state,
        "integrity_state": integrity,
        "summary_state": summary,
        "hard_gate": hard_gate,
        "attempt": attempt,
        "errors": errors,
        "integrity_errors": integrity_errors,
        "warnings": warnings,
        "missing": packet.get("missing", []),
        "conflicts": packet.get("conflicts", []),
        "model_status": model.get("status") if model else "invalid",
        "fact_catalog_fingerprint": packet.get("fact_catalog_fingerprint"),
        "validated_evidence": validated_evidence,
        "validated_execution_contracts": (
            _validated_execution_contracts(packet) if integrity == INTEGRITY_PASS else {}),
        "strategic_view": _strategic_view(validated_evidence, summary_text, summary_source),
        "summary": summary_text,
        "summary_source": summary_source,
        "adapter_report": adapter_report(integrity=integrity, summary=summary, errors=errors),
        "next_action": next_action(integrity, summary, attempt=attempt),
    }
    if summary == "retry_required" and integrity == INTEGRITY_PASS:
        result["retry_context"] = _retry_context(packet, errors)
    return result


def render_retry_prompt(packet: Mapping[str, object], review: Mapping[str, object]) -> str:
    context = review.get("retry_context")
    if not isinstance(context, dict):
        raise ValueError("review does not contain retry_context")
    return (render_prompt(packet)
            + "\n\nThis is the one permitted review of the same immutable packet. "
              "Correct only the listed schema/reference problems. Do not add history, "
              "actions, or replacement values.\n```json\n"
            + json.dumps(context, ensure_ascii=False, indent=2)
            + "\n```\n")


def _extract_json_object(text: str) -> Dict[str, object]:
    """Extract the last JSON object from a bounded model response."""
    decoder = json.JSONDecoder()
    fenced = re.findall(r"```(?:json)?\s*(.*?)\s*```", text,
                        flags=re.IGNORECASE | re.DOTALL)
    for candidate in reversed(fenced):
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and REQUIRED_MODEL_KEYS.issubset(value):
            return value
    candidates: List[Dict[str, object]] = []
    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            candidates.append(value)
    complete = [value for value in candidates if REQUIRED_MODEL_KEYS.issubset(value)]
    if complete:
        return complete[-1]
    if not candidates:
        raise ValueError("model response contains no JSON object")
    return candidates[0]


def _model_command(template: object, *, prompt: str, prompt_file: Path,
                   project_root: Path, work_dir: Path, model: str) -> List[str]:
    if not isinstance(template, list) or not template:
        raise ValueError("model_command must be one non-empty argv list")
    values = {
        "prompt": prompt, "prompt_file": prompt_file.as_posix(),
        "project_root": project_root.as_posix(), "work_dir": work_dir.as_posix(),
        "model": model,
    }
    command = [str(part).format(**values) for part in template]
    if not any("{prompt}" in str(part) or "{prompt_file}" in str(part)
               for part in template):
        command.append(prompt)
    return command


def _public_packet(packet: Mapping[str, object]) -> Dict[str, object]:
    return {str(key): value for key, value in packet.items()
            if not str(key).startswith("_")}


def run_workflow(*, stage: str, run_id: str, artifacts: Mapping[str, Path],
                 model_command: object, work_dir: Path, project_root: Path,
                 model: str = "", run_label: str = "current",
                 contract: Optional[Mapping[str, object]] = None,
                 max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
                 prompt_max_chars: int = DEFAULT_PROMPT_MAX_CHARS,
                 max_facts_per_skill: int = DEFAULT_MAX_FACTS_PER_SKILL,
                 max_total_facts: int = DEFAULT_MAX_TOTAL_FACTS,
                 model_timeout_seconds: int = 900) -> Dict[str, object]:
    """Own prepare -> model -> validate -> one retry -> fallback.

    The caller provides artifact references and an argv template.  This
    function owns review timing and never asks the controller to inspect the
    Fact Catalog or construct a retry prompt.
    """
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", run_label):
        raise ValueError("run_label contains unsupported characters")
    if model_timeout_seconds < 1:
        raise ValueError("model_timeout_seconds must be positive")
    work_dir = work_dir.expanduser().resolve()
    project_root = project_root.expanduser().resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    packet = prepare(
        stage=stage, run_id=run_id, artifacts=artifacts, contract=contract,
        max_artifact_bytes=max_artifact_bytes, prompt_max_chars=prompt_max_chars,
        max_facts_per_skill=max_facts_per_skill,
        max_total_facts=max_total_facts)
    packet_path = work_dir / f"stage_evidence_input_{run_label}.json"
    _write_json(packet_path, _public_packet(packet))
    attempts: List[Dict[str, object]] = []
    last_model: Dict[str, object] = {}
    last_review: Dict[str, object] = {}

    if packet.get("input_status") != "ready_for_model":
        # Missing, conflicting, stale, or unreadable source evidence is not a
        # summarization problem and cannot be repaired by spending a model call.
        last_review = validate_model_output(packet, {}, attempt=1)
        final_path = work_dir / f"stage_evidence_review_{run_label}_source_hold.json"
        _write_json(final_path, last_review)
        return {
            "schema_version": SCHEMA_VERSION,
            "workflow": WORKFLOW,
            "stage": stage,
            "run_id": run_id,
            "packet": _public_packet(packet),
            "review": last_review,
            "model": last_model,
            "model_attempts": attempts,
            "hard_gate": last_review.get("hard_gate", "hold"),
        }

    for attempt in (0, 1):
        if attempt == 0:
            prompt = render_prompt(packet)
        else:
            prompt = render_retry_prompt(packet, last_review)
        prompt_path = work_dir / f"evidence_model_prompt_{run_label}_attempt{attempt}.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        log_path = work_dir / f"evidence_model_{run_label}_attempt{attempt}.log"
        model_exit: Optional[int] = None
        transport_error: Optional[str] = None
        try:
            command = _model_command(
                model_command, prompt=prompt, prompt_file=prompt_path,
                project_root=project_root, work_dir=work_dir, model=model)
            completed = subprocess.run(
                command, cwd=project_root, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=model_timeout_seconds, check=False)
            model_exit = completed.returncode
            model_text = completed.stdout or ""
        except (OSError, subprocess.SubprocessError) as error:
            model_text = ""
            transport_error = f"{type(error).__name__}: {error}"
        log_path.write_text(
            model_text + (("\n[model transport] " + transport_error + "\n")
                          if transport_error else ""), encoding="utf-8")
        parse_error: Optional[str] = None
        try:
            model_output = _extract_json_object(model_text) if model_exit == 0 else {}
        except (ValueError, json.JSONDecodeError) as error:
            model_output = {}
            parse_error = str(error)
        model_path = work_dir / f"evidence_model_output_{run_label}_attempt{attempt}.json"
        _write_json(model_path, model_output)
        review = validate_model_output(packet, model_output, attempt=attempt)
        if model_exit not in (None, 0):
            review.setdefault("warnings", []).append({
                "kind": "evidence_model_failed", "exit": model_exit,
                "fallback_allowed": review.get("integrity_state") == INTEGRITY_PASS,
            })
        if transport_error:
            review.setdefault("warnings", []).append({
                "kind": "evidence_model_transport_failed", "detail": transport_error,
                "fallback_allowed": review.get("integrity_state") == INTEGRITY_PASS,
            })
        if parse_error:
            review.setdefault("warnings", []).append({
                "kind": "evidence_model_parse_failed", "detail": parse_error})
        review_path = work_dir / f"stage_evidence_review_{run_label}_attempt{attempt}.json"
        _write_json(review_path, review)
        attempts.append({
            "attempt": attempt, "model_exit": model_exit,
            "transport_error": transport_error, "parse_error": parse_error,
            "prompt": prompt_path.name, "model_log": log_path.name,
            "model_output": model_path.name, "review": review_path.name,
        })
        last_model = model_output
        last_review = review
        if review.get("integrity_state") != INTEGRITY_PASS:
            break
        if review.get("summary_state") != "retry_required":
            break

    return {
        "schema_version": SCHEMA_VERSION,
        "workflow": WORKFLOW,
        "stage": stage,
        "run_id": run_id,
        "packet": _public_packet(packet),
        "review": last_review,
        "model": last_model,
        "model_attempts": attempts,
        "hard_gate": last_review.get("hard_gate", "hold"),
    }


def _parse_artifacts(values: Iterable[str]) -> Dict[str, Path]:
    artifacts: Dict[str, Path] = {}
    for value in values:
        role, separator, path = value.partition("=")
        if not separator or not role.strip() or not path.strip():
            raise ValueError("--artifact must use role=path")
        role_name = role.strip()
        if role_name in DISALLOWED_ROLES:
            raise ValueError(f"artifact role {role_name!r} is not allowed; use a current-stage scoped role such as progress_delta")
        if role_name in artifacts:
            raise ValueError(f"duplicate artifact role: {role.strip()}")
        artifacts[role_name] = Path(path)
    return artifacts


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--stage", required=True)
    prepare_parser.add_argument("--run-id", required=True)
    prepare_parser.add_argument("--artifact", action="append", default=[])
    prepare_parser.add_argument("--contract", type=Path)
    prepare_parser.add_argument("--max-artifact-bytes", type=int, default=DEFAULT_MAX_ARTIFACT_BYTES)
    prepare_parser.add_argument("--prompt-max-chars", type=int, default=DEFAULT_PROMPT_MAX_CHARS)
    prepare_parser.add_argument("--max-facts-per-skill", type=int, default=DEFAULT_MAX_FACTS_PER_SKILL)
    prepare_parser.add_argument("--max-total-facts", type=int, default=DEFAULT_MAX_TOTAL_FACTS)
    prepare_parser.add_argument("--output", type=Path)
    prepare_parser.add_argument("--prompt-output", type=Path)
    validate_parser = sub.add_parser("validate")
    validate_parser.add_argument("--input", required=True, type=Path)
    validate_parser.add_argument("--model-output", required=True, type=Path)
    validate_parser.add_argument("--attempt", type=int, default=0)
    validate_parser.add_argument("--output", type=Path)
    retry_parser = sub.add_parser("render-retry")
    retry_parser.add_argument("--input", required=True, type=Path)
    retry_parser.add_argument("--review", required=True, type=Path)
    retry_parser.add_argument("--output", type=Path)
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--stage", required=True)
    run_parser.add_argument("--run-id", required=True)
    run_parser.add_argument("--artifact", action="append", default=[])
    run_parser.add_argument("--contract", type=Path)
    run_parser.add_argument("--work-dir", required=True, type=Path)
    run_parser.add_argument("--run-label", default="current")
    run_parser.add_argument("--project-root", required=True, type=Path)
    run_parser.add_argument("--model-command-json", required=True)
    run_parser.add_argument("--model", default="")
    run_parser.add_argument("--model-timeout-seconds", type=int, default=900)
    run_parser.add_argument("--max-artifact-bytes", type=int, default=DEFAULT_MAX_ARTIFACT_BYTES)
    run_parser.add_argument("--prompt-max-chars", type=int, default=DEFAULT_PROMPT_MAX_CHARS)
    run_parser.add_argument("--max-facts-per-skill", type=int, default=DEFAULT_MAX_FACTS_PER_SKILL)
    run_parser.add_argument("--max-total-facts", type=int, default=DEFAULT_MAX_TOTAL_FACTS)
    run_parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            custom = json.loads(args.contract.read_text(encoding="utf-8")) if args.contract else None
            if custom is not None and not isinstance(custom, dict):
                raise ValueError("--contract must decode to one JSON object")
            model_command = json.loads(args.model_command_json)
            if not isinstance(model_command, list) or not model_command:
                raise ValueError("--model-command-json must decode to one non-empty argv list")
            result = run_workflow(
                stage=args.stage, run_id=args.run_id,
                artifacts=_parse_artifacts(args.artifact), contract=custom,
                model_command=model_command, work_dir=args.work_dir,
                project_root=args.project_root, model=args.model,
                run_label=args.run_label,
                model_timeout_seconds=args.model_timeout_seconds,
                max_artifact_bytes=args.max_artifact_bytes,
                prompt_max_chars=args.prompt_max_chars,
                max_facts_per_skill=args.max_facts_per_skill,
                max_total_facts=args.max_total_facts)
            if args.output:
                _write_json(args.output, result)
            else:
                sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            # A hold is a valid evidence classification, not a transport error.
            return 0
        if args.command == "prepare":
            custom = json.loads(args.contract.read_text(encoding="utf-8")) if args.contract else None
            if custom is not None and not isinstance(custom, dict):
                raise ValueError("--contract must decode to one JSON object")
            packet = prepare(stage=args.stage, run_id=args.run_id, artifacts=_parse_artifacts(args.artifact),
                             contract=custom, max_artifact_bytes=args.max_artifact_bytes,
                             prompt_max_chars=args.prompt_max_chars,
                             max_facts_per_skill=args.max_facts_per_skill,
                             max_total_facts=args.max_total_facts)
            if args.prompt_output:
                args.prompt_output.parent.mkdir(parents=True, exist_ok=True)
                args.prompt_output.write_text(render_prompt(packet), encoding="utf-8")
            packet_for_output = {key: value for key, value in packet.items()
                                 if not str(key).startswith("_")}
            if args.output:
                _write_json(args.output, packet_for_output)
            else:
                sys.stdout.write(json.dumps(packet_for_output, ensure_ascii=False, indent=2) + "\n")
            return 0
        packet = json.loads(args.input.read_text(encoding="utf-8"))
        if args.command == "render-retry":
            review = json.loads(args.review.read_text(encoding="utf-8"))
            prompt = render_retry_prompt(packet, review)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(prompt, encoding="utf-8")
            else:
                sys.stdout.write(prompt)
            return 0
        model = json.loads(args.model_output.read_text(encoding="utf-8"))
        result = validate_model_output(packet, model, attempt=args.attempt)
        if args.output:
            _write_json(args.output, result)
        else:
            sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return 0 if result["hard_gate"] == "pass" else 2
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
