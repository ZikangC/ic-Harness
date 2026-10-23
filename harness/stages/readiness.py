"""Readiness-only entry: import an isolated seed and investigate without generation."""
from harness.stages.rtl import run as run_rtl_controller


def run(args):
    diagnose = bool(getattr(args, 'diagnose_only', False) or getattr(args, 'diagnosis_inputs', None))
    if getattr(args, 'diagnose_only', False) and getattr(args, 'allow_experiments', False):
        raise ValueError('--diagnose-only and --allow-experiments are mutually exclusive')
    if diagnose and not getattr(args, 'validate_inputs', False):
        if not getattr(args, 'diagnosis_inputs', None) or not getattr(args, 'diagnosis_event', None):
            raise ValueError('diagnosis requires --diagnosis-inputs and --diagnosis-event')
    if getattr(args, 'allow_experiments', False) and not diagnose:
        raise ValueError('--allow-experiments requires a diagnosis evidence index')
    seed = (getattr(args, 'seed_work', None), getattr(args, 'seed_file', None),
            getattr(args, 'seed_provenance', None))
    if args.resume:
        if any(seed):
            raise ValueError('readiness resume retains its frozen seed; omit all seed options')
    elif not getattr(args, 'validate_inputs', False) and not all(seed):
        raise ValueError('fresh readiness requires --seed-work, one or more --seed-file, and --seed-provenance')
    return run_rtl_controller(args, readiness_only=True, diagnosis_only=diagnose)
