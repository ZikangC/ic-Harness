"""Baseline citation locations guide explicit model repairs without accepting bad evidence."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

import test_audit as audit
from harness.runtime import audit_session
from harness.runtime.skill_tools import execute_script


def location_fixture(repeats=9):
    quote='All writes must be accepted before completion.'
    sources={'spec.md':'范围：独立合成输入\r\n'+(quote+'\r\n')*repeats+'Final task constraints.\r\n',
             'reference.py':'def transform(datum):\r\n    return datum + 1\r\n'}
    baseline={'scope':'independent source-only baseline fixture','limits':'source interpretation and completeness are not proven',
              'ambiguities':[], 'obligations':[
                  {'id':'write-completion','category':'completion','requirement':'completion follows accepted writes',
                   'references':[{'file':'spec.md','quote':quote}]},
                  {'id':'arithmetic','category':'computation','requirement':'perform the declared source arithmetic',
                   'references':[{'file':'reference.py','quote':'return datum + 1'}]}]}
    # Executed scalar witness keeps citation tests independent of proposal data.
    baseline['analysis']=audit.baseline_fixture({k:v for k,v in sources.items() if k in ('spec.md','reference.py')})['analysis']
    baseline['analysis']['workloads'][0]['quantity']=1
    baseline['analysis']['computations'][0]['steps'][0]['shape']=[1]
    return baseline,sources


class BaselineLocationTests(unittest.TestCase):
    def assert_actual_location(self,row,sources):
        self.assertIn(row['file'],('spec.md','reference.py'))
        self.assertIs(type(row['start']),int);self.assertIs(type(row['end']),int)
        lines=sources[row['file']].splitlines()
        self.assertTrue(1<=row['start']<=row['end']<=len(lines))
        self.assertIn(row['quote'],'\n'.join(lines[row['start']-1:row['end']]))

    def test_repeated_exact_excerpt_has_bounded_locations_but_needs_explicit_bounds(self):
        baseline,sources=location_fixture();before=copy.deepcopy(baseline)
        checked=audit.validator.validate_baseline(baseline,sources)
        self.assertFalse(checked['ok']);self.assertEqual(baseline,before)
        diagnostic=checked['citation_diagnostics'][0]
        self.assertEqual(diagnostic['obligation_id'],'write-completion');self.assertEqual(diagnostic['reference_index'],0)
        self.assertEqual(diagnostic['kind'],'ambiguous_excerpt');self.assertEqual(diagnostic['match_count'],9)
        self.assertLessEqual(len(diagnostic['matches']),6);self.assertEqual(len(diagnostic['matches']),6)
        self.assertTrue(diagnostic['matches_truncated'])
        for row in diagnostic['matches']:self.assert_actual_location(row,sources)
        self.assertNotIn('start',baseline['obligations'][0]['references'][0])
        for bounds in ({'start':1,'end':1},{'start':0,'end':0},{'start':2.0,'end':2}):
            changed=copy.deepcopy(baseline);changed['obligations'][0]['references'][0].update(bounds)
            self.assertFalse(audit.validator.validate_baseline(changed,sources)['ok'])
        fixed=copy.deepcopy(baseline);fixed['obligations'][0]['references'][0].update(start=2,end=2)
        good=audit.validator.validate_baseline(fixed,sources)
        self.assertTrue(good['ok'],good['errors']);self.assertEqual(good['citation_diagnostics'],[])

    def test_multiline_duplicate_locations_preserve_actual_start_and_end(self):
        baseline,sources=location_fixture(1);quote='sequence begins\nsequence ends'
        sources['spec.md']='header\n'+quote+'\nseparator\n'+quote+'\n'
        baseline['obligations'][0]['references'][0]['quote']=quote
        checked=audit.validator.validate_baseline(baseline,sources);self.assertFalse(checked['ok'])
        diagnostic=checked['citation_diagnostics'][0]
        self.assertEqual(diagnostic['match_count'],2)
        self.assertEqual([(row['start'],row['end']) for row in diagnostic['matches']],[(2,3),(5,6)])
        for row in diagnostic['matches']:self.assert_actual_location(row,sources)
        baseline['obligations'][0]['references'][0].update(start=5,end=6)
        good=audit.validator.validate_baseline(baseline,sources);self.assertTrue(good['ok'],good['errors'])

    def test_inexact_excerpt_suggests_real_lines_without_rewriting_baseline(self):
        baseline,sources=location_fixture(1)
        baseline['obligations'][0]['references'][0]['quote']='All writes must be accepetd before completion.'
        before=copy.deepcopy(baseline);source_bytes={name:text.encode() for name,text in sources.items()}
        checked=audit.validator.validate_baseline(baseline,sources)
        self.assertFalse(checked['ok']);self.assertEqual(baseline,before)
        diagnostic=checked['citation_diagnostics'][0]
        self.assertEqual(diagnostic['kind'],'inexact_excerpt');self.assertTrue(diagnostic['suggestions'])
        self.assertLessEqual(len(diagnostic['suggestions']),3)
        for row in diagnostic['suggestions']:self.assert_actual_location(row,sources)
        self.assertTrue(any(row['quote']=='All writes must be accepted before completion.' for row in diagnostic['suggestions']))
        self.assertEqual({name:text.encode() for name,text in sources.items()},source_bytes)
        self.assertIn('no automatic',diagnostic['scope'].lower())

    def test_nonoriginal_or_missing_source_never_gets_location_suggestions(self):
        for file in ('design.json','architecture.md','budget.json','/etc/passwd','absent.py'):
            baseline,sources=location_fixture(1);baseline['obligations'][0]['references'][0]['file']=file
            sources[file]='All writes must be accepted before completion.'
            before=copy.deepcopy(baseline)
            with self.subTest(file=file):
                checked=audit.validator.validate_baseline(baseline,sources)
                self.assertFalse(checked['ok']);diagnostic=checked['citation_diagnostics'][0]
                self.assertEqual(diagnostic['kind'],'invalid_reference')
                self.assertFalse(diagnostic.get('matches'));self.assertFalse(diagnostic.get('suggestions'))
                self.assertEqual(baseline,before)
        baseline,sources=location_fixture(1);del sources['spec.md']
        checked=audit.validator.validate_baseline(baseline,sources)
        self.assertFalse(checked['ok']);self.assertFalse(checked['citation_diagnostics'][0].get('matches'))
        self.assertFalse(checked['citation_diagnostics'][0].get('suggestions'))

    def test_runtime_repairs_use_executed_locations_and_preserve_original_versions(self):
        baseline,sources=location_fixture();fixed=copy.deepcopy(baseline)
        fixed['obligations'][0]['references'][0].update(start=2,end=2)
        before=copy.deepcopy(baseline);source_bytes={name:text.encode() for name,text in sources.items()}
        out=audit.snapshot(sources);(out/'audit-baseline.current.json').unlink()
        raw_folder=out/'raw-input';raw_folder.mkdir()
        for name,data in source_bytes.items():(raw_folder/name).write_bytes(data)
        prompts=[];first_saved_bytes=[]
        def invoke(name,role,prompt,parser):
            prompts.append(prompt)
            self.assertNotIn('FILE design.json',prompt);self.assertNotIn('FILE architecture.md',prompt)
            if name.endswith('baseline-0'):return audit.wire_reply(baseline,parser)
            self.assertEqual(name,'locator.baseline-1')
            first_saved_bytes.append((out/'locator.baseline-0.json').read_bytes())
            report=json.loads((out/'locator.baseline-0.check.result.json').read_text())
            self.assertFalse(report['ok']);self.assertTrue(report['citation_diagnostics'])
            feedback=json.loads(prompt.split('\nCURRENT BASELINE CHECK FEEDBACK\n',1)[1])
            self.assertEqual(feedback['citation_diagnostics'],report['citation_diagnostics'])
            self.assertEqual(json.loads((out/'locator.baseline-0.json').read_text()),baseline)
            execution=json.loads((out/'locator.baseline-0.check.execution.json').read_text())
            self.assertEqual(execution['exit_code'],0)
            request=json.loads((out/'locator.baseline-0.check.request.json').read_text())
            self.assertEqual(request['baseline'],baseline)
            self.assertEqual({name:text.encode() for name,text in request['sources'].items()},source_bytes)
            return audit.wire_reply(fixed,parser)
        record=audit_session.prepare_baseline(out,'locator',sources,invoke,1)
        self.assertEqual(len(prompts),2);self.assertEqual(record['baseline'],fixed)
        self.assertEqual(baseline,before);self.assertEqual({name:text.encode() for name,text in sources.items()},source_bytes)
        self.assertEqual((out/'locator.baseline-0.json').read_bytes(),first_saved_bytes[0])
        self.assertEqual(json.loads((out/'locator.baseline-0.json').read_text()),before)
        for name,data in source_bytes.items():self.assertEqual((raw_folder/name).read_bytes(),data)

    def test_copied_frozen_script_produces_locations_without_modifying_source_files(self):
        baseline,sources=location_fixture();out=audit.snapshot()
        folder=Path(tempfile.mkdtemp());before=copy.deepcopy(baseline)
        for name,text in sources.items():(folder/name).write_bytes(text.encode())
        original={name:(folder/name).read_bytes() for name in sources}
        script=out/'snapshot/harness/skills/microarchitecture-audit/scripts/validate_review.py'
        report=execute_script(out,'source-locations',script,{'mode':'baseline','baseline':baseline,'sources':sources})
        self.assertFalse(report['ok']);self.assertTrue(report['citation_diagnostics'])
        self.assertEqual(baseline,before)
        for name,data in original.items():self.assertEqual((folder/name).read_bytes(),data)


if __name__=='__main__':unittest.main()
