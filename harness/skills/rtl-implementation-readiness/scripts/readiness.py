#!/usr/bin/env python3
"""Standalone, bounded RTL synthesis-cost investigation."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from core import ReadinessError, create_variant, init_workspace, read_json, save_json


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)
    a = sub.add_parser("init", help="Freeze the explicitly selected RTL input closure")
    a.add_argument("--project", required=True, type=Path)
    a.add_argument("--workspace", required=True, type=Path)
    a.add_argument("--top", required=True)
    a.add_argument("--source", action="append", required=True, dest="sources")
    a.add_argument("--include", action="append", default=[], dest="include_dirs")
    a.add_argument("--asset", action="append", default=[], dest="assets")
    a.add_argument("--allow-edit", action="append", default=None, dest="allowed_paths")
    a = sub.add_parser("clone-workspace", help="Clone sealed inputs and explicitly reference completed measurements or paired functional checks")
    a.add_argument("source", type=Path)
    a.add_argument("--workspace", type=Path, required=True)
    a.add_argument("--job", action="append", required=True, dest="jobs")
    a.add_argument("--variant", action="append", default=[], dest="variants")
    a = sub.add_parser("analyze", help="Locate source risks and owned expensive experiments")
    a.add_argument("workspace", type=Path)
    a.add_argument("--out", type=Path)
    a = sub.add_parser("variant", help="Seal an RTL overlay as an independent candidate")
    a.add_argument("workspace", type=Path)
    a.add_argument("name")
    a.add_argument("--overlay", type=Path, required=True)
    a = sub.add_parser("run", help="Submit or query the same persistent experiment identity")
    a.add_argument("workspace", type=Path)
    a.add_argument("--variant", default="baseline")
    a.add_argument("--config", type=Path, required=True)
    a.add_argument("--job-id")
    a.add_argument("--foreground", action="store_true")
    a = sub.add_parser("status", help="Observe only: never restarts or resumes a missing worker")
    a.add_argument("workspace", type=Path)
    a.add_argument("job_id")
    for action in ("batch", "verify-batch"):
        a = sub.add_parser(action, help="Run or read-only verify one finite claimed experiment plan")
        a.add_argument("workspace", type=Path)
        a.add_argument("--plan", required=True, type=Path)
        a.add_argument("--batch-id", required=True)
        a.add_argument("--tool-seconds", required=True, type=int)
    for action, help_text in (("compare", "Compare compatible repeated measurements and functional receipts"),
                              ("finalize", "Atomically compare, export and publish a source-bound finalization")):
        a = sub.add_parser(action, help=help_text)
        a.add_argument("workspace", type=Path)
        a.add_argument("--variant", required=True)
        a.add_argument("--baseline-job", action="append", required=True)
        a.add_argument("--candidate-job", action="append", required=True)
        a.add_argument("--verification-job", action="append", default=[])
        a.add_argument("--metric", choices=["elapsed_seconds", "peak_rss_bytes"], default="elapsed_seconds")
        a.add_argument("--min-ratio", type=float, default=1.2)
        a.add_argument("--min-repeats", type=int, default=3)
        a.add_argument("--out", type=Path, required=True)
    a = sub.add_parser("verify-finalization", help="Read-only fresh verification of a finalization and its evidence")
    a.add_argument("workspace", type=Path)
    a.add_argument("--delivery", type=Path, required=True)
    a = sub.add_parser("verify", help="Revalidate comparison, sources and export without applying changes")
    a.add_argument("workspace", type=Path)
    a.add_argument("--comparison", type=Path, required=True)
    a.add_argument("--delivery", type=Path, required=True)
    a = sub.add_parser("export", help="Deliver patch plus evidence; original RTL remains untouched")
    a.add_argument("workspace", type=Path)
    a.add_argument("--comparison", type=Path, required=True)
    a.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    try:
        if args.action == "init":
            result = init_workspace(args.project, args.workspace, args.sources, args.top,
                                    args.include_dirs, args.assets, args.allowed_paths)
        elif args.action == "clone-workspace":
            from receipt_reuse import clone_workspace
            result = clone_workspace(args.source, args.workspace, args.jobs, args.variants)
        elif args.action == "analyze":
            from analysis import analyze
            result = analyze(args.workspace)
            if args.out:
                _owned_output(args.workspace, args.out)
                save_json(args.out, result)
        elif args.action == "variant":
            result = create_variant(args.workspace, args.name, args.overlay)
        elif args.action == "run":
            from experiments import submit
            result = submit(args.workspace, variant=args.variant, config=read_json(args.config),
                            job_id=args.job_id, background=not args.foreground)
        elif args.action == "status":
            from experiments import status
            result = status(args.workspace, args.job_id)
        elif args.action in ("batch", "verify-batch"):
            from batches import batch, verify_batch
            operation = batch if args.action == "batch" else verify_batch
            result = operation(args.workspace, plan=args.plan, batch_id=args.batch_id,
                               tool_seconds=args.tool_seconds)
        elif args.action == "compare":
            from comparison import compare
            result = compare(args.workspace, args.variant, args.baseline_job, args.candidate_job,
                             args.verification_job, metric=args.metric, min_ratio=args.min_ratio,
                             min_repeats=args.min_repeats)
            _owned_output(args.workspace, args.out)
            save_json(args.out, result)
        elif args.action == "finalize":
            from comparison import finalize
            result = finalize(args.workspace, args.variant, args.baseline_job, args.candidate_job,
                              args.verification_job, destination=args.out, metric=args.metric,
                              min_ratio=args.min_ratio, min_repeats=args.min_repeats)
        elif args.action == "verify-finalization":
            from comparison import verify_finalization
            result = verify_finalization(args.workspace, args.delivery)
        elif args.action == "verify":
            from comparison import verify
            result = verify(args.workspace, args.comparison, args.delivery)
        else:
            from comparison import export
            result = export(args.workspace, read_json(args.comparison), args.out)
        print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    except (ReadinessError, OSError, KeyError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


def _owned_output(workspace, out):
    root, path = Path(workspace).resolve(), Path(out).resolve()
    if not path.is_relative_to(root) or path.exists() or path.relative_to(root).parts[0] in ("variants", "jobs") or path == root / "workspace.json":
        raise ReadinessError("Use a new report path within this workspace, outside variants/jobs")


if __name__ == "__main__":
    raise SystemExit(main())
