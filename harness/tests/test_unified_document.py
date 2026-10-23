import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from test_architecture import fixture
import test_architecture as design_tests
from harness.prompts.architecture import parse_design
from harness.runtime import run_store,audit_session
from harness.runtime.design_session import execute_tool,identity


class UnifiedDocumentTests(unittest.TestCase):
    def test_only_new_final_receipt_and_one_document_accepted(self):
        d,s=fixture();prefix='<<<ARCHITECTURE_DATA>>>\n'+json.dumps({'evaluated_design_sha256':identity(d)})+'\n<<<ARCHITECTURE_MD>>>\n'
        result=parse_design(prefix+d['architecture_md'])
        self.assertEqual(set(result),{'evaluated_design_sha256','architecture_md'})
        for suffix in ['\n<<<CONTRACT_MD>>>\nold handoff','\n<<<COMPARISON_MD>>>\nold comparison']:
            with self.assertRaises(ValueError):parse_design(prefix+d['architecture_md']+suffix)
        for extra in [{'contract_md':'old'},{'comparison_md':'old'}]:
            with self.assertRaises(ValueError):parse_design('<<<ARCHITECTURE_DATA>>>\n'+json.dumps({'evaluated_design_sha256':identity(d),**extra})+'\n<<<ARCHITECTURE_MD>>>\n'+d['architecture_md'])
        with self.assertRaises(ValueError):parse_design('<<<ARCHITECTURE_DATA>>>\n'+json.dumps(d)+'\n<<<ARCHITECTURE_MD>>>\n'+d['architecture_md'])

    def test_budget_rejects_extra_prose_and_old_cli_option(self):
        d,s=fixture();out=design_tests.SkillToolTests().setup_run();old=copy.deepcopy(d);old['contract_md']='legacy content'
        result=execute_tool(out,'legacy',old,s,True)
        self.assertFalse(result['ok']);self.assertIn('only architecture_md is a supported design document field',result['errors'])
        script=out/'snapshot/harness/skills/microarchitecture-design/scripts/evaluate.py'
        cli=subprocess.run([sys.executable,'-B',str(script),'--require-documents'],input=json.dumps({'document':d,'sources':s}),capture_output=True,text=True)
        self.assertNotEqual(cli.returncode,0)

    def test_audited_draft_and_export_share_one_rendered_text(self):
        d,s=fixture();out=design_tests.SkillToolTests().setup_run();report=execute_tool(out,'budget',d,s,True)
        run_store.save_draft(out,'version',d,report)
        text=run_store.architecture_text(d,report);packet=audit_session.packet(s,d,report)
        self.assertEqual((out/'drafts/version/architecture.md').read_text(),text)
        self.assertEqual(packet['sources']['architecture.md'],text)
        self.assertEqual(sorted(packet['sources']),['architecture.md','budget.json','design.json','reference.py','spec.md'])
        self.assertEqual([p.name for p in (out/'drafts/version').glob('*.md')],['architecture.md'])
        self.assertIn('程序复算预算',text)

    def test_input_loader_rejects_legacy_proposal_without_conversion(self):
        base=Path(tempfile.mkdtemp(prefix='unified-load-test-'));h=Path(__file__).resolve().parents[1]
        shutil.copytree(h,base/'harness',ignore=shutil.ignore_patterns('__pycache__','.DS_Store'))
        (base/'input').mkdir();(base/'input/spec.md').write_text('synthetic input');(base/'input/reference.py').write_text('def changed_entry(x): return x\n')
        d,s=fixture();proposal=base/'proposal.json';proposal.write_text(json.dumps(d))
        args=SimpleNamespace(config=base/'harness/config/environment.toml',feedback=None,audit_design=proposal)
        run_store.load(args)
        d['comparison_md']='legacy';proposal.write_text(json.dumps(d))
        with self.assertRaisesRegex(ValueError,'exactly one Markdown field'):run_store.load(args)

if __name__=='__main__':unittest.main()
