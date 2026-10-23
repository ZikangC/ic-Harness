#!/usr/bin/env python3
"""Single entry point for implemented harness stages."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=['architecture', 'rtl', 'readiness'])
    parser.add_argument('--config', type=Path, default=Path(__file__).resolve().parent / 'config/environment.toml')
    parser.add_argument('--run-id', help='new output directory name')
    parser.add_argument('--resume', help='existing run id; requires identical inputs, config and code')
    parser.add_argument('--audit-design', type=Path, help='diagnose an existing structured proposal with one architecture_md; never deliver or edit it')
    parser.add_argument('--feedback', type=Path, help='optional current-design review JSON; frozen with run, never overrides inputs')
    parser.add_argument('--validate-inputs', action='store_true', help='load/parse/fingerprint inputs without calling a model')
    parser.add_argument('--task', type=Path, help='RTL implementation task text; frozen with the server run')
    parser.add_argument('--audit-only', action='store_true', help='RTL: review an existing frozen run without repeating initial generation; requires --resume')
    parser.add_argument('--seed-work', help='Readiness: absolute server path of the read-only RTL seed; fresh runs only')
    parser.add_argument('--seed-file', action='append', default=[], help='Readiness: explicit relative seed file to import; repeat in source order')
    parser.add_argument('--seed-provenance', type=Path, help='Readiness: local JSON provenance of the imported seed')
    parser.add_argument('--diagnose-only', action='store_true', help='Readiness: investigate existing evidence in a read-only bounded session; never start experiments')
    parser.add_argument('--diagnosis-inputs', type=Path, help='Readiness: local JSON index of server evidence and objective job/resource observations')
    parser.add_argument('--diagnosis-event', choices=['rtl_review_passed', 'stage_completed', 'observation_budget', 'job_failed'], help='Readiness: observation that prompted investigation; does not diagnose an RTL fault')
    parser.add_argument('--allow-experiments', action='store_true', help='Readiness: permit an existing-evidence diagnosis to request bounded optimization after controller admission')
    args = parser.parse_args()
    if args.audit_only and args.stage != 'rtl':
        parser.error('--audit-only applies to RTL runs')
    if args.stage != 'readiness' and (args.seed_work or args.seed_file or args.seed_provenance):
        parser.error('seed options apply only to readiness runs')
    if args.stage != 'readiness' and (args.diagnose_only or args.diagnosis_inputs or args.diagnosis_event or args.allow_experiments):
        parser.error('diagnosis options apply only to readiness runs')
    if args.diagnose_only and args.allow_experiments:
        parser.error('--diagnose-only and --allow-experiments are mutually exclusive')
    if (args.diagnose_only or args.allow_experiments or args.diagnosis_event) and not args.diagnosis_inputs and not args.validate_inputs:
        parser.error('diagnosis requires --diagnosis-inputs')
    if args.diagnosis_inputs and not args.diagnosis_event:
        parser.error('--diagnosis-inputs requires --diagnosis-event')
    if args.stage == 'architecture':
        from harness.stages.architecture import run
    elif args.stage == 'rtl':
        from harness.stages.rtl import run
    else:
        from harness.stages.readiness import run
    return run(args)


if __name__ == '__main__':
    raise SystemExit(main())
