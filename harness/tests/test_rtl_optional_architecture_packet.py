"""Optional adopted-architecture evidence is copied and bound as one packet."""
import hashlib,json,os,sys,tempfile,unittest
from pathlib import Path
from harness.runtime.rtl_worker import dispatch,inventory

class OptionalPacket(unittest.TestCase):
    def test_optional_packet_survives_init_resume_and_detects_tamper(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);h=root/'harness';h.mkdir();(h/'test.txt').write_text('fixture')
            inputs=root/'input';inputs.mkdir()
            for name,body in [('spec.md','spec'),('reference.py','x=1\n')]:
                (inputs/name).write_text(body)
            b=root/'result/architecture';(b/'input').mkdir(parents=True)
            for name in ('architecture.md','SOURCE.md'):(b/name).write_text('frozen')
            for name in ('design.json','budgets.json','review.json'):(b/name).write_text('{}')
            (b/'decision.json').write_text('{"deliverable":true}')
            for name in ('spec.md','reference.py'):(b/'input'/name).write_bytes((inputs/name).read_bytes())
            (b/'budget_binding.json').write_text('{"binding":"actual-evidence"}')
            (b/'CHANGES.md').write_text('accepted revision provenance')
            req=dict(action='init',resume=False,input_sha256={n:hashlib.sha256((inputs/n).read_bytes()).hexdigest() for n in ('spec.md','reference.py')},harness_sha256=inventory(h),settings={},models={},job_isolation=dict(backend='docker_readonly_job_v1',uid=os.getuid(),gid=os.getgid()))
            dispatch(root,'packet',req)
            out=root/'output/packet'
            self.assertEqual(json.loads((out/'manifest.json').read_text())['architecture_sha256'],inventory(b))
            self.assertEqual(dispatch(root,'packet',dict(req,resume=True))['run_dir'],str(out))
            dispatch(root,'packet',{'action':'set_status','value':{'status':'tested'}})
            (out/'snapshot/architecture/budget_binding.json').write_text('changed')
            with self.assertRaisesRegex(ValueError,'frozen architecture changed'):
                dispatch(root,'packet',{'action':'rtl_readiness_seed','validate_only':True})
