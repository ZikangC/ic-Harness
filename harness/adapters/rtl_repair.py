"""Explicit default RTL-stage repair policy; independent audit does not import it."""
from pathlib import Path

from harness.adapters.rtl_audit import (RepairHandler, RepairSession, _context_text,
                                        _delivery_task, _frozen_architecture)


def _prepare(context, remote, review):
    harness = Path(context['harness_dir'])
    skill = harness / 'skills/rtl-implementation/SKILL.md'
    guidance = harness / 'skills/rtl-implementation/references/repair.md'
    prompt = (skill.read_text()
              + '\nMODE: repair this run after independent review.\n'
              + _delivery_task(context)
              + _frozen_architecture(remote, review)
              + guidance.read_text()
              + '\nSERVER CONTEXT:\n' + _context_text(context, review)
              + '\nRead the exact review report at @run/' + review['decision']['report_path']
              + '. Diagnose and repair using actual server execution. '
              'You may read prior audit tests but must not change audit/candidate '
              'artifacts. Keep edits in work. Before resubmitting commands, inspect '
              'PROGRESS.md and persistent jobs. Wait for your jobs and preserve '
              'failed evidence. Do not change the source requirements or generic skill.')
    return RepairSession(prompt)


implementation_repair = RepairHandler('rtl-implementation', _prepare)
