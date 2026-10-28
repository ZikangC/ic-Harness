"""Source-bound release evidence for the native, pre-mapping structure check.

This module decides no hardware intent. The independent reviewer identifies the
required configurations from frozen sources; the runner verifies actual native
jobs, exact closure/configuration binding and individual supported dispositions.
"""
from __future__ import annotations
import base64
import hashlib
import json
import re
from pathlib import Path

SHA = re.compile(r'[0-9a-f]{64}')
ID = re.compile(r'[0-9]+')
CONFIGURATION_FIELDS = {'top', 'sources', 'include_dirs', 'defines', 'parameters'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _plain(out, relative):
    path = out / relative
    try:
        path.resolve().relative_to(out.resolve())
    except ValueError:
        raise ValueError('structure evidence path escapes run') from None
    for part in [path, *path.parents]:
        if part == out.parent:
            break
        if part.is_symlink():
            raise ValueError('structure evidence metadata must not be a symlink')
    return path


def source_references(out, review, references, *, coverage=False):
    if not isinstance(references, list) or not references:
        raise ValueError('structure disposition needs exact frozen source references')
    kinds = set()
    accepted = []
    for item in references:
        if not isinstance(item, dict) or set(item) != {'path', 'sha256'}:
            raise ValueError('invalid structure source reference')
        name, expected = item['path'], item['sha256']
        if not isinstance(name, str) or not isinstance(expected, str) or not SHA.fullmatch(expected):
            raise ValueError('invalid structure source reference identity')
        if name.startswith('@input/'):
            prefix = 'snapshot/input'; relative = name[7:]; kind = 'input'
        elif name.startswith('@candidate/'):
            prefix = review['candidate_scope']; relative = name[11:]; kind = 'candidate'
        elif name.startswith('@architecture/'):
            # In this evidence contract the alias is the review's frozen copy,
            # so the implementation can declare it before review-N exists.
            prefix = review['architecture_scope']; relative = name[14:]; kind = 'architecture'
        elif name.startswith('@run/' + review['architecture_scope'] + '/'):
            prefix = review['architecture_scope']; relative = name[len('@run/' + prefix + '/'):]; kind = 'architecture'
        else:
            raise ValueError('structure references must name frozen input, candidate or its architecture')
        if not relative or Path(relative).is_absolute() or '..' in Path(relative).parts:
            raise ValueError('invalid structure source reference path')
        path = (out / prefix / relative).resolve()
        try:
            path.relative_to((out / prefix).resolve())
        except ValueError:
            raise ValueError('structure source reference escapes frozen source') from None
        if not path.is_file() or sha(path) != expected:
            raise ValueError('structure source reference is missing or stale: ' + name)
        kinds.add(kind); accepted.append(dict(item))
    if coverage and not {'input', 'candidate', 'architecture'} <= kinds:
        raise ValueError('full-design structure coverage needs input, candidate and architecture references')
    return accepted


def _completed_job(out, review, job_id, *, success=False):
    if not isinstance(job_id, str) or not ID.fullmatch(job_id):
        raise ValueError('structure evidence must name actual job IDs')
    job = _plain(out, 'jobs/' + job_id)
    try:
        request = json.loads(_plain(job, 'request.json').read_text())
        result = json.loads(_plain(job, 'result.json').read_text())
    except FileNotFoundError:
        raise ValueError('structure evidence job missing or unfinished: ' + job_id) from None
    if (request.get('scope') != review['audit_scope'] or
            request.get('candidate_sha256') != review['candidate_sha256'] or
            result.get('candidate_sha256') != review['candidate_sha256']):
        raise ValueError('structure evidence is not this independent candidate')
    if (type(result.get('exit_code')) is not int or result.get('cancelled') or result.get('timed_out') or result.get('error') or result.get('integrity_error') or
            result.get('sandbox_setup_failed') or result.get('cleanup_complete') is not True or
            result.get('container_running') is not False or
            not isinstance(result.get('isolation'), dict) or
            result['isolation'].get('cleanup_complete') is not True or
            result['isolation'].get('container_running') is not False):
        raise ValueError('structure evidence job did not complete with clean isolation')
    manifest = json.loads((out / 'manifest.json').read_text())
    expected_isolation = manifest.get('job_isolation')
    if (not isinstance(expected_isolation, dict) or request.get('job_isolation') != expected_isolation or
            result.get('job_isolation') != expected_isolation or
            any(result['isolation'].get(name) != value for name, value in expected_isolation.items())):
        raise ValueError('structure job isolation differs from frozen manifest')
    try:
        descriptor = json.loads(_plain(job, 'host-launch.json').read_text())
        intent = json.loads(_plain(job, 'host-launch.intent.json').read_text())
        container = json.loads(_plain(job, 'container.json').read_text())
        finished = json.loads(_plain(job, 'container-finished.json').read_text())
    except FileNotFoundError:
        raise ValueError('structure evidence requires actual host-launched job receipts') from None
    cid = container.get('container_id')
    if (descriptor.get('request_sha256') != sha(job / 'request.json') or
            descriptor.get('manifest_sha256') != sha(out / 'manifest.json') or
            descriptor.get('helper_sha256') != manifest['harness_sha256'].get('runtime/rtl_job_host.py') or
            intent.get('job_id') != job_id or not isinstance(intent.get('nonce'), str) or
            not isinstance(cid, str) or not SHA.fullmatch(cid) or
            finished.get('container_id') != cid or result.get('container_id') != cid or
            container.get('image_id') != expected_isolation.get('image_id') or
            finished.get('image_id') != expected_isolation.get('image_id') or
            finished.get('state', {}).get('Running') is not False or
            finished.get('state', {}).get('Status') != 'exited' or
            finished.get('state', {}).get('ExitCode') != result.get('exit_code')):
        raise ValueError('structure host/container receipt binding differs')
    if success and result.get('exit_code') != 0:
        raise ValueError('structure waiver evidence needs a successful independent job')
    return job, request, result


def _configuration_key(config):
    return digest({name: config[name] for name in sorted(CONFIGURATION_FIELDS)})


def _tool_identity(tool, expected, label):
    if (not isinstance(expected, str) or not Path(expected).is_absolute() or
            not isinstance(tool, dict) or tool.get('path') != expected or
            tool.get('version_exit_code') != 0 or tool.get('version_timed_out') or
            not isinstance(tool.get('binary_sha256'), str) or not SHA.fullmatch(tool['binary_sha256']) or
            tool.get('binary_stable') is not True or tool.get('binary_after_sha256') != tool['binary_sha256'] or
            not any(isinstance(tool.get(name), str) and tool[name].strip()
                    for name in ('version_stdout', 'version_stderr'))):
        raise ValueError(label + ' tool version/binary identity missing')


def _transcript(execution, label):
    if not isinstance(execution, dict) or execution.get('capture_complete') is not True:
        raise ValueError(label + ' tool capture did not complete')
    for stream in ('stdout', 'stderr'):
        try:
            raw = base64.b64decode(execution[stream + '_base64'], validate=True)
        except (KeyError, ValueError, TypeError):
            raise ValueError(label + ' tool transcript is incomplete') from None
        if (execution.get(stream + '_sha256') != hashlib.sha256(raw).hexdigest() or
                execution.get(stream) != raw.decode('utf-8', 'replace')):
            raise ValueError(label + ' tool transcript identity differs')


def _fast_lint(checked, config, tools):
    """Require real same-configuration lint; its findings never replace Yosys."""
    from harness.runtime import rtl_structure
    lint = checked.get('fast_lint')
    if (not isinstance(lint, dict) or lint.get('schema') != 1 or
            lint.get('kind') != 'verilator_full_config_lint' or
            lint.get('config_sha256') != digest(config) or
            lint.get('compile_config_sha256') != _configuration_key(config) or
            lint.get('compile') != {key: config[key] for key in CONFIGURATION_FIELDS} or
            lint.get('completed') is not True or lint.get('status') not in ('passed', 'findings')):
        raise ValueError('fast lint is missing, incomplete or bound to a different configuration')
    if lint.get('profile') != rtl_structure.synthesis_profile(config):
        raise ValueError('fast lint effective macro profile differs from frozen synthesis view')
    _tool_identity(lint.get('tool'), tools.get('verilator'), 'fast lint')
    execution = lint.get('execution')
    _transcript(execution, 'fast lint')
    if (execution.get('timed_out') or type(execution.get('exit_code')) is not int or
            execution['exit_code'] != 0 or not isinstance(execution.get('argv'), list) or
            not execution['argv'] or execution['argv'][0] != config['lint_tool_path']):
        raise ValueError('fast lint execution did not complete successfully')
    argv = execution['argv']
    try:
        if argv.count('--Mdir') != 1:
            raise ValueError()
        directory = Path(argv[argv.index('--Mdir') + 1])
        if not directory.is_absolute() or directory.name != 'obj' or '..' in directory.parts:
            raise ValueError()
        expected_argv = rtl_structure.make_lint_argv(config, directory.parent)
    except (IndexError, TypeError, ValueError):
        raise ValueError('fast lint trusted invocation is incomplete') from None
    if argv != expected_argv:
        raise ValueError('fast lint invocation differs from frozen full configuration')
    try:
        aliases = rtl_structure.include_aliases(config, directory.parent)
    except (OSError, ValueError):
        raise ValueError('trusted Yosys include alias reconstruction failed') from None
    compile_info = checked.get('compile', {})
    if compile_info.get('include_aliases') != aliases or compile_info.get('include_aliases_stable') is not True:
        raise ValueError('trusted Yosys include aliases differ or changed during execution')
    yosys = checked.get('execution')
    if not isinstance(yosys, dict) or yosys.get('argv') != [config['tool_path'], '-s', str(directory.parent / 'check.ys')]:
        raise ValueError('structure Yosys invocation differs from frozen full configuration')
    diagnostics = rtl_structure.parse_lint_diagnostics(execution['stdout'], execution['stderr'], digest(config))
    if lint.get('diagnostics') != diagnostics or diagnostics.get('errors'):
        raise ValueError('fast lint diagnostics differ or contain unresolved tool errors')
    findings = diagnostics.get('findings')
    if not isinstance(findings, list) or (lint['status'] == 'findings') != bool(findings):
        raise ValueError('fast lint status differs from actual diagnostic findings')
    return findings, {'status': lint['status'], 'config_sha256': digest(config),
                      'compile_config_sha256': lint['compile_config_sha256'], 'tool': lint['tool'],
                      'stdout_sha256': execution['stdout_sha256'], 'stderr_sha256': execution['stderr_sha256']}


def native_check(out, review, job_id, *, expected_closure):
    job, request, result = _completed_job(out, review, job_id)
    marker = request.get('structure_check')
    if not isinstance(marker, dict) or marker.get('version') != 2:
        raise ValueError('ordinary submit job cannot replace native structure_check')
    config_path = _plain(job, 'structure-config.json')
    try:
        config = json.loads(config_path.read_text())
    except FileNotFoundError:
        raise ValueError('protected structure configuration missing') from None
    if (marker.get('config_sha256') != digest(config) or
            marker.get('config_file_sha256') != sha(config_path)):
        raise ValueError('native structure configuration changed')
    manifest = json.loads((out / 'manifest.json').read_text())
    tools = manifest.get('settings', {}).get('tools', {})
    runner = out / 'snapshot/harness/runtime/rtl_structure.py'
    expected_binding = {
        'version': 1, 'run_id': out.name, 'scope': review['audit_scope'],
        'candidate_scope': review['candidate_scope'], 'candidate_sha256': review['candidate_sha256'],
        'source_inventory_sha256': digest(expected_closure), 'manifest_sha256': sha(out / 'manifest.json'),
        'input_sha256': manifest['input_sha256'],
        'architecture_identity_sha256': digest(review['architecture_identity']),
    }
    if (config.get('binding') != expected_binding or
            config.get('version') != 2 or
            config.get('source_root') != str((out / review['candidate_scope']).resolve()) or
            config.get('tool_path') != tools.get('yosys') or
            config.get('lint_tool_path') != tools.get('verilator') or
            marker.get('runner_sha256') != sha(runner) or
            marker.get('runner_sha256') != manifest['harness_sha256'].get('runtime/rtl_structure.py') or
            request.get('argv') != [tools.get('python'), str(runner), '--config', str(config_path)]):
        raise ValueError('structure check is not bound to frozen runner/source/configuration')
    try:
        raw = _plain(job, 'stdout.log').read_text()
        checked = json.loads(raw)
    except (FileNotFoundError, UnicodeError, json.JSONDecodeError):
        raise ValueError('structure check has no complete native result') from None
    if (not isinstance(checked, dict) or checked.get('kind') != 'rtl_structure_check' or
            checked.get('schema') != 2 or checked.get('version') != 2):
        raise ValueError('invalid native structure result schema')
    if checked.get('config_sha256') != digest(config) or checked.get('source_stable') is not True or checked.get('binding') != expected_binding:
        raise ValueError('structure result has stale config or changed source')
    compile_info = checked.get('compile', {})
    if (compile_info.get('source_inventory_sha256') != digest(expected_closure) or
            compile_info.get('source_after_inventory_sha256') != digest(expected_closure) or
            request.get('source_sha256') != result.get('source_at_start_sha256') or
            request.get('source_sha256') != result.get('source_after_sha256')):
        raise ValueError('structure source closure changed or differs from candidate')
    if any(compile_info.get(name) != config.get(name) for name in CONFIGURATION_FIELDS):
        raise ValueError('structure result compile configuration differs')
    tool = checked.get('tool', {})
    _tool_identity(tool, tools.get('yosys'), 'structure')
    status = checked.get('status')
    if status not in ('passed', 'findings'):
        raise ValueError('structure check has no usable result: ' + str(status))
    lint_findings, lint_proof = _fast_lint(checked, config, tools)
    execution = checked.get('execution')
    if not isinstance(execution, dict):
        raise ValueError('structure full Yosys execution is missing or incomplete')
    if (execution.get('timed_out') or type(execution.get('exit_code')) is not int or execution['exit_code'] not in (0, 1) or
            type(result.get('exit_code')) is not int or
            result['exit_code'] != (0 if status == 'passed' else 1)):
        raise ValueError('structure native execution status differs')
    from harness.runtime import rtl_structure
    script = checked.get('script', {})
    lint_argv = checked['fast_lint']['execution']['argv']
    private_temp = Path(lint_argv[lint_argv.index('--Mdir') + 1]).parent
    expected_script = rtl_structure.make_script(config, private_temp)
    if script != {'content': expected_script, 'sha256': hashlib.sha256(expected_script.encode()).hexdigest()}:
        raise ValueError('structure check did not use the frozen trusted script')
    _transcript(execution, 'structure')
    try:
        module_inventory = rtl_structure.parse_module_inventory(execution['stdout'])
    except ValueError:
        raise ValueError('structure module inventory is incomplete') from None
    if module_inventory.get('module_inventory_complete') is not True or any(compile_info.get(key) != value for key, value in module_inventory.items()):
        raise ValueError('structure actual module inventory differs')
    diagnostics = checked.get('diagnostics', {})
    if diagnostics != rtl_structure.parse_diagnostics(execution['stdout'], execution['stderr'], digest(config)):
        raise ValueError('structure diagnostic inventory differs from actual transcript')
    findings = diagnostics.get('findings')
    if not isinstance(findings, list) or type(diagnostics.get('unclassified_warning_count')) is not int:
        raise ValueError('structure diagnostic inventory missing')
    if diagnostics['unclassified_warning_count'] or diagnostics.get('errors'):
        raise ValueError('unclassified structural diagnostics cannot be waived')
    found = {}
    for finding in findings + lint_findings:
        if (not isinstance(finding, dict) or not isinstance(finding.get('finding_id'), str) or
                not SHA.fullmatch(finding['finding_id']) or
                finding.get('category') not in ('used_undriven', 'warning') or
                not isinstance(finding.get('message'), str) or not finding['message'].strip()):
            raise ValueError('unsupported structure finding cannot be waived')
        if finding['finding_id'] in found and any(found[finding['finding_id']].get(key) != finding.get(key) for key in ('category', 'message')):
            raise ValueError('conflicting structure finding identity')
        found[finding['finding_id']] = finding
    if (status == 'passed') != (checked.get('ok') is True) or (status == 'passed' and (found or execution['exit_code'] != 0)):
        raise ValueError('structure result claims pass with unresolved diagnostics')
    if execution['exit_code'] == 1 and (not diagnostics.get('check_assert_errors') or not findings):
        raise ValueError('only normal check-assert failure may receive a finding disposition')
    if execution['exit_code'] == 0 and (diagnostics.get('check_assert_errors') or rtl_structure.COMPLETE_MARKER not in execution['stdout'].splitlines()):
        raise ValueError('structure successful tool execution did not complete its check')
    if status == 'findings' and not found:
        raise ValueError('structure findings status has no classified findings')
    return {'job_id': job_id, 'configuration_sha256': digest(config),
            'configuration_key': _configuration_key(config), 'findings': found,
            'module_inventory': module_inventory, 'top': config['top'],
            'stdout_sha256': sha(job / 'stdout.log'), 'config_file_sha256': sha(config_path),
            'status': status, 'tool': tool, 'fast_lint': lint_proof}


def compile_contract(out, review):
    """Read the complete, frozen declaration; its engineering scope is audited."""
    from harness.runtime import rtl_structure
    path = _plain(out, review['candidate_scope'] + '/RTL_STRUCTURE.json')
    try:
        contract = json.loads(path.read_text())
    except FileNotFoundError:
        raise ValueError('pass requires frozen candidate RTL_STRUCTURE.json compile declaration') from None
    if (not isinstance(contract, dict) or set(contract) != {'version', 'configurations', 'blackboxes'} or
            type(contract.get('version')) is not int or contract['version'] != 1):
        raise ValueError('invalid RTL_STRUCTURE.json compile declaration schema')
    configs = contract['configurations']
    if not isinstance(configs, list) or not configs:
        raise ValueError('compile declaration requires all delivery configurations')
    manifest = json.loads((out / 'manifest.json').read_text())
    required = {}; identifiers = set()
    for item in configs:
        if (not isinstance(item, dict) or set(item) - (CONFIGURATION_FIELDS | {'id'}) or
                not isinstance(item.get('id'), str) or not item['id'].strip() or len(item['id']) > 128 or
                item['id'] in identifiers):
            raise ValueError('invalid or duplicate compile declaration configuration')
        normalized = rtl_structure.normalize_config((out / review['candidate_scope']).resolve(), {
            **{key: value for key, value in item.items() if key != 'id'},
            'version': 2, 'tool_path': manifest['settings']['tools']['yosys'],
            'lint_tool_path': manifest['settings']['tools'].get('verilator'),
            'timeout_seconds': 600, 'binding': {}})
        key = _configuration_key(normalized)
        if key in required:
            raise ValueError('duplicate semantic compile declaration configuration')
        required[key] = {'id': item['id'], 'top': normalized['top']}; identifiers.add(item['id'])
    boxes = contract['blackboxes']
    if not isinstance(boxes, list):
        raise ValueError('invalid compile black/white-box declarations')
    allowed = {'blackbox_modules': set(), 'whitebox_modules': set()}
    for item in boxes:
        if (not isinstance(item, dict) or set(item) - {'module', 'kind', 'reason', 'source_refs'} or
                not isinstance(item.get('module'), str) or not item['module'] or
                any(char.isspace() for char in item['module']) or
                not isinstance(item.get('reason'), str) or not item['reason'].strip() or
                item.get('kind', 'blackbox') not in ('blackbox', 'whitebox')):
            raise ValueError('invalid compile black/white-box source declaration')
        source_references(out, review, item.get('source_refs'), coverage=True)
        kind = item.get('kind', 'blackbox') + '_modules'
        if item['module'] in allowed[kind]:
            raise ValueError('duplicate compile black/white-box declaration')
        if item['module'] in {value['top'] for value in required.values()}:
            raise ValueError('full-design structure top cannot itself be a black/white-box')
        allowed[kind].add(item['module'])
    return {'path': '@candidate/RTL_STRUCTURE.json', 'sha256': sha(path), 'required': required,
            'allowed_modules': {key: sorted(value) for key, value in allowed.items()}}


def validate_release(out, review, report):
    """Validate all declared configurations and exact, independently supported waivers."""
    contract = report.get('structure_checks')
    if not isinstance(contract, dict) or contract.get('version') != 1:
        raise ValueError('pass requires source-bound native structure_checks version 1')
    coverage = contract.get('coverage', {})
    if (not isinstance(coverage, dict) or coverage.get('claim') != 'full_design' or
            not isinstance(coverage.get('reason'), str) or not coverage['reason'].strip()):
        raise ValueError('pass requires explicit full-design structure coverage judgment')
    declared = compile_contract(out, review)
    refs = source_references(out, review, coverage.get('source_refs'), coverage=True)
    if {'path': declared['path'], 'sha256': declared['sha256']} not in refs:
        raise ValueError('structure coverage must cite the exact frozen compile declaration')
    required = contract.get('required_configurations')
    if not isinstance(required, list) or not required:
        raise ValueError('pass requires actual structure checks for every required configuration')
    native_jobs = {}
    for job in sorted((out / 'jobs').glob('*')):
        request_path = job / 'request.json'
        if request_path.is_file():
            request = json.loads(request_path.read_text())
            if (request.get('scope') == review['audit_scope'] and
                    request.get('candidate_sha256') == review['candidate_sha256'] and
                    isinstance(request.get('structure_check'), dict)):
                cfg = json.loads(_plain(job, 'structure-config.json').read_text())
                native_jobs[job.name] = _configuration_key(cfg)
    latest = {}
    for job_id, key in sorted(native_jobs.items(), key=lambda item: int(item[0])):
        latest[key] = job_id
    checked = {}; keys = set()
    for item in required:
        if not isinstance(item, dict) or set(item) != {'job_id', 'configuration_sha256'}:
            raise ValueError('invalid required structure configuration')
        job_id = item['job_id']
        if job_id in checked:
            raise ValueError('duplicate required structure check')
        value = native_check(out, review, job_id, expected_closure=review['work_inventory'])
        key = value['configuration_key']
        if item['configuration_sha256'] != value['configuration_sha256']:
            raise ValueError('structure report has stale configuration binding')
        if key in keys or latest.get(key) != job_id:
            raise ValueError('structure report must use latest native check of each configuration')
        if job_id not in report.get('evidence', []):
            raise ValueError('structure job must also appear in independent report evidence')
        checked[job_id] = value; keys.add(key)
    if keys != set(latest):
        raise ValueError('structure report omits a native checked configuration')
    if not set(declared['required']) <= keys:
        raise ValueError('structure report omits a required declared top/configuration')
    actual_boxes = {'blackbox_modules': set(), 'whitebox_modules': set()}
    for value in checked.values():
        for kind in actual_boxes:
            actual_boxes[kind].update(value['module_inventory'][kind])
        if value['top'] in set(value['module_inventory']['blackbox_modules']) | set(value['module_inventory']['whitebox_modules']):
            raise ValueError('full-design structure top cannot itself be a black/white-box')
    if any(sorted(actual_boxes[kind]) != declared['allowed_modules'][kind] for kind in actual_boxes):
        raise ValueError('actual black/white-box modules differ from frozen source-backed declaration')
    waivers = contract.get('waivers', [])
    if not isinstance(waivers, list):
        raise ValueError('invalid structure waivers')
    resolved = set()
    for waiver in waivers:
        if not isinstance(waiver, dict):
            raise ValueError('invalid structure waiver')
        job_id, finding_id = waiver.get('job_id'), waiver.get('finding_id')
        key = (job_id, finding_id)
        if key in resolved or job_id not in checked or finding_id not in checked[job_id]['findings']:
            raise ValueError('structure waiver is duplicate or does not match an exact finding')
        if not isinstance(waiver.get('reason'), str) or not waiver['reason'].strip():
            raise ValueError('structure waiver requires independent engineering reason')
        source_references(out, review, waiver.get('source_refs'))
        evidence = waiver.get('evidence')
        if not isinstance(evidence, list) or not evidence:
            raise ValueError('structure waiver requires actual independent evidence jobs')
        for item in evidence:
            if item in checked or item == job_id:
                raise ValueError('structure check cannot justify its own waiver')
            _completed_job(out, review, item, success=True)
            if item not in report.get('evidence', []):
                raise ValueError('structure waiver job must appear in report evidence')
        resolved.add(key)
    unresolved = {(job_id, finding) for job_id, value in checked.items() for finding in value['findings']} - resolved
    if unresolved:
        raise ValueError('unresolved used-undriven/structural findings prevent pass')
    return {'version': 1, 'scope': 'pre-mapping_structure_only',
            'compile_contract': {'path': declared['path'], 'sha256': declared['sha256'],
                                 'configuration_ids': [value['id'] for value in declared['required'].values()],
                                 'allowed_modules': declared['allowed_modules']},
            'coverage': dict(coverage, source_refs=refs),
            'checks': [{key: value[key] for key in ('job_id', 'configuration_sha256', 'stdout_sha256', 'config_file_sha256', 'status', 'tool', 'fast_lint')}
                       for value in checked.values()], 'waived_findings': len(resolved)}


def _live_lint_summary(job, marker):
    """Bounded development hints from live logs, never completion evidence."""
    path = job / 'stderr.log'
    if not path.is_file():
        return None
    maximum = 512 * 1024
    with path.open('rb') as stream:
        raw = stream.read(maximum + 1)
    text = raw[:maximum].decode('utf-8', 'replace')
    begin = 'HARNESS_RTL_STRUCTURE_FAST_LINT_BEGIN_V2'
    end = 'HARNESS_RTL_STRUCTURE_FAST_LINT_END_V2'
    # A preview's artificial EOF cannot terminate a stage-marker line.
    # Require the trusted writer's visible LF (or CRLF).
    begin_match = re.search(r'(?m)^' + re.escape(begin) + r'\r?\n', text)
    if begin_match is None:
        return None
    stage = text[begin_match.end():]
    end_match = re.search(r'(?m)^' + re.escape(end) + r'\r?\n', stage)
    body = stage[:end_match.start()] if end_match is not None else stage
    from harness.runtime import rtl_structure
    diagnostics = rtl_structure.parse_lint_diagnostics('', body, marker.get('config_sha256'))
    findings = diagnostics.get('findings', [])
    complete = end_match is not None
    truncated = len(raw) > maximum
    observed = {key: len(diagnostics.get(key, [])) for key in ('warnings', 'errors', 'findings')}
    yosys_observed = (complete and re.search(r'(?m)^HARNESS_RTL_STRUCTURE_YOSYS_BEGIN_V2\r?\n',
                                            stage[end_match.end():]) is not None)
    return {'provisional': True, 'acceptance_evidence': False,
            'phase': ('yosys' if yosys_observed else
                      'unknown' if truncated and not complete else 'fast_lint'),
            'log_path': '@run/jobs/' + job.name + '/stderr.log',
            'prefix_truncated': truncated,
            'fast_lint_end_observed': complete,
            'counts_complete': complete,
            'counts': observed if complete else dict.fromkeys(observed),
            'observed_counts': observed,
            'note': ('Complete fast-lint stage is present in this provisional preview.' if complete else
                     'Complete lint totals are unknown. Observed counts are prefix lower bounds; '
                     'continue reading the protected log before claiming no diagnostics.'),
            'findings': [{key: item.get(key)[:600] if isinstance(item.get(key), str) else item.get(key)
                          for key in ('finding_id', 'classification', 'message', 'code')}
                         for item in findings[:8]],
            'errors': [value[:500] for value in diagnostics.get('errors', [])[:3]]}


def job_summary(job, request):
    """Bounded view of protected native output; never release evidence itself."""
    marker = request.get('structure_check')
    if not isinstance(marker, dict):
        return None
    summary = {'version': 2, 'configuration_sha256': marker.get('config_sha256'),
               'report_path': '@run/jobs/' + job.name + '/stdout.log', 'available': False}
    live = _live_lint_summary(job, marker) if marker.get('version') == 2 else None
    if live is not None:
        summary['live_diagnostics'] = live
    path = job / 'stdout.log'
    if not path.is_file():
        summary['status'] = 'pending_result'
        return summary
    try:
        result = json.loads(path.read_text())
    except (UnicodeError, json.JSONDecodeError):
        summary.update(status='invalid_result' if (job / 'result.json').is_file() else 'pending_result',
                       reason='Native output is incomplete or is not one JSON result.')
        return summary
    if (not isinstance(result, dict) or result.get('kind') != 'rtl_structure_check' or
            result.get('schema') != 2 or result.get('version') != 2 or
            result.get('config_sha256') != marker.get('config_sha256')):
        summary.update(status='invalid_result', reason='Native result schema/configuration binding differs.')
        return summary
    def text(value, limit):
        return value[:limit] if isinstance(value, str) else None
    diagnostics = result.get('diagnostics', {})
    if not isinstance(diagnostics, dict):
        diagnostics = {}
    findings = diagnostics.get('findings', [])
    findings = findings if isinstance(findings, list) else []
    samples = []
    for item in findings[:8]:
        if not isinstance(item, dict):
            continue
        samples.append({'finding_id': text(item.get('finding_id'), 64),
                        'category': text(item.get('category'), 40),
                        'message': text(item.get('message'), 600)})
    tool = result.get('tool', {})
    tool = tool if isinstance(tool, dict) else {}
    compiled = result.get('compile', {})
    compiled = compiled if isinstance(compiled, dict) else {}
    count = lambda key: len(diagnostics[key]) if isinstance(diagnostics.get(key), list) else None
    summary.update(available=True, status=text(result.get('status'), 40),
                   top=text(compiled.get('top'), 160), reason=text(result.get('reason'), 800),
                   source_stable=result.get('source_stable') is True,
                   counts={key: count(key) for key in ('warnings', 'errors', 'check_assert_errors', 'undriven', 'findings')},
                   unclassified_warning_count=diagnostics.get('unclassified_warning_count') if type(diagnostics.get('unclassified_warning_count')) is int else None,
                   findings=samples, findings_truncated=len(findings) > len(samples),
                   errors=[text(value, 500) for value in diagnostics.get('errors', [])[:3]] if isinstance(diagnostics.get('errors'), list) else [],
                   tool={'path': text(tool.get('path'), 240),
                         'version': text(tool.get('version_stdout') or tool.get('version_stderr'), 240),
                         'binary_sha256': text(tool.get('binary_sha256'), 64)})
    if result.get('execution') is None:
        # This is only a non-authorizing view. An absent Yosys execution has
        # unknown structural totals, not evidence of zero findings or a pass.
        summary.update(counts={key: None for key in ('warnings', 'errors', 'check_assert_errors', 'undriven', 'findings')},
                       unclassified_warning_count=None,
                       yosys={'status': 'not_run', 'coverage': 'unclosed'})
    lint = result.get('fast_lint')
    if isinstance(lint, dict):
        detail = lint.get('diagnostics', {})
        detail = detail if isinstance(detail, dict) else {}
        lint_findings = detail.get('findings', [])
        lint_findings = lint_findings if isinstance(lint_findings, list) else []
        summary['fast_lint'] = {
            'status': text(lint.get('status'), 40), 'completed': lint.get('completed') is True,
            'counts': {key: len(detail[key]) if isinstance(detail.get(key), list) else None
                       for key in ('warnings', 'errors', 'findings', 'other_warnings')},
            'findings': [{key: text(item.get(key), 600) for key in ('finding_id', 'classification', 'message', 'code')}
                         for item in lint_findings[:8] if isinstance(item, dict)],
            'errors': [text(value, 500) for value in detail.get('errors', [])[:3]] if isinstance(detail.get('errors'), list) else []}
    return summary
