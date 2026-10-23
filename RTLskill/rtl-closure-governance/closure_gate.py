#!/usr/bin/env python3
"""Stable controller-facing API for synthesis-to-STA closure evidence.

This module is deliberately transport-agnostic.  Invoke it wherever the
netlist and feedback packet are available; a remote controller may instead
run collection remotely and pass the resulting ``artifact_path`` locally.
"""
import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional


SCRIPTS = Path(__file__).resolve().parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from sta_closure_audit import apply_policy_context, classify, parse_feedback  # noqa: E402
from sta_closure_collect import collect  # noqa: E402


GATE_API_VERSION = 6


def _merge_artifact(values: Dict[str, str], artifact: Mapping[str, object]) -> None:
    for key, value in artifact.items():
        if isinstance(value, list):
            values[key] = ",".join(str(item) for item in value)
        elif value is not None:
            values[key] = str(value)


def _write_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def run(*, feedback_path: Optional[Path] = None,
        artifact_path: Optional[Path] = None,
        probe_path: Optional[Path] = None,
        netlist_path: Optional[Path] = None,
        top: Optional[str] = None,
        required_bodies: Iterable[str] = (),
        artifact_output: Optional[Path] = None,
        policy_context: Optional[Mapping[str, object]] = None) -> Dict[str, object]:
    """Return a stable closure audit without copying a netlist into memory.

    ``feedback_path`` is optional and must be produced deterministically by a
    measurement adapter, never by a planning model.  Without it, the function
    can still classify export/top/body failures; otherwise it returns
    ``not_attempted`` once those structural checks pass.
    """
    values: Dict[str, str] = {}
    if feedback_path is not None:
        values.update(parse_feedback(feedback_path.read_text(encoding="utf-8", errors="replace")))
    if artifact_path is not None:
        _merge_artifact(values, json.loads(artifact_path.read_text(encoding="utf-8")))
    if probe_path is not None:
        probe = json.loads(probe_path.read_text(encoding="utf-8"))
        status = probe.get("status")
        if status not in ("passed", "failed"):
            raise ValueError("probe_path must contain status=passed or status=failed")
        values["tool_probe_status"] = status
        values["tool_probe_failed"] = ",".join(str(item) for item in probe.get("failed_probes", []))
    if netlist_path is not None:
        if not top:
            raise ValueError("top is required when netlist_path is supplied")
        artifact = collect(netlist_path, top, required_bodies)
        _merge_artifact(values, artifact)
        if artifact_output is not None:
            _write_json(artifact_output, artifact)
    audit = apply_policy_context(classify(values), policy_context)
    audit["gate_api_version"] = GATE_API_VERSION
    return audit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feedback", type=Path, help="optional deterministic STA feedback packet")
    parser.add_argument("--artifact", type=Path, help="artifact JSON collected on another execution host")
    parser.add_argument("--probe", type=Path, help="tool probe JSON from scripts/tool_probe.py")
    parser.add_argument("--netlist", type=Path, help="mapped/closure netlist available on this host")
    parser.add_argument("--top", help="configured top; required with --netlist")
    parser.add_argument("--required-body", action="append", default=[], metavar="MODULE")
    parser.add_argument("--artifact-output", type=Path, help="where to write a newly collected artifact")
    parser.add_argument("--output", type=Path, help="where to write the audit JSON")
    parser.add_argument("--policy-json", help="controller-owned resource/candidate facts as one JSON object")
    args = parser.parse_args()
    if args.netlist is not None and not args.netlist.is_file():
        parser.error("--netlist must name an existing regular file")
    if args.artifact is not None and not args.artifact.is_file():
        parser.error("--artifact must name an existing regular file")
    if args.probe is not None and not args.probe.is_file():
        parser.error("--probe must name an existing regular file")
    if args.feedback is not None and not args.feedback.is_file():
        parser.error("--feedback must name an existing regular file")
    try:
        policy_context = json.loads(args.policy_json) if args.policy_json else None
        if policy_context is not None and not isinstance(policy_context, dict):
            raise ValueError("--policy-json must decode to one JSON object")
        audit = run(feedback_path=args.feedback, artifact_path=args.artifact, probe_path=args.probe,
                    netlist_path=args.netlist, top=args.top,
                    required_bodies=args.required_body,
                    artifact_output=args.artifact_output,
                    policy_context=policy_context)
    except ValueError as error:
        parser.error(str(error))
    if args.output is not None:
        _write_json(args.output, audit)
    else:
        sys.stdout.write(json.dumps(audit, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
