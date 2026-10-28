"""Executor contract tests.  Real EDA fixture execution belongs on the server."""
import base64
import io
import sys
import time
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from harness.runtime import rtl_structure as check


class StructureExecutorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'source'
        self.root.mkdir()
        self.root = self.root.resolve()
        (self.root / 'top.sv').write_text('module top(input a, output y); assign y=a; endmodule\n')
        self.tool = Path(self.directory.name) / 'yosys'
        self.tool.write_bytes(b'fixture tool identity, never executed locally')
        self.lint_tool = Path(self.directory.name) / 'verilator'
        self.lint_tool.write_bytes(b'fixture lint tool identity, never executed locally')
        self.spec = {'top': 'top', 'sources': ['top.sv'], 'tool_path': str(self.tool),
                     'lint_tool_path': str(self.lint_tool), 'timeout_seconds': 10, 'binding': {}}

    def config(self, **changes):
        spec = dict(self.spec)
        spec.update(changes)
        return check.normalize_config(self.root, spec)

    def transcript(self, body, blackbox=(), whitebox=()):
        before = []
        for kind, modules in (('blackbox', blackbox), ('whitebox', whitebox)):
            begin, end = check.MODULE_MARKERS[kind]
            before.extend([begin, *modules, end])
        return ('\n'.join(before) + '\n').encode() + body

    def actual(self, argv, stdout=b'', stderr=b'', code=0, timed_out=False, complete=True):
        return check._execution_result(argv, code, timed_out, time.monotonic(),
                                       {'stdout': [stdout], 'stderr': [stderr]}, complete)

    def fake_tool(self, actual_stdout, actual_stderr=b'', actual_code=0, mutate=None,
                  inventory=True, blackbox=(), whitebox=(), lint_stdout=b'', lint_stderr=b'',
                  lint_code=0, lint_complete=True, lint_timed_out=False):
        def invoke(argv, timeout, cwd, **kwargs):
            if argv[-1] in ('-V', '--version'):
                version = b'Verilator fixture version\n' if argv[0] == str(self.lint_tool) else b'Yosys fixture version\n'
                return self.actual(argv, version)
            if argv[0] == str(self.lint_tool):
                return self.actual(argv, lint_stdout, lint_stderr, lint_code,
                                   timed_out=lint_timed_out, complete=lint_complete)
            if mutate: mutate()
            stdout = self.transcript(actual_stdout, blackbox, whitebox) if inventory else actual_stdout
            return self.actual(argv, stdout, actual_stderr, actual_code)
        return patch.object(check, '_execution', side_effect=invoke)

    def test_script_preserves_order_and_has_frontend_only_no_mapping(self):
        (self.root / 'second.sv').write_text('module second; endmodule\n')
        (self.root / 'inc').mkdir()
        config = self.config(sources=['second.sv', 'top.sv'], include_dirs=['inc'],
                             defines={'MODE': None, 'WIDTH': '8'}, parameters={'W': "8'd8"})
        script = check.make_script(config, "/tmp/harness-test")
        self.assertLess(script.index('second.sv'), script.index('top.sv'))
        self.assertIn('-sv -noautowire', script)
        self.assertIn('hierarchy -check -top top -chparam W 8\'d8', script)
        self.assertIn('\nproc -noopt\n', script)
        self.assertIn('\ncheck -assert\n', script)
        self.assertLess(script.index('\nproc -noopt\n'), script.index('\ncheck -assert\n'))
        self.assertNotIn('abc', script)
        self.assertNotIn('\nopt', script)
        self.assertNotIn('synth', script)

    def test_config_rejects_untrusted_script_or_command_injection(self):
        for changes in ({'script': 'check'}, {'top': 'top; shell whoami'},
                        {'parameters': {'W': '8; shell x'}}, {'defines': {'W': '8; shell x'}},
                        {'sources': ['../escape.sv']}, {'timeout_seconds': True},
                        {'parameters': {'W': 8}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.config(**changes)

    def test_clean_exit_requires_real_completion_marker_and_no_warnings(self):
        with self.fake_tool((check.COMPLETE_MARKER + '\n').encode()):
            result = check.run_config(self.config())
        self.assertTrue(result['ok'])
        self.assertEqual(result['status'], 'passed')
        self.assertTrue(result['source_stable'])
        self.assertEqual(result['config_sha256'], check.digest(self.config()))
        self.assertEqual(result['tool']['binary_sha256'], check.file_sha(self.tool))
        self.assertEqual(base64.b64decode(result['execution']['stdout_base64']),
                         self.transcript((check.COMPLETE_MARKER + '\n').encode()))
        with self.fake_tool(b'normal looking incomplete transcript\n'):
            result = check.run_config(self.config())
        self.assertEqual(result['status'], 'tool_failure')
        self.assertFalse(result['ok'])

    def test_bitwise_undriven_diagnostics_are_preserved_not_reduced_to_return_code(self):
        output = (b'Warning: Wire top.\\word [7] is used but has no driver.\n'
                  b'Warning: Wire top.\\word [6] is used but has no driver.\n'
                  b"ERROR: Found 2 problems in 'check -assert'.\n")
        with self.fake_tool(output, actual_code=1):
            result = check.run_config(self.config())
        self.assertEqual(result['status'], 'findings')
        self.assertFalse(result['ok'])
        self.assertEqual(len(result['diagnostics']['undriven']), 2)
        self.assertEqual(result['diagnostics']['errors'], [])
        self.assertEqual(len(result['diagnostics']['check_assert_errors']), 1)
        findings = result['diagnostics']['findings']
        self.assertEqual({f['category'] for f in findings}, {'used_undriven'})
        self.assertNotEqual(findings[0]['finding_id'], findings[1]['finding_id'])
        again = check.parse_diagnostics(output.decode(), '', result['config_sha256'])
        self.assertEqual(findings, again['findings'])

    def test_warning_and_check_summary_cannot_hide_abnormal_exit(self):
        output = (b'Warning: Wire top.\\word [7] is used but has no driver.\n'
                  b"ERROR: Found 1 problems in 'check -assert'.\n")
        for code in (-11, -9, 2, 139):
            with self.subTest(code=code), self.fake_tool(output, actual_code=code):
                result = check.run_config(self.config())
            self.assertEqual(result['status'], 'tool_failure')
            self.assertFalse(result['ok'])
        with self.fake_tool(output, actual_code=0):
            self.assertEqual(check.run_config(self.config())['status'], 'tool_failure')

    def test_actual_module_inventory_must_be_complete_and_is_reported(self):
        output = (check.COMPLETE_MARKER + '\n').encode()
        with self.fake_tool(output, blackbox=['peripheral'], whitebox=['math_macro']):
            result = check.run_config(self.config())
        self.assertEqual(result['compile']['blackbox_modules'], ['peripheral'])
        self.assertEqual(result['compile']['whitebox_modules'], ['math_macro'])
        self.assertTrue(result['compile']['module_inventory_complete'])
        with self.fake_tool(output, inventory=False):
            self.assertEqual(check.run_config(self.config())['status'], 'tool_failure')
        for malformed in (self.transcript(output) + (check.MODULE_MARKERS['blackbox'][0] + '\n').encode(),
                          self.transcript(output, blackbox=['Warning: unexpected output'])):
            with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                check.parse_module_inventory(malformed.decode())

    def test_unknown_warning_even_exit_zero_is_not_clean_pass(self):
        output = ('Warning: Future unclassified frontend warning.\n' + check.COMPLETE_MARKER + '\n').encode()
        with self.fake_tool(output):
            result = check.run_config(self.config())
        self.assertEqual(result['status'], 'findings')
        self.assertEqual(result['diagnostics']['unclassified_warning_count'], 1)
        self.assertFalse(result['ok'])

    def test_known_memory_lowering_warning_requires_exact_disposition_not_global_suppression(self):
        warning = 'Warning: Replacing memory \\mem with list of registers. See top.sv:12'
        with self.fake_tool((warning + '\n' + check.COMPLETE_MARKER + '\n').encode()):
            result = check.run_config(self.config())
        self.assertEqual(result['status'], 'findings')
        self.assertFalse(result['ok'])
        self.assertEqual(result['diagnostics']['unclassified_warning_count'], 0)
        self.assertEqual(result['diagnostics']['findings'][0]['classification'], 'memory_lowering')
        self.assertEqual(result['diagnostics']['findings'][0]['message'], warning)

    def test_parser_error_nonzero_without_diagnostics_and_version_failure_are_not_waivable(self):
        for stdout, stderr, code in ((b'', b'ERROR: syntax error\n', 1),
                                     (b'', b'', 139)):
            with self.subTest(code=code), self.fake_tool(stdout, stderr, code):
                result = check.run_config(self.config())
                self.assertEqual(result['status'], 'tool_failure')
        with patch.object(check, '_execution', return_value=self.actual([], b'', b'bad version', 1)):
            self.assertEqual(check.run_config(self.config())['status'], 'tool_failure')

    def test_timeout_keeps_partial_original_bytes_and_cannot_pass(self):
        def invoke(argv, timeout, cwd, **kwargs):
            if argv[-1] in ('-V', '--version'):
                return self.actual(argv, b'fixture version\n')
            if argv[0] == str(self.lint_tool): return self.actual(argv)
            return self.actual(argv, b'partial\xff', b'last diagnostic', None, True)
        with patch.object(check, '_execution', side_effect=invoke):
            result = check.run_config(self.config())
        self.assertEqual(result['status'], 'timed_out')
        self.assertEqual(base64.b64decode(result['execution']['stdout_base64']), b'partial\xff')
        self.assertFalse(result['ok'])

    def test_source_before_after_and_trusted_inventory_binding_are_checked(self):
        changed = lambda: (self.root / 'top.sv').write_text('module top; endmodule\n')
        with self.fake_tool(check.COMPLETE_MARKER.encode(), mutate=changed):
            result = check.run_config(self.config())
        self.assertEqual(result['status'], 'invalid')
        self.assertFalse(result['source_stable'])
        with patch.object(check, '_execution') as tool:
            result = check.run_config(self.config(binding={'source_inventory_sha256': '0' * 64}))
        self.assertEqual(result['status'], 'invalid')
        tool.assert_not_called()

    def test_dependencies_include_header_and_memory_data_in_typed_boundary(self):
        (self.root / 'inc').mkdir()
        (self.root / 'inc' / 'body.svh').write_text('assign y=a;\n')
        (self.root / 'memory.hex').write_text('00\n')
        (self.root / 'top.sv').write_text('module top(input a,output y);\n`include "body.svh"\n'
                                        'reg [7:0] m[0:0]; initial $readmemh("memory.hex",m); endmodule\n')
        config = self.config(include_dirs=['inc'])
        references = check.check_dependency_policy(config)
        self.assertEqual({item['path'] for item in references}, {'inc/body.svh', 'memory.hex'})
        with self.fake_tool(check.COMPLETE_MARKER.encode()):
            result = check.run_config(config)
        self.assertIn('inc/body.svh', result['compile']['source_files_sha256'])
        self.assertIn('memory.hex', result['compile']['source_files_sha256'])
        self.assertEqual(result['compile']['source_inventory_sha256'], check.digest(check.closure_inventory(self.root)))

    def test_external_and_dynamic_file_dependencies_are_explicitly_unsupported(self):
        for source in ('`include "/external/a.svh"\n', '`include `FILE\n',
                       'initial $readmemh(filename, memory);\n'):
            (self.root / 'top.sv').write_text(source)
            with self.subTest(source=source), patch.object(check, '_execution') as tool:
                result = check.run_config(self.config())
            self.assertEqual(result['status'], 'unsupported')
            self.assertFalse(result['ok'])
            tool.assert_not_called()

    def test_dependency_lexer_ignores_comments_and_strings(self):
        (self.root / 'top.sv').write_text('''// `include "/outside/bad"\n/* $readmemh(x,y); */\nmodule top;\ninitial $display("$readmemh(dynamic, x)");\nendmodule\n''')
        self.assertEqual(check.check_dependency_policy(self.config()), [])

    def test_external_symlink_cannot_be_claimed_as_closed_source(self):
        (self.root / 'escape').symlink_to(self.tool)
        with self.assertRaisesRegex(ValueError, 'external symlink'):
            check.closure_inventory(self.root)

    def test_options_have_real_token_boundaries_and_lint_same_full_config(self):
        (self.root / 'header folder').mkdir()
        c = self.config(include_dirs=['header folder'], defines={'FLAG': None, 'N': '4'},
                        parameters={'WIDTH': "8'd4"})
        script = check.make_script(c, "/tmp/harness-test")
        self.assertIn('-I/tmp/harness-test/include-0', script)
        self.assertNotIn(str(self.root / 'header folder'), script)
        self.assertNotIn('"-I', script)
        self.assertIn('-DN=4', script)
        self.assertNotIn('"-D', script)
        argv = check.make_lint_argv(c, '/private/tmp/fixture temp')
        self.assertIn('-I' + str(self.root / 'header folder'), argv)
        self.assertIn('-DN=4', argv)
        self.assertIn("-GWIDTH=8'd4", argv)
        self.assertEqual(argv[argv.index('--top-module') + 1], c['top'])
        self.assertEqual(argv[-1], str(self.root / 'top.sv'))
        for code in check.LINT_TARGET_CODES: self.assertIn('-Wwarn-' + code, argv)
        self.assertNotIn('--bbox-unsup', argv)

    def test_lint_target_findings_continue_full_yosys_and_require_disposition(self):
        warning = b"%Warning-UNDRIVEN: top.sv:1:8: Signal is not driven: 'a'\n    1 | wire a;\n      |      ^\n"
        with self.fake_tool(check.COMPLETE_MARKER.encode(), lint_stderr=warning) as actual:
            result = check.run_config(self.config())
        self.assertTrue(result['fast_lint']['completed'])
        self.assertEqual(result['fast_lint']['status'], 'findings')
        self.assertEqual(result['status'], 'findings')
        self.assertFalse(result['ok'])
        self.assertIsNotNone(result['execution'])
        self.assertTrue(result['compile']['module_inventory_complete'])
        f = result['fast_lint']['diagnostics']['findings'][0]
        self.assertEqual(f['classification'], 'verilator_UNDRIVEN')
        self.assertIn('wire a;', f['message'])
        self.assertEqual(result['fast_lint']['diagnostics'], check.parse_lint_diagnostics('', warning.decode(), result['config_sha256']))
        self.assertEqual(sum(call.args[0][0] == str(self.tool) for call in actual.call_args_list), 2)

    def test_execution_plan_is_explicit_validated_and_bound_without_changing_omitted_config(self):
        original = self.config()
        self.assertNotIn('execution_plan', original)
        self.assertEqual(set(original), {'version', 'source_root', 'top', 'sources', 'include_dirs',
                                        'defines', 'parameters', 'tool_path', 'lint_tool_path',
                                        'timeout_seconds', 'binding'})
        for plan in ('complete', 'diagnostic_first'):
            explicit = self.config(execution_plan=plan)
            self.assertEqual(explicit.pop('execution_plan'), plan)
            self.assertEqual(explicit, original)
            self.assertNotEqual(check.digest(self.config(execution_plan=plan)), check.digest(original))
        for plan in (None, False, '', 'lint_only', [], {}):
            with self.subTest(plan=plan), self.assertRaisesRegex(ValueError, 'execution_plan'):
                self.config(execution_plan=plan)

    def test_diagnostic_first_findings_preserve_complete_negative_evidence_without_yosys(self):
        warning = b"%Warning-UNDRIVEN: top.sv:1:8: Signal is not driven: 'a'\n    1 | wire a;\n      |      ^\n"
        with self.fake_tool(b'should never execute Yosys', lint_stderr=warning) as actual:
            result = check.run_config(self.config(execution_plan='diagnostic_first'))
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'findings')
        self.assertEqual(result['coverage'], {'kind': 'diagnostic_only', 'fast_lint': 'complete',
                                             'bitwise': 'unclosed', 'yosys': 'not_run',
                                             'structural_release': False})
        self.assertIsNone(result['execution'])
        self.assertFalse(result['compile']['module_inventory_complete'])
        self.assertEqual([call.args[0][0] for call in actual.call_args_list], [str(self.lint_tool)] * 2)
        lint = result['fast_lint']
        self.assertTrue(lint['completed'])
        self.assertEqual(base64.b64decode(lint['execution']['stderr_base64']), warning)
        self.assertEqual(lint['diagnostics'], check.parse_lint_diagnostics('', warning.decode(), result['config_sha256']))
        self.assertEqual(json.loads(json.dumps(result)), result)
        self.assertTrue(result['source_stable'])
        self.assertTrue(result['compile']['include_aliases_stable'])
        self.assertTrue(lint['tool']['binary_stable'])
        self.assertNotIn('binary_sha256', result['tool'])
        self.assertNotIn('yosys_execution_seconds', result['timings'])

    def test_diagnostic_first_clean_lint_still_requires_complete_yosys(self):
        for output, expected in (((check.COMPLETE_MARKER + '\n').encode(), 'passed'),
                                 (b'incomplete Yosys', 'tool_failure')):
            with self.subTest(expected=expected), self.fake_tool(output) as actual:
                result = check.run_config(self.config(execution_plan='diagnostic_first'))
            self.assertEqual(result['status'], expected)
            self.assertEqual(result['ok'], expected == 'passed')
            self.assertNotIn('coverage', result)
            self.assertEqual(sum(call.args[0][0] == str(self.tool) for call in actual.call_args_list), 2)

    def test_explicit_complete_retains_target_warning_disposition_path(self):
        warning = b'%Warning-UNDRIVEN: top.sv:1: unused interface bit\n'
        with self.fake_tool((check.COMPLETE_MARKER + '\n').encode(), lint_stderr=warning) as actual:
            result = check.run_config(self.config(execution_plan='complete'))
        self.assertEqual(result['status'], 'findings')
        self.assertFalse(result['ok'])
        self.assertTrue(result['compile']['module_inventory_complete'])
        self.assertIsNotNone(result['execution'])
        self.assertEqual(sum(call.args[0][0] == str(self.tool) for call in actual.call_args_list), 2)

    def test_diagnostic_first_lint_failure_is_not_a_dut_finding_conclusion(self):
        warning = b'%Warning-UNDRIVEN: top.sv:1: partial warning before failure\n'
        for changes, status in [
            ({'lint_stderr': warning + b'%Error: unsupported construct\n', 'lint_code': 1}, 'unsupported'),
            ({'lint_stderr': warning, 'lint_code': -11}, 'tool_failure'),
            ({'lint_stderr': warning, 'lint_complete': False}, 'tool_failure'),
            ({'lint_stderr': warning, 'lint_timed_out': True}, 'timed_out'),
        ]:
            with self.subTest(status=status), self.fake_tool(b'never Yosys', **changes) as actual:
                result = check.run_config(self.config(execution_plan='diagnostic_first'))
            self.assertEqual(result['status'], status)
            self.assertFalse(result['ok'])
            self.assertFalse(result['fast_lint']['completed'])
            self.assertNotIn('coverage', result)
            self.assertIsNone(result['execution'])
            self.assertTrue(all(call.args[0][0] == str(self.lint_tool) for call in actual.call_args_list))

    def test_diagnostic_first_still_rechecks_source_lint_tool_and_include_alias(self):
        warning = b'%Warning-UNDRIVEN: top.sv:1: signal not driven\n'
        (self.root / 'inc').mkdir()
        for target in ('source', 'tool', 'alias'):
            with self.subTest(target=target), self.fake_tool(b'never Yosys', lint_stderr=warning) as actual:
                invoke = actual.side_effect
                def mutate_after_lint(argv, *args, **kwargs):
                    value = invoke(argv, *args, **kwargs)
                    if argv[0] == str(self.lint_tool) and '--lint-only' in argv:
                        if target == 'source':
                            (self.root / 'top.sv').write_text('module top; endmodule\n')
                        elif target == 'tool':
                            self.lint_tool.write_bytes(b'changed fixture tool')
                        else:
                            alias = Path(argv[argv.index('--Mdir') + 1]).parent / 'include-0'
                            alias.unlink()
                    return value
                actual.side_effect = mutate_after_lint
                result = check.run_config(self.config(execution_plan='diagnostic_first', include_dirs=['inc']))
            self.assertEqual(result['status'], 'invalid')
            self.assertFalse(result['ok'])
            self.assertIsNone(result['execution'])
            if target == 'source': self.assertFalse(result['source_stable'])
            if target == 'tool': self.assertFalse(result['fast_lint']['tool']['binary_stable'])
            if target == 'alias': self.assertFalse(result['compile']['include_aliases_stable'])

    def test_lint_other_warning_kept_but_not_promoted_to_structural_acceptance(self):
        warning = b'%Warning-WIDTH: top.sv:1:8: Width mismatch\n'
        with self.fake_tool(check.COMPLETE_MARKER.encode(), lint_stderr=warning):
            result = check.run_config(self.config())
        self.assertEqual(result['fast_lint']['diagnostics']['findings'], [])
        self.assertEqual(result['fast_lint']['diagnostics']['other_warnings'], [warning.decode().strip()])
        self.assertEqual(result['status'], 'passed')
        # Lint alone still cannot replace missing Yosys completion.
        with self.fake_tool(b'', lint_stderr=warning, inventory=False):
            self.assertFalse(check.run_config(self.config())['ok'])

    def test_lint_compile_failure_crash_incomplete_or_timeout_never_waivable(self):
        for changes in ({'lint_stderr': b'%Error: top.sv:1: syntax error\n', 'lint_code': 1},
                        {'lint_code': -11}, {'lint_complete': False}):
            with self.subTest(changes=changes), self.fake_tool(check.COMPLETE_MARKER.encode(), **changes):
                result = check.run_config(self.config())
            self.assertFalse(result['ok'])
            self.assertFalse(result['fast_lint']['completed'])
            self.assertIsNone(result['execution'])
        with self.fake_tool(check.COMPLETE_MARKER.encode(), lint_stderr=b'%Warning-LATCH: top.sv:3: inferred latch\n'):
            result = check.run_config(self.config())
        self.assertEqual(result['fast_lint']['diagnostics']['findings'][0]['classification'], 'verilator_LATCH')

    def test_lint_parser_separates_target_diagnostics_from_other_warnings_and_errors(self):
        raw = ('%Warning-MULTIDRIVEN: top.sv:2: Multiple drivers for signal\n'
               '    2 | assign y=a;\n'
               '%Warning-WIDTH: top.sv:3: width mismatch\n'
               '%Error-BLKANDNBLK: top.sv:4: mixed assignments\n')
        parsed = check.parse_lint_diagnostics('', raw, '0' * 64)
        self.assertEqual(len(parsed['warnings']), 2)
        self.assertEqual(len(parsed['errors']), 1)
        self.assertEqual(len(parsed['findings']), 1)
        self.assertEqual(parsed['findings'][0]['code'], 'MULTIDRIVEN')
        self.assertEqual(parsed['findings'][0]['tool'], 'verilator')
        self.assertEqual(len(parsed['other_warnings']), 1)
        changed = check.parse_lint_diagnostics('', raw, '1' * 64)
        self.assertNotEqual(parsed['findings'][0]['finding_id'], changed['findings'][0]['finding_id'])

    def test_synthesis_profile_is_explicit_and_conflicting_or_unknown_intrinsics_do_not_pass(self):
        config = self.config(defines={'YOSYS': '1', 'VERILATOR': '7'})
        profile = check.synthesis_profile(config)
        self.assertEqual(profile['effective_defines'], {'SYNTHESIS': '1', 'YOSYS': '1', 'VERILATOR': '7'})
        argv = check.make_lint_argv(config, '/private/tmp/fixture')
        self.assertLess(argv.index('-UVERILATOR'), argv.index('-DVERILATOR=7'))
        self.assertIn('-DSYNTHESIS=1', argv)
        self.assertIn('-DYOSYS=1', argv)
        self.assertIn('-DYOSYS=1', check.make_script(config))
        for intrinsic in ('SYNTHESIS', 'YOSYS'):
            with self.subTest(intrinsic=intrinsic), patch.object(check, '_execution') as tool:
                result = check.run_config(dict(self.spec, source_root=str(self.root), defines={intrinsic: '0'}))
            self.assertEqual(result['status'], 'unsupported')
            self.assertFalse(result['ok'])
            tool.assert_not_called()
        for body in ('`ifdef VERILATOR_UNSUPPORTED\n`endif\n',
                     'wire x = `YOSYS_UNSUPPORTED;\n'):
            (self.root / 'top.sv').write_text(body)
            with self.subTest(body=body), patch.object(check, '_execution') as tool:
                result = check.run_config(self.config())
            self.assertEqual(result['status'], 'unsupported')
            tool.assert_not_called()

    def test_streaming_preserves_raw_both_streams_before_process_completion(self):
        # A plain Python child is not an EDA tool; this tests pipe transport only.
        class Sink(io.BytesIO):
            first_write = None
            def write(self, raw):
                if self.first_write is None: self.first_write = time.monotonic()
                return super().write(raw)
        sink = Sink()
        class ErrorStream:
            buffer = sink
        program = "import sys,time;sys.stdout.buffer.write(b'first\\xff');sys.stdout.flush();time.sleep(.15);sys.stderr.buffer.write(b'second');sys.stderr.flush();time.sleep(.15)"
        with patch.object(check.sys, 'stderr', ErrorStream()):
            actual = check._execution([sys.executable, '-u', '-c', program], 3, self.root)
        self.assertEqual(actual['exit_code'], 0)
        self.assertTrue(actual['capture_complete'])
        self.assertEqual(base64.b64decode(actual['stdout_base64']), b'first\xff')
        self.assertEqual(base64.b64decode(actual['stderr_base64']), b'second')
        self.assertIn(b'first\xff', sink.getvalue())
        self.assertIn(b'second', sink.getvalue())
        self.assertGreater(time.monotonic() - sink.first_write, .1)

    def test_transport_timeout_retains_already_forwarded_bytes(self):
        sink = io.BytesIO()
        class ErrorStream:
            buffer = sink
        with patch.object(check.sys, 'stderr', ErrorStream()):
            actual = check._execution([sys.executable, '-u', '-c',
                                       "import sys,time;print('early',flush=True);time.sleep(10)"], .15, self.root)
        self.assertTrue(actual['timed_out'])
        self.assertIn(b'early', base64.b64decode(actual['stdout_base64']))
        self.assertIn(b'early', sink.getvalue())

    def test_tool_mutation_even_clean_output_invalidates_two_stage_check(self):
        with self.fake_tool(check.COMPLETE_MARKER.encode(), mutate=lambda: self.lint_tool.write_bytes(b'changed')):
            result = check.run_config(self.config())
        self.assertEqual(result['status'], 'invalid')
        self.assertFalse(result['ok'])


    def test_include_aliases_bind_original_space_directory_without_source_mutation(self):
        directory = self.root / 'headers with spaces'
        directory.mkdir(); (directory / 'body.svh').write_text('assign y=a;\n')
        (self.root / 'top.sv').write_text('module top(input a,output y);\n`include "body.svh"\nendmodule\n')
        config = self.config(include_dirs=['headers with spaces'])
        before = check.closure_inventory(self.root)
        observed = []
        def inspect_alias():
            # The fake Yosys phase runs while trusted aliases still exist.
            aliases = list(Path('/tmp').glob('harness-rtl-structure-*/include-0'))
            own = [p for p in aliases if p.resolve() == directory]
            self.assertEqual(len(own), 1)
            self.assertTrue(own[0].is_symlink())
            observed.append(str(own[0].parent.resolve() / own[0].name))
        with self.fake_tool(check.COMPLETE_MARKER.encode(), mutate=inspect_alias):
            result = check.run_config(config)
        self.assertTrue(result['ok'])
        self.assertEqual(check.closure_inventory(self.root), before)
        self.assertTrue(result['compile']['include_aliases_stable'])
        self.assertEqual(result['compile']['include_aliases'][0]['target'], str(directory))
        self.assertEqual(result['compile']['include_aliases'][0]['path'], observed[0])
        self.assertFalse(Path(observed[0]).exists())
        self.assertIn('-I' + observed[0], result['script']['content'])
        self.assertIn('-I' + str(directory), result['fast_lint']['execution']['argv'])

    def test_include_alias_retargeting_invalidates_result_and_never_chmods_source(self):
        directory = self.root / 'headers with spaces'
        directory.mkdir(); (self.root / 'other').mkdir()
        before_mode = directory.stat().st_mode
        def replace_alias():
            own = [p for p in Path('/tmp').glob('harness-rtl-structure-*/include-0')if p.resolve() == directory]
            self.assertEqual(len(own), 1)
            own[0].unlink(); own[0].symlink_to(self.root / 'other', target_is_directory=True)
        with self.fake_tool(check.COMPLETE_MARKER.encode(), mutate=replace_alias):
            result = check.run_config(self.config(include_dirs=['headers with spaces']))
        self.assertEqual(result['status'], 'invalid')
        self.assertFalse(result['ok'])
        self.assertFalse(result['compile']['include_aliases_stable'])
        self.assertEqual(directory.stat().st_mode, before_mode)
        with self.assertRaises(ValueError): check.include_aliases(self.config(include_dirs=['headers with spaces']), '/tmp/bad path')
        with self.assertRaises(ValueError): check.make_script(self.config(include_dirs=['headers with spaces']))


class StructureTimingTests(unittest.TestCase):
    """Timing metadata cannot substitute for the unchanged structural results."""
    setUp = StructureExecutorTests.setUp
    config = StructureExecutorTests.config
    transcript = StructureExecutorTests.transcript
    actual = StructureExecutorTests.actual
    fake_tool = StructureExecutorTests.fake_tool

    def markers(self):
        return check.yosys_timing_markers('/tmp/fresh-trusted-timing-test')

    def test_trusted_script_has_ordered_phase_markers_without_replacing_checks(self):
        script = check.make_script(self.config(), '/tmp/fresh-trusted-timing-test')
        markers = self.markers()
        self.assertEqual(script, check.make_script(self.config(), '/tmp/fresh-trusted-timing-test'))
        self.assertEqual(markers, check.yosys_timing_markers('/tmp/fresh-trusted-timing-test'))
        previous = -1
        for name in check.YOSYS_TIMING_PHASES:
            begin, end = markers[name]
            first, last = script.index('log ' + begin), script.index('log ' + end)
            self.assertGreater(first, previous)
            self.assertGreater(last, first)
            previous = last
        self.assertIn('\nread_verilog -sv -noautowire ', script)
        self.assertIn('\nhierarchy -check -top top\n', script)
        self.assertIn('\nproc -noopt\n', script)
        self.assertIn('\ncheck -assert\n', script)
        self.assertIn('\nlog ' + check.COMPLETE_MARKER + '\n', script)
        self.assertNotEqual(markers, check.yosys_timing_markers('/tmp/other-trusted-timing-test'))

    def test_marker_observations_handle_chunked_crlf_and_require_real_line_end(self):
        observation = check._ObservedYosysTiming(self.markers(), 100.)
        raw = b''.join((marker + '\r\n').encode() for pair in self.markers().values() for marker in pair)
        for offset in range(0, len(raw), 7): observation.feed(raw[offset:offset + 7], 101. + offset / 1000)
        value = observation.result()
        self.assertTrue(value['complete'])
        self.assertTrue(value['markers_valid'])
        self.assertEqual(value['acceptance_effect'], 'none')
        self.assertTrue(all(item['complete'] for item in value['phases'].values()))
        self.assertIn('no exact command wall/CPU duration', value['precision_limit'])
        last = check._ObservedYosysTiming(self.markers(), 100.)
        last.feed(raw.rstrip(b'\r\n'), 101.)
        self.assertFalse(last.result()['complete'])
        self.assertIsNone(last.result()['phases']['check']['observed_duration_seconds'])

    def test_same_read_markers_may_have_zero_observed_duration(self):
        observation = check._ObservedYosysTiming(self.markers(), 100.)
        raw = b''.join((marker + '\n').encode() for pair in self.markers().values() for marker in pair)
        observation.feed(raw, 101.)
        self.assertTrue(observation.result()['complete'])
        self.assertTrue(all(item['observed_duration_seconds'] == 0 for item in observation.result()['phases'].values()))

    def test_wrong_nonce_prefixed_duplicate_and_out_of_order_markers_do_not_complete(self):
        begin, end = self.markers()['read']
        for raw in ((begin + '\n' + begin + '\n' + end + '\n').encode(),
                    (end + '\n' + begin + '\n').encode()):
            value = check._ObservedYosysTiming(self.markers(), 100.)
            value.feed(raw, 101.)
            self.assertFalse(value.result()['markers_valid'])
            self.assertFalse(value.result()['complete'])
            self.assertIsNone(value.result()['phases']['read']['observed_duration_seconds'])
        value = check._ObservedYosysTiming(self.markers(), 100.)
        wrong = check.yosys_timing_markers('/tmp/unrelated-script')['read'][0]
        value.feed((wrong + '\nnoise ' + begin + '\n').encode(), 101.)
        self.assertEqual(value.result()['events'], [])

    def test_long_nonmarker_lines_remain_bounded_and_cannot_inject_a_tail_marker(self):
        observation = check._ObservedYosysTiming(self.markers(), 100.)
        begin, end = self.markers()['read']
        observation.feed(b'x' * 1024 * 1024, 101.)
        self.assertLessEqual(len(observation.pending), observation.maximum)
        observation.feed((begin + '\n' + begin + '\n' + end + '\n').encode(), 102.)
        events = observation.result()['events']
        self.assertEqual([item['edge'] for item in events], ['begin', 'end'])
        self.assertEqual(events[0]['observed_seconds'], 2.)

    def test_completed_executor_records_hash_dependency_tools_and_check_wall_times(self):
        with self.fake_tool((check.COMPLETE_MARKER + '\n').encode()):
            result = check.run_config(self.config())
        self.assertTrue(result['ok'])
        self.assertEqual(result['schema'], 2)
        timing = result['timings']
        for name in ('normalize_seconds', 'source_inventory_before_seconds', 'source_inventory_after_seconds',
                     'dependency_policy_seconds', 'fast_lint_seconds', 'yosys_execution_seconds'):
            self.assertIsInstance(timing[name], float)
            self.assertGreaterEqual(timing[name], 0)
        self.assertIn('worker admission', timing['scope'])
        self.assertIsNone(timing['yosys_phases'])  # A mock cannot manufacture real marker observations.
        for tool in (result['tool'], result['fast_lint']['tool']):
            for name in ('binary_hash_before_seconds', 'version_query_seconds', 'binary_hash_after_seconds'):
                self.assertGreaterEqual(tool['identity_timings'][name], 0)

    def test_timing_is_not_permission_to_pass_a_timeout_or_unsupported_config(self):
        output = (check.COMPLETE_MARKER + '\n').encode()
        with self.fake_tool(output, lint_stderr=b'%Error: top.sv:1: unsupported\n', lint_code=1):
            result = check.run_config(self.config())
        self.assertFalse(result['ok'])
        self.assertNotIn('yosys_execution_seconds', result['timings'])
        result = check.run_config(dict(self.spec, source_root=str(self.root), defines={'YOSYS': '0'}))
        self.assertEqual(result['status'], 'unsupported')
        self.assertFalse(result['ok'])
        self.assertGreaterEqual(result['timings']['normalize_seconds'], 0)

    def test_plain_python_pipe_observation_is_live_and_preserves_original_streams(self):
        sink = io.BytesIO()
        class ErrorStream:
            buffer = sink
        raw_lines = [marker for pair in self.markers().values() for marker in pair]
        program = ('import sys,time\n'
                   'for line in ' + repr(raw_lines) + ':\n'
                   ' sys.stdout.write(line+"\\n");sys.stdout.flush();time.sleep(.025)\n'
                   'sys.stderr.buffer.write(b"original-stderr\\xff");sys.stderr.flush()\n')
        with patch.object(check.sys, 'stderr', ErrorStream()):
            actual = check._execution([sys.executable, '-u', '-c', program], 3, self.root,
                                      timing_markers=self.markers())
        self.assertEqual(actual['exit_code'], 0)
        self.assertTrue(actual['capture_complete'])
        self.assertTrue(actual['observed_phases']['complete'])
        self.assertEqual(base64.b64decode(actual['stderr_base64']), b'original-stderr\xff')
        self.assertEqual(base64.b64decode(actual['stdout_base64']), ('\n'.join(raw_lines) + '\n').encode())
        self.assertIn(check.TIMING_OBSERVATION_PREFIX.encode(), sink.getvalue())
        self.assertIn(b'original-stderr\xff', sink.getvalue())
        for event in actual['observed_phases']['events']:
            self.assertGreaterEqual(event['observed_seconds'], 0)
            self.assertLessEqual(event['observed_seconds'], actual['duration_seconds'])


if __name__ == '__main__':
    unittest.main()
