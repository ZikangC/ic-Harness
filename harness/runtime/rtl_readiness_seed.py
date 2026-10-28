"""Controller-only import of a selected source closure into a fresh isolated run."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import shutil
import tempfile


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _reviewed_work(out, receipt, adopted, removed=()):
    """Recognize formal repair state without relabeling the imported source pins."""
    from harness.runtime.rtl_worker import (
        closure_inventory, integrity_guards, reviews, review_status,
    )
    manifest = out / 'manifest.json'
    if not manifest.is_file() or json.loads(manifest.read_text()).get('settings', {}).get('readiness', {}).get('rtl_review') is not True:
        return {}, False
    history = reviews(out)
    if not history:
        return {}, False
    integrity_guards(out)
    # The formal loop must start from this imported/adopted closure, not an
    # unrelated review tree merely present in the run.
    first = history[0]['work_inventory']
    initial = {rel: entry['sha256'] for rel, entry in receipt['selected_files'].items()}
    initial.update(adopted)
    for rel in removed:
        if rel in first:
            raise ValueError('formal review does not descend from the adopted deletion: ' + rel)
        initial.pop(rel, None)
    for rel, expected_sha in initial.items():
        frozen = first.get(rel, {})
        if frozen.get('type') != 'file' or frozen.get('sha256') != expected_sha:
            raise ValueError('formal review does not descend from the imported seed: ' + rel)
    latest = history[-1]
    review_status(out, latest)
    repair = latest.get('repair') or {}
    if repair.get('status') == 'active':
        recorded = out / 'control/rtl-audit' / (latest['review'] + '.repair.json')
        actor = json.loads(recorded.read_text()) if recorded.is_file() else {}
        decision = latest.get('decision') or {}
        if (decision.get('verdict') != 'revise' or actor.get('repair_actor') != 'rtl-implementation-readiness'
                or actor.get('candidate_sha256') != latest['candidate_sha256']
                or actor.get('decision_report_sha256') != decision.get('report_sha256')):
            # A crash before the repair actor starts may leave unchanged work;
            # ordinary original/adoption pin checks still apply in that case.
            return {}, False
        return {}, True
    expected = repair.get('work_inventory') if repair.get('status') == 'completed' else latest['work_inventory']
    if closure_inventory(out / 'work') != expected:
        raise ValueError('work changed outside the recorded formal review/repair state')
    return expected, False


def seed(out, req):
    # This is intentionally absent from the model tool bridge allowlist.
    from harness.runtime.rtl_worker import active_jobs, digest, save
    if req.get('validate_only') is True:
        receipt = json.loads((out / 'control/readiness-seed.json').read_text())
        request = {key: receipt[key] for key in ('source_work', 'include_paths', 'provenance')}
        if digest(request) != receipt['request_identity'] or digest(receipt['selected_files']) != receipt['selected_closure_sha256']:
            raise ValueError('imported seed receipt changed')
        adopted = {}
        removed = set()
        frozen_assets = set()
        adoption_paths = (out / 'readiness').glob('campaign-*.adoption.json')
        for path in sorted(adoption_paths, key=lambda p: int(p.name[len('campaign-'):-len('.adoption.json')])):
            value = json.loads(path.read_text())
            if value.get('adopted') is True:
                operations = value.get('operations', {})
                for rel in operations.get('delete', []):
                    removed.add(rel)
                    adopted.pop(rel, None)
                for rel, expected_sha in value['changed_files'].items():
                    adopted[rel] = expected_sha
                    removed.discard(rel)
                frozen_assets.update(value.get('verification', {}).get('frozen_assets', []))
        reviewed, active_repair = _reviewed_work(out, receipt, adopted, removed)
        from harness.runtime.rtl_readiness import regional_policy, owned
        build_files = regional_policy(out)['build_files'] if (out / 'manifest.json').is_file() else []
        expected_files = {rel: entry['sha256'] for rel, entry in receipt['selected_files'].items()}
        expected_files.update(adopted)
        for rel in sorted(set(expected_files) | removed):
            path = owned(out / 'work', rel)
            expected = None if rel in removed else expected_files[rel]
            editable = (rel.startswith('rtl/') or rel in build_files) and rel not in frozen_assets
            mutable = editable and active_repair
            if editable and reviewed:
                expected = reviewed.get(rel, {}).get('sha256')
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise ValueError('imported seed or adopted source changed: ' + rel)
            if mutable:
                continue
            if expected is None:
                if path.exists():
                    raise ValueError('imported seed or adopted source changed: ' + rel)
                continue
            if not path.is_file() or _sha(path) != expected:
                raise ValueError('imported seed or adopted source changed: ' + rel)
        return receipt
    source = Path(req['source_work'])
    if not source.is_absolute() or source.is_symlink() or not source.is_dir():
        raise ValueError('seed source must be an existing absolute directory')
    source = source.resolve()
    paths = req.get('include_paths')
    if not isinstance(paths, list) or not paths or len(paths) != len(set(paths)):
        raise ValueError('seed requires an explicit unique file list')
    provenance = req.get('provenance', {})
    if not isinstance(provenance, dict):
        raise ValueError('seed provenance must be an object')
    request = {'source_work': str(source), 'include_paths': sorted(paths), 'provenance': provenance}
    identity = digest(request)
    receipt_path = out / 'control/readiness-seed.json'
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text())
        if receipt['request_identity'] != identity:
            raise ValueError('run already imported a different seed')
        return receipt
    work = out / 'work'
    if active_jobs(out) or any(work.iterdir()) or any((out / 'rtl').glob('reviews/review-*.json')):
        raise ValueError('seed import requires a fresh idle empty work tree')
    expected = provenance.get('selected_sha256', {})
    if expected and set(expected) != set(paths):
        raise ValueError('seed expected fingerprint set differs from selected files')
    files = {}
    for rel in paths:
        if not isinstance(rel, str):
            raise ValueError('unsafe seed relative path')
        raw = Path(rel)
        if raw.is_absolute() or '..' in raw.parts or '\\' in rel or not raw.parts:
            raise ValueError('unsafe seed relative path')
        path = source / raw
        if not path.resolve().is_relative_to(source) or any(p.is_symlink() for p in (path, *path.parents) if p != source and p.is_relative_to(source)):
            raise ValueError('seed links are unsupported')
        if not path.is_file():
            raise ValueError('seed selected input is not a regular file: ' + rel)
        entry = {'sha256': _sha(path), 'size': path.stat().st_size, 'mode': path.stat().st_mode & 0o777}
        if expected and expected[rel] != entry['sha256']:
            raise ValueError('seed source differs from expected fingerprint: ' + rel)
        files[rel] = entry
    # Validate all paths before copying any input; never modify the source.
    staging = Path(tempfile.mkdtemp(prefix='.readiness-seed-', dir=out))
    try:
        for rel, entry in files.items():
            dest = staging / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / rel, dest)
            if _sha(dest) != entry['sha256'] or _sha(source / rel) != entry['sha256']:
                raise ValueError('seed changed during import: ' + rel)
        staging.replace(work)  # atomic publication over the fresh empty directory
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    receipt = {**request, 'request_identity': identity, 'selected_files': files,
               'selected_closure_sha256': digest(files),
               'note': 'Selected closure identity is distinct from the historical complete candidate identity.'}
    save(receipt_path, receipt)
    return receipt
