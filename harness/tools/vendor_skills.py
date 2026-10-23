#!/usr/bin/env python3
"""Explicitly vendor the two architecture skills; never runs during design."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile

NAMES = ('microarchitecture-design','microarchitecture-audit')

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',required=True,type=Path,help='canonical skill library directory')
    p.add_argument('--name', action='append', dest='names', help='skill to vendor; defaults to the two architecture skills')
    args=p.parse_args()
    names=tuple(args.names or NAMES)
    target=Path(__file__).resolve().parents[1]/'skills'
    # Validate all sources before altering the installed copy.
    for name in names:
        if not (args.source/name/'SKILL.md').is_file():
            raise ValueError('missing canonical skill '+name)
    with tempfile.TemporaryDirectory(prefix='skills-vendor-') as tmp:
        staging=Path(tmp)
        for name in names:
            shutil.copytree(args.source/name,staging/name,ignore=shutil.ignore_patterns('__pycache__','.DS_Store'))
        hashes={str(f.relative_to(staging)):hashlib.sha256(f.read_bytes()).hexdigest() for f in staging.rglob('*') if f.is_file()}
        # Explicit maintenance action, not part of a running stage.
        for name in names:
            if (target/name).exists():shutil.rmtree(target/name)
            shutil.copytree(staging/name,target/name)
        old=json.loads((target/'manifest.json').read_text())['sha256'] if (target/'manifest.json').exists() else {}
        hashes={**{name:h for name,h in old.items() if name.split('/')[0] not in names},**hashes}
        (target/'manifest.json').write_text(json.dumps({'source_library':args.source.name,'update_policy':'explicit vendoring; runtime never reads canonical source','sha256':hashes},indent=2)+'\n')
    print('vendored '+', '.join(names)+'; start a new run after updating')

if __name__=='__main__':main()
