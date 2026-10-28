"""Clone sealed sources and directly reference intact completed receipts."""
from pathlib import Path
import shutil
import tempfile

from core import (ReadinessError, digest, label, load_variant, load_workspace,
                  read_json, save_json, sha, under, variant_tree)


def _supported(source, request, result):
    config = request.get('config', {})
    if config.get('kind') == 'yosys' or (config.get('kind') == 'command' and config.get('purpose') == 'measurement'):
        return
    functional = (config.get('kind') == 'equiv' or
                  (config.get('kind') == 'command' and config.get('purpose') in ('regression', 'equivalence')))
    if (functional and result.get('verification_passed') is True
            and request.get('baseline_identity') == load_workspace(source)['baseline_identity']):
        return
    raise ReadinessError('Receipt reference requires a completed measurement or a successful paired functional receipt')


def _source(source, job_id, *, flatten=False):
    from comparison import receipt
    source = Path(source).resolve(); job = under(source, Path('jobs') / label(job_id))
    if (job / 'reference.json').exists():
        if flatten:
            # Recheck the existing direct reference before pinning its origin.
            # The new workspace does not retain a dependency on this hop.
            reference = read_json(under(job, 'reference.json'))
            referenced_receipt(source, job_id)
            if read_json(under(job, 'reference.json')) != reference:
                raise ReadinessError('Source reference changed while flattening')
            original = _source(reference['source_workspace'], job_id)
            if original[3] != reference['source_sha256']:
                raise ReadinessError('Referenced source request or result changed while flattening')
            return original
        raise ReadinessError('Receipt reference chains are unavailable; select a direct completed source receipt')
    if any(not under(job, name).is_file() for name in ('request.json', 'result.json')):
        raise ReadinessError('Receipt references require a completed source request and result: ' + job_id)
    pins = {name: sha(under(job, name)) for name in ('request.json', 'result.json')}
    request, result = receipt(source, job_id)
    _supported(source, request, result)
    if any(sha(under(job, name)) != expected for name, expected in pins.items()):
        raise ReadinessError('Source receipt changed during validation')
    return source, request, result, pins


def referenced_receipt(workspace, job_id):
    """Revalidate the original bytes in place; never rewrite historical identities."""
    workspace = Path(workspace).resolve(); job = under(workspace, Path('jobs') / label(job_id))
    if (job / 'request.json').exists() or (job / 'result.json').exists():
        raise ReadinessError('Receipt reference conflicts with a local job')
    reference = read_json(under(job, 'reference.json'))
    unsigned = {key: value for key, value in reference.items() if key != 'identity'}
    if reference.get('kind') != 'readiness_receipt_reference_v1' or digest(unsigned) != reference.get('identity'):
        raise ReadinessError('Receipt reference identity changed')
    source_name = reference.get('source_workspace')
    if not isinstance(source_name, str) or not Path(source_name).is_absolute() or Path(source_name).resolve() == workspace:
        raise ReadinessError('Receipt reference needs a distinct absolute source workspace')
    if reference.get('source_job_id') != job_id:
        raise ReadinessError('Receipt references retain their original job ID; aliases cannot create repeats')
    source, request, result, pins = _source(source_name, job_id)
    if pins != reference.get('source_sha256'):
        raise ReadinessError('Referenced source request or result changed')
    ws = load_workspace(workspace)
    for key in ('workspace_id', 'variant', 'variant_identity', 'baseline_identity', 'measurement_key'):
        if reference.get(key) != request.get(key):
            raise ReadinessError('Referenced receipt binding changed: ' + key)
    if ws['api_version'] == 3:
        for key in ('build', 'baseline_build'):
            if reference.get(key) != request.get(key):
                raise ReadinessError('Referenced receipt build binding changed: ' + key)
        from experiments import _verify_request_builds
        _verify_request_builds(workspace, request)
    if (request['workspace_id'] != ws['workspace_id']
            or load_variant(workspace, 'baseline')['identity'] != ws['baseline_identity']
            or (request.get('baseline_identity') is not None and request['baseline_identity'] != ws['baseline_identity'])
            or load_variant(workspace, request['variant'])['identity'] != request['variant_identity']):
        raise ReadinessError('Referenced receipt does not match the target sealed sources')
    return request, result


def clone_workspace(source, destination, jobs, variants=()):
    """Copy only immutable input trees; old raw evidence stays at its source."""
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.exists() or source == destination or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ReadinessError('Clone needs a new workspace outside the source workspace')
    if not jobs or len(jobs) != len(set(jobs)):
        raise ReadinessError('Clone requires explicit unique completed receipt job IDs')
    ws = load_workspace(source); selected = {'baseline'} | {label(name) for name in variants}
    references = {}
    for job_id in jobs:
        origin, request, _, pins = _source(source, job_id, flatten=True)
        selected.add(request['variant'])
        reference = {'kind': 'readiness_receipt_reference_v1', 'source_workspace': str(origin),
                     'source_job_id': job_id, 'source_sha256': pins,
                     **{key: request.get(key) for key in ('workspace_id', 'variant', 'variant_identity',
                                                        'baseline_identity', 'measurement_key')}}
        if ws['api_version'] == 3:
            reference.update({key: request[key] for key in ('build', 'baseline_build')})
        reference['identity'] = digest(reference)
        references[job_id] = reference
    for name in selected:
        load_variant(source, name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.clone-', dir=destination.parent))
    try:
        shutil.copyfile(source / 'workspace.json', staging / 'workspace.json')
        for name in sorted(selected):
            target = staging / 'variants' / name; target.mkdir(parents=True)
            shutil.copyfile(under(source, Path('variants') / name / 'variant.json'), target / 'variant.json')
            shutil.copytree(variant_tree(source, name), target / 'tree')
            load_variant(staging, name)
        for job_id, reference in references.items():
            save_json(staging / 'jobs' / job_id / 'reference.json', reference)
            referenced_receipt(staging, job_id)
        if load_workspace(staging) != ws or load_workspace(source) != ws:
            raise ReadinessError('Workspace manifest changed while cloning')
        staging.replace(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {'workspace': str(destination), 'source_workspace': str(source), 'workspace_id': ws['workspace_id'],
            'baseline_identity': ws['baseline_identity'], 'variants': sorted(selected),
            'referenced_jobs': sorted(references), 'note': 'Original receipt identities and times retained; actual origin sources and functional coverage are revalidated.'}
