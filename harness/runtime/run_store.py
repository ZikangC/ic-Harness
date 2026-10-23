"""Immutable inputs, run identity, versioned evidence, locks and recovery."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tomllib
from harness.runtime.input_bundle import load_input_bundle, original_sources

def dump(path, data):
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(path)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def feedback_revision_seed(feedback, sources):
    """Check optional revision transport; source replay remains separate."""
    if not isinstance(feedback, dict):return None
    expected = {name: digest(text.encode()) for name,text in original_sources(sources).items()}
    if 'input_sha256' in feedback and feedback['input_sha256'] != expected:
        raise ValueError('feedback fingerprint does not match current inputs')
    if 'native_revision_seed' not in feedback:return None
    seed = feedback['native_revision_seed']
    if not isinstance(seed,dict) or set(seed)!={'proposal','requirement_declaration','input_sha256'}:
        raise ValueError('native_revision_seed requires exactly proposal, requirement_declaration and input_sha256')
    if feedback.get('input_sha256') != expected or seed['input_sha256'] != feedback['input_sha256']:
        raise ValueError('native revision seed fingerprints must match top-level feedback and current inputs')
    proposal = seed['proposal']
    if (not isinstance(proposal,dict) or not isinstance(proposal.get('requirements'),dict)
            or not isinstance(proposal.get('work_model'),dict) or not isinstance(proposal.get('candidates'),list)
            or not proposal['candidates'] or not isinstance(proposal.get('selected'),str)
            or not isinstance(proposal.get('architecture_md'),str) or not proposal['architecture_md'].strip()
            or any(key.endswith('_md') and key!='architecture_md' for key in proposal)):
        raise ValueError('native revision seed needs a complete proposal with requirements, work_model, candidates, selected and one architecture_md')
    if not isinstance(seed['requirement_declaration'],dict):
        raise ValueError('native revision seed needs its raw requirement_declaration object')
    return seed


def load(args):
    harness = Path(__file__).resolve().parents[1]
    config_path = args.config.resolve()
    env = tomllib.loads(config_path.read_text())
    root = (config_path.parent / env['project']['root']).resolve()
    models_path = config_path.parent / 'models.toml'
    settings_path = config_path.parent / 'architecture.toml'
    models = tomllib.loads(models_path.read_text())
    settings = tomllib.loads(settings_path.read_text())
    verify_skills(harness)
    sources,files,bundle_check = load_input_bundle(root / env['project']['input_dir'])
    for p in sorted(harness.rglob('*')):
        if p.is_file() and (p.suffix in ('.py', '.js', '.toml', '.md') or p == harness/'skills/manifest.json') and '__pycache__' not in p.parts:
            files['harness/' + str(p.relative_to(harness))] = p.read_bytes()
    if args.feedback:
        feedback = json.loads(args.feedback.resolve().read_text())
        feedback_revision_seed(feedback,sources)
        files['feedback.json'] = (json.dumps(feedback, ensure_ascii=False, indent=2) + '\n').encode()
    if getattr(args,'audit_design',None):
        proposal=args.audit_design.resolve()
        data=json.loads(proposal.read_text())
        if not isinstance(data,dict) or not isinstance(data.get('architecture_md'),str) or not data['architecture_md'].strip() or any(k.endswith('_md') and k!='architecture_md' for k in data):
            raise ValueError('audit proposal must contain exactly one Markdown field: architecture_md')
        # A known originating manifest, if present, must refer to these inputs.
        origin=proposal.parent/'manifest.json'
        if origin.exists():
            prior=json.loads(origin.read_text()).get('sha256',{})
            originating_sources=original_sources({name[len('input/'):]:sha for name,sha in prior.items() if name.startswith('input/')})
            if originating_sources!={name:digest(text.encode()) for name,text in sources.items()}:
                raise ValueError('audit proposal originating inputs differ from current input')
        files['audit/design.json']=proposal.read_bytes()
    for p in (config_path, models_path, settings_path):
        files['effective_config/' + p.name] = p.read_bytes()
    hashes = {name: digest(data) for name, data in files.items()}
    input_check = {'ok': True, 'input_sha256': {name: hashes['input/' + name] for name in sources},
                   **bundle_check, 'reference_execution': 'not_run',
                   'config_resolved_root': str(root), 'config_resolved_output': str(root / env['project']['output_dir'])}
    return root, models, settings, sources, files, hashes, input_check


def open_run(args, root, models, settings, files, hashes, input_check):
    if args.resume and args.run_id:
        raise ValueError('use either --resume or --run-id')
    run_id = args.resume or args.run_id or 'architecture--' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', run_id) or run_id in ('.', '..'):
        raise ValueError('invalid run id')
    out = Path(input_check['config_resolved_output']) / run_id
    if args.resume:
        previous = json.loads((out / 'manifest.json').read_text())
        if previous['sha256'] != hashes:
            raise ValueError('resume refused: inputs/config/code/skill changed; create a new run')
        for name, expected in hashes.items():
            snapshot = out / 'snapshot' / name
            if not snapshot.is_file() or digest(snapshot.read_bytes()) != expected:
                raise ValueError('resume refused: snapshot changed: ' + name)
        index = out / 'evidence.json'
        if index.exists():
            for name, expected in json.loads(index.read_text())['sha256'].items():
                evidence_path = out / name
                if not evidence_path.is_file() or digest(evidence_path.read_bytes()) != expected:
                    raise ValueError('resume refused: recorded evidence changed: ' + name)
    else:
        out.mkdir(parents=True, exist_ok=False)
        for name, data in files.items():
            p = out / 'snapshot' / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        dump(out / 'manifest.json', {'stage': 'architecture', 'created_utc': datetime.now(timezone.utc).isoformat(),
             'sha256': hashes, 'models': models['roles'], 'execution_limits': settings,
             'evidence_scope': 'input-only design estimates; no RTL or physical implementation'})
        dump(out / 'input_check.json', input_check)
    lock = out / '.lock'
    if lock.exists():
        try:
            os.kill(int(lock.read_text()), 0)
        except ProcessLookupError:
            lock.unlink()
        except PermissionError:
            raise RuntimeError('run lock may be active; process check permission unavailable')
        else:
            raise RuntimeError('run is active; lock held')
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return out, lock


def close_run(out, lock, state):
    dump(out / 'status.json', state)
    evidence = {str(p.relative_to(out)): digest(p.read_bytes()) for p in sorted(out.rglob('*'))
                if p.is_file() and p.name not in ('.lock', 'evidence.json') and not p.name.endswith('.tmp')}
    dump(out / 'evidence.json', {'sha256': evidence, 'scope': state['status']})
    lock.unlink()


def verify_skills(harness):
    manifest = json.loads((harness/'skills/manifest.json').read_text())
    expected = manifest['sha256']
    actual = {str(p.relative_to(harness/'skills')):digest(p.read_bytes())
              for p in (harness/'skills').rglob('*') if p.is_file() and p.name not in ('manifest.json','.DS_Store') and '__pycache__' not in p.parts}
    if actual != expected:
        raise ValueError('vendored skills changed; explicitly update from canonical library')


def architecture_text(document,budget=None):
    """One rendered proposal, shared by draft, audit packet and final export."""
    text=document['architecture_md']+'\n'
    return text+(budget_tables(budget) if budget is not None else '')


def save_draft(out,label,document,budget=None):
    folder=out/'drafts'/label
    folder.mkdir(parents=True,exist_ok=True)
    if isinstance(document.get('architecture_md'),str):
        (folder/'architecture.md').write_text(architecture_text(document,budget))


def budget_tables(report):
    """Tables generated from actual calculator results, not model arithmetic."""
    lines=['\n## 程序复算预算','这些表由冻结 skill 的计算结果生成；估算不代表综合或时序闭合。','', '| 候选 | 约束 | 复算值 | 限值 | 单位 | 满足估算 |','|---|---|---:|---:|---|---|']
    for cid,candidate in report.get('candidates',{}).items():
        for check in candidate.get('checks',[]):
            lines.append('| '+' | '.join(str(v).replace('|','\\|') for v in (cid,check['constraint'],check.get('actual','待验证'),check.get('limit','—'),check.get('unit','—'),check.get('estimate_pass','待验证')))+' |')
    return '\n'.join(lines)+'\n'
