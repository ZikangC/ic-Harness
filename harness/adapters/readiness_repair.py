"""Caller-selected readiness repair; the audit skill never selects its editor."""
from pathlib import Path, PurePosixPath

from harness.adapters.rtl_audit import (
    RepairHandler, RepairSession, _context_text, _delivery_task, _frozen_architecture,
)


def prepare(context, remote, review):
    skill = Path(context['harness_dir']) / 'skills/rtl-implementation-readiness/SKILL.md'
    if not skill.is_file():
        raise FileNotFoundError('selected readiness repair skill is unavailable')
    decision = review.get('decision') or {}
    if decision.get('verdict') != 'revise':
        raise ValueError('readiness source repair requires an independent revise decision')
    candidate = str(PurePosixPath(context['remote_run_dir']) / review['candidate_scope'])
    workspace = str(PurePosixPath(context['remote_run_dir']) / 'work/readiness-repair' / review['review'])
    prompt = (skill.read_text()
              + '\nMODE: repair a readiness-generated candidate after independent RTL review.\n'
              + _delivery_task(context) + _frozen_architecture(remote, review)
              + '\nSERVER CONTEXT:\n' + _context_text(context, review)
              + '\nRead the exact independent report at @run/' + decision['report_path']
              + '. The caller selected this readiness skill to investigate and repair its candidate. '
              'The frozen @candidate/ and all prior audit/candidate/evidence artifacts remain read-only. '
              'Your writable scope is work. Use ' + workspace
              + ' for new experiment overlays, configurations and reports, and ' + candidate
              + ' as the frozen source baseline. Keep original relative RTL paths in overlays. '
              'Inspect existing progress and jobs before acting; preserve unsuccessful evidence. '
              'Make only the source changes required to resolve the reported findings. '
              'Use bounded experiments and affected functional checks, with exact baseline/candidate '
              'source and tool/configuration identities. Do not run full-design synthesis. '
              'Preserve architectural commitments, source requirements and acceptance criteria. '
              'If those commitments prevent repair, explain the concrete issue and retain the evidence. '
              'Do not change generic skills, project synthesis scripts or audit-owned tests. '
              'After checking a proposed repair, apply its changed original RTL files to work/rtl/; '
              'a patch left only in an experiment overlay is not a returned implementation. '
              'Record changes, actual checks and remaining obligations in work/PROGRESS.md. '
              'Wait for persistent jobs and end this session. The caller freezes the returned work '
              'and a fresh independent RTL audit decides whether it resolves the findings.')
    return RepairSession(prompt, context_updates={
        'repair_skill': 'rtl-implementation-readiness',
    })


readiness_repair = RepairHandler('rtl-implementation-readiness', prepare)
