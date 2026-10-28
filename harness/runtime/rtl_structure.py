#!/usr/bin/env python3
"""Trusted early RTL structure check, without synthesis mapping or ABC.

The worker owns configuration, job metadata and admission.  This executor owns
the generated Yosys script and reports the actual tool transcript.  A clean
result is deliberately narrower than functional or synthesis sign-off.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import sys
import tempfile
import time

VERSION = 2
KIND = 'rtl_structure_check'
IDENTIFIER = re.compile(r'[A-Za-z_][A-Za-z0-9_$]*\Z')
CONSTANT = re.compile(r"[-+]?(?:[0-9]+|(?:[0-9]+)?'[sS]?[bBoOdDhH][0-9a-fA-FxXzZ?_]+)\Z")
MACRO_VALUE = re.compile(r"[A-Za-z0-9_$'.:+*/()<>!&|^~?%=-]+\Z")
CHECK_ERROR = re.compile(r"ERROR: Found [1-9][0-9]* problems? in 'check -assert'\.\Z")
COMPLETE_MARKER = 'HARNESS_RTL_STRUCTURE_CHECK_COMPLETED_V1'
MODULE_MARKERS = {
    kind: ('HARNESS_RTL_STRUCTURE_' + kind.upper() + '_BEGIN_V1',
           'HARNESS_RTL_STRUCTURE_' + kind.upper() + '_END_V1')
    for kind in ('blackbox', 'whitebox')
}
STAGE_MARKERS = {
    name: ('HARNESS_RTL_STRUCTURE_' + name + '_BEGIN_V2',
           'HARNESS_RTL_STRUCTURE_' + name + '_END_V2')
    for name in ('FAST_LINT', 'YOSYS')
}
YOSYS_TIMING_PHASES = ('read', 'hierarchy', 'proc', 'check')
TIMING_OBSERVATION_PREFIX = 'HARNESS_RTL_TIMING_OBSERVATION_V1 '
LINT_TARGET_CODES = frozenset(('UNDRIVEN', 'MULTIDRIVEN', 'LATCH'))
# Exact defaults documented by Verilator's V3PreShell; remove them before
# applying the declared synthesis profile. This is not a global macro reset.
VERILATOR_DEFAULT_MACROS = (
    'VERILATOR', 'verilator', 'verilator3', 'SYSTEMVERILOG', 'coverage_block_off',
    'SV_COV_START', 'SV_COV_STOP', 'SV_COV_RESET', 'SV_COV_CHECK',
    'SV_COV_MODULE', 'SV_COV_HIER', 'SV_COV_ASSERTION', 'SV_COV_FSM_STATE',
    'SV_COV_STATEMENT', 'SV_COV_TOGGLE', 'SV_COV_OVERFLOW', 'SV_COV_ERROR',
    'SV_COV_NOCOV', 'SV_COV_OK', 'SV_COV_PARTIAL',
)


class UnsupportedConfiguration(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def byte_sha(value):
    return hashlib.sha256(value).hexdigest()


def file_sha(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def _timings():
    return {'schema': 1, 'clock': 'monotonic_wall',
            'clock_resolution_seconds': time.get_clock_info('monotonic').resolution,
            'scope': 'executor only; worker admission, queueing and remote log reads are not measured'}


def _measure(timings, name, function, *args):
    begin = time.monotonic()
    try:
        return function(*args)
    finally:
        timings[name] = time.monotonic() - begin
        _forward_stderr(('\n' + TIMING_OBSERVATION_PREFIX + json.dumps(
            {'measurement': name, 'duration_seconds': timings[name], 'clock': 'monotonic_wall'},
            sort_keys=True) + '\n').encode())


def yosys_timing_markers(private_temp):
    # Bind observation lines deterministically to this private scratch path.
    # The release gate reconstructs the exact script; timing is not acceptance.
    nonce = byte_sha(str(private_temp).encode())[:24]
    return {name: tuple('HARNESS_RTL_TIMING_' + name.upper() + '_' + edge + '_' + nonce
                        for edge in ('BEGIN', 'END')) for name in YOSYS_TIMING_PHASES}


class _ObservedYosysTiming:
    """Observe complete stdout marker lines; never infer a tool CPU duration.

    Pipe/tool buffering can put several markers in one read. Their observed
    separation may then be zero and has no known error bound. Missing end lines
    remain incomplete; this metadata never authorizes structural acceptance.
    """
    def __init__(self, markers, begin):
        self.begin = begin
        self.expected = [(marker.encode(), name, edge)
                         for name, pair in markers.items() for edge, marker in enumerate(pair)]
        self.known = {marker: (name, edge) for marker, name, edge in self.expected}
        self.maximum = max(len(marker) for marker in self.known) + 1
        self.pending = b''
        self.discarding = False
        self.events = []
        self.valid = True

    def feed(self, raw, observed):
        if self.discarding:
            boundary = raw.find(b'\n')
            if boundary < 0: return
            raw = raw[boundary + 1:]
            self.discarding = False
        lines = (self.pending + raw).split(b'\n')
        for line in lines[:-1]:
            if line.endswith(b'\r'): line = line[:-1]
            if line not in self.known: continue
            index = len(self.events)
            if index >= len(self.expected) or line != self.expected[index][0]:
                self.valid = False
            name, edge = self.known[line]
            self.events.append({'phase': name, 'edge': 'begin' if edge == 0 else 'end',
                                'observed_seconds': observed - self.begin})
        self.pending = lines[-1]
        if len(self.pending) > self.maximum:
            self.pending = b''
            self.discarding = True

    def result(self):
        phases = {}
        for name in YOSYS_TIMING_PHASES:
            selected = [event for event in self.events if event['phase'] == name]
            complete = self.valid and len(selected) == 2 and [event['edge'] for event in selected] == ['begin', 'end']
            phases[name] = {'complete': complete,
                            'observed_duration_seconds': (selected[1]['observed_seconds'] - selected[0]['observed_seconds']) if complete else None}
        return {'kind': 'stdout_marker_receive_time', 'clock': 'monotonic_wall',
                'markers_valid': self.valid,
                'complete': self.valid and len(self.events) == len(self.expected),
                'events': self.events, 'phases': phases,
                'clock_resolution_seconds': time.get_clock_info('monotonic').resolution,
                'selector_poll_seconds': .05, 'maximum_read_bytes': 65536,
                'precision_limit': 'Receive-time observation only, including tool/pipe buffering and scheduling; no exact command wall/CPU duration or error bound. Incomplete/non-newline EOF markers are not completed phases.',
                'acceptance_effect': 'none'}


def closure_inventory(base):
    """Same typed source boundary as rtl_worker.closure_inventory."""
    base = Path(base).resolve(strict=True)
    result = {}
    for path in sorted(base.rglob('*')):
        name = str(path.relative_to(base))
        if path.is_symlink():
            target = path.resolve(strict=True)
            try:
                relative = str(target.relative_to(base))
            except ValueError as exc:
                raise ValueError('build closure has external symlink: ' + name) from exc
            result[name] = {'type': 'symlink', 'target': relative}
        elif path.is_file():
            result[name] = {'type': 'file', 'sha256': file_sha(path), 'mode': path.stat().st_mode & 0o777}
        elif path.is_dir():
            result[name] = {'type': 'directory', 'mode': path.stat().st_mode & 0o777}
        else:
            raise ValueError('unsupported build closure entry: ' + name)
    return result


def _relative(root, value, directory=False):
    if not isinstance(value, str) or not value or any(c in value for c in '\x00\r\n'):
        raise ValueError('invalid source-relative path')
    path = Path(value)
    if path.is_absolute() or str(path) != value or '..' in path.parts:
        raise ValueError('non-canonical source-relative path: ' + value)
    resolved = (root / path).resolve(strict=True)
    resolved.relative_to(root)
    if directory and not resolved.is_dir():
        raise ValueError('include path is not a directory: ' + value)
    if not directory and not resolved.is_file():
        raise ValueError('source is not a regular file: ' + value)
    return value


def normalize_config(source_root, spec):
    if not isinstance(spec, dict):
        raise ValueError('structure configuration must be an object')
    allowed = {'version', 'source_root', 'top', 'sources', 'include_dirs', 'defines',
               'parameters', 'tool_path', 'lint_tool_path', 'timeout_seconds', 'binding',
               'execution_plan'}
    if set(spec) - allowed:
        raise ValueError('unsupported structure configuration fields: ' + ', '.join(sorted(set(spec) - allowed)))
    if spec.get('version', VERSION) != VERSION:
        raise ValueError('unsupported structure configuration version')
    if not isinstance(source_root, (str, Path)) or not Path(source_root).is_absolute():
        raise ValueError('source_root must be absolute')
    root = Path(source_root).resolve(strict=True)
    if not root.is_dir() or root == Path('/') or any(c in str(root) for c in '\x00\r\n'):
        raise ValueError('invalid source_root')
    if 'source_root' in spec and Path(spec['source_root']).resolve(strict=True) != root:
        raise ValueError('conflicting source_root')
    top = spec.get('top')
    if not isinstance(top, str) or not IDENTIFIER.fullmatch(top):
        raise ValueError('unsupported top module identifier')
    sources = spec.get('sources')
    if not isinstance(sources, list) or not sources or len(sources) != len(set(sources)):
        raise ValueError('sources must be a nonempty ordered unique path list')
    sources = [_relative(root, value) for value in sources]
    includes = spec.get('include_dirs', [])
    if not isinstance(includes, list) or len(includes) != len(set(includes)):
        raise ValueError('include_dirs must be a unique path list')
    includes = [_relative(root, value, directory=True) for value in includes]
    defines = spec.get('defines', {})
    if not isinstance(defines, dict):
        raise ValueError('defines must be an object')
    for name, value in defines.items():
        if not isinstance(name, str) or not IDENTIFIER.fullmatch(name):
            raise ValueError('unsupported define name')
        if value is not None and (not isinstance(value, str) or not MACRO_VALUE.fullmatch(value)):
            raise ValueError('unsupported define value: ' + name)
    for intrinsic in ('SYNTHESIS', 'YOSYS'):
        if intrinsic in defines and defines[intrinsic] != '1':
            # Yosys's synthesis/global defaults can override these -D values.
            # Preserve that frontend semantics; do not silently pretend a
            # conflicting requested value was honored by both frontends.
            raise UnsupportedConfiguration('unsupported synthesis profile: ' + intrinsic + ' must be 1')
    parameters = spec.get('parameters', {})
    if not isinstance(parameters, dict):
        raise ValueError('parameters must be an object')
    for name, value in parameters.items():
        if not isinstance(name, str) or not IDENTIFIER.fullmatch(name):
            raise ValueError('unsupported parameter name')
        if not isinstance(value, str) or not CONSTANT.fullmatch(value):
            raise ValueError('parameter requires an explicit Verilog numeric constant: ' + name)
    tool = spec.get('tool_path')
    if not isinstance(tool, str) or not Path(tool).is_absolute() or any(c in tool for c in '\x00\r\n'):
        raise ValueError('tool_path must be an absolute configured tool')
    lint_tool = spec.get('lint_tool_path')
    if not isinstance(lint_tool, str) or not Path(lint_tool).is_absolute() or any(c in lint_tool for c in '\x00\r\n'):
        raise ValueError('lint_tool_path must be an absolute configured tool')
    timeout = spec.get('timeout_seconds')
    if type(timeout) is not int or timeout <= 0:
        raise ValueError('timeout_seconds must be a positive integer')
    binding = spec.get('binding', {})
    if not isinstance(binding, dict):
        raise ValueError('binding must be an object')
    # Prove it is JSON data, rather than accepting objects with custom encoders.
    binding = json.loads(json.dumps(binding, sort_keys=True))
    normalized = {'version': VERSION, 'source_root': str(root), 'top': top, 'sources': sources,
                  'include_dirs': includes, 'defines': dict(defines), 'parameters': dict(parameters),
                  'tool_path': tool, 'lint_tool_path': lint_tool,
                  'timeout_seconds': timeout, 'binding': binding}
    # Omission preserves the old configuration bytes/identity and complete path.
    # Only an explicit, source-bound diagnostic request may stop after findings.
    if 'execution_plan' in spec:
        if spec['execution_plan'] not in ('complete', 'diagnostic_first'):
            raise ValueError('unsupported structure execution_plan')
        normalized['execution_plan'] = spec['execution_plan']
    return normalized


def _quote(value):
    # Yosys command tokens, never shell interpolation.  Newlines are forbidden.
    if any(c in value for c in '\x00\r\n'):
        raise ValueError('invalid Yosys command token')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def include_aliases(config, private_temp):
    """Deterministic aliases in trusted private scratch, never in the source."""
    if not config['include_dirs']: return []
    if private_temp is None:
        raise ValueError('Yosys include directories require trusted private temporary aliases')
    temporary = Path(private_temp)
    if not temporary.is_absolute() or not re.fullmatch(r'/[A-Za-z0-9_./-]+', str(temporary)) or '..' in temporary.parts:
        raise ValueError('private include alias directory requires an absolute token-safe path')
    root = Path(config['source_root']).resolve()
    aliases = []
    for index, name in enumerate(config['include_dirs']):
        target = (root / name).resolve()
        target.relative_to(root)
        aliases.append({'include_dir': name, 'path': str(temporary / ('include-' + str(index))),
                        'target': str(target)})
    return aliases


def _verify_aliases(aliases):
    for item in aliases:
        alias = Path(item['path'])
        if (not alias.is_symlink() or os.readlink(alias) != item['target']
                or str(alias.resolve(strict=True)) != item['target'] or not alias.is_dir()):
            raise ValueError('trusted include alias changed during structure check')


def make_script(config, private_temp=None):
    root = Path(config['source_root'])
    flags = ['-sv', '-noautowire']
    # Yosys option arguments do not use the filename unquoting path. Preserve
    # original directories (including spaces) through contained temporary links
    # with token-safe names; never chmod a link or its source-directory target.
    for item in include_aliases(config, private_temp):
        flags.append('-I' + item['path'])
    for name, value in sorted(synthesis_profile(config)['effective_defines'].items()):
        # IDENTIFIER/MACRO_VALUE exclude whitespace, quotes used by the Yosys
        # command tokenizer, semicolons and newlines. No shell is involved.
        flags.append('-D' + name + ('' if value is None else '=' + value))
    # One command preserves the caller's ordered translation-unit list.
    timing = yosys_timing_markers(private_temp)
    commands = ['log ' + timing['read'][0],
                'read_verilog ' + ' '.join(flags + [_quote(str(root / name)) for name in config['sources']]),
                'log ' + timing['read'][1]]
    hierarchy = ['hierarchy', '-check', '-top', config['top']]
    for name, value in sorted(config['parameters'].items()):
        hierarchy.extend(['-chparam', name, value])
    # proc's default final opt_expr can rewrite X/undriven expressions before
    # check sees them.  Do not silently fall back if this option is unavailable.
    commands += ['log ' + timing['hierarchy'][0], ' '.join(hierarchy), 'log ' + timing['hierarchy'][1]]
    for kind, (begin, end) in MODULE_MARKERS.items():
        # '=' is essential: ordinary selections exclude black/white boxes.
        commands += ['log ' + begin, 'select -list-mod =A:' + kind, 'log ' + end]
    commands += ['select -clear', 'log ' + timing['proc'][0], 'proc -noopt',
                 'log ' + timing['proc'][1], 'log ' + timing['check'][0], 'check -assert',
                 'log ' + timing['check'][1], 'log ' + COMPLETE_MARKER]
    return '\n'.join(commands) + '\n'


def parse_module_inventory(stdout):
    """Parse fixed actual Yosys selections, never infer modules from source text."""
    lines = [line.strip() for line in stdout.splitlines()]
    result = {}
    for kind, (begin, end) in MODULE_MARKERS.items():
        if lines.count(begin) != 1 or lines.count(end) != 1:
            raise ValueError('missing or duplicate actual ' + kind + ' inventory markers')
        first, last = lines.index(begin), lines.index(end)
        if last <= first:
            raise ValueError('invalid actual module inventory order')
        modules = []
        for line in lines[first + 1:last]:
            if not line:
                continue
            if any(char.isspace() for char in line) or line.endswith(':'):
                raise ValueError('unrecognized actual module inventory output')
            # RTLIL print names may include the identifier escape prefix.
            name = line[1:] if line.startswith('\\') else line
            if not name or name in modules:
                raise ValueError('invalid or duplicate actual module inventory name')
            modules.append(name)
        result[kind + '_modules'] = sorted(modules)
    if lines.index(MODULE_MARKERS['blackbox'][1]) >= lines.index(MODULE_MARKERS['whitebox'][0]):
        raise ValueError('overlapping actual module inventory regions')
    result['module_inventory_complete'] = True
    return result


def parse_diagnostics(stdout, stderr, config_sha256):
    warnings, errors, check_errors, findings = [], [], [], []
    # Preserve every original diagnostic line; repeated bit warnings remain distinct.
    for stream_name, content in (('stdout', stdout), ('stderr', stderr)):
        for line in content.splitlines():
            stripped = line.strip()
            if re.match(r'(?i)^warning\s*:', stripped):
                category = 'used_undriven' if 'used but has no driver' in stripped else 'warning'
                if category == 'used_undriven':
                    classification = 'used_undriven'
                elif re.fullmatch(r'Warning: Replacing memory .+ with list of registers\.(?: See .+)?', stripped):
                    # Known frontend lowering, still unresolved until an exact
                    # independent disposition; this is never an automatic pass.
                    classification = 'memory_lowering'
                else:
                    classification = 'unclassified_warning'
                warnings.append(line)
            elif re.match(r'(?i)^error\s*:', stripped):
                # The terminal check summary is not an additional unknown tool
                # failure or a separately waivable bit.  Preserve it verbatim.
                (check_errors if CHECK_ERROR.fullmatch(stripped) else errors).append(line)
                continue
            else:
                continue
            identity = {'config_sha256': config_sha256, 'category': category, 'message': line}
            findings.append({'finding_id': digest(identity), 'category': category,
                             'classification': classification, 'message': line, 'stream': stream_name})
    return {'warnings': warnings, 'errors': errors, 'check_assert_errors': check_errors,
            'undriven': [f['message'] for f in findings if f['category'] == 'used_undriven'],
            'unclassified_warning_count': sum(f['classification'] == 'unclassified_warning' for f in findings),
            'findings': findings}


def compile_configuration(config):
    return {key: config[key] for key in ('top', 'sources', 'include_dirs', 'defines', 'parameters')}


def synthesis_profile(config):
    effective = {'SYNTHESIS': '1', 'YOSYS': '1'}
    effective.update(config['defines'])
    return {'name': 'yosys_synthesis_v1', 'effective_defines': effective,
            'verilator_undefines': list(VERILATOR_DEFAULT_MACROS),
            'limits': 'Declared macros plus the listed tool defaults only; not universal intrinsic-macro equivalence.'}


def make_lint_argv(config, temporary):
    """Same declared full top, ordered files and overrides; never shell text."""
    root = Path(config['source_root'])
    argv = [config['lint_tool_path'], '--lint-only', '--timing', '--top-module', config['top'],
            '-Wno-fatal', '-Wwarn-UNDRIVEN', '-Wwarn-MULTIDRIVEN', '-Wwarn-LATCH',
            '--Mdir', str(Path(temporary) / 'obj')]
    argv += ['-U' + name for name in VERILATOR_DEFAULT_MACROS]
    argv += ['-I' + str(root / name) for name in config['include_dirs']]
    argv += ['-D' + name + ('' if value is None else '=' + value)
             for name, value in sorted(synthesis_profile(config)['effective_defines'].items())]
    argv += ['-G' + name + '=' + value for name, value in sorted(config['parameters'].items())]
    return argv + [str(root / name) for name in config['sources']]


def parse_lint_diagnostics(stdout, stderr, config_sha256):
    """Retain complete Verilator diagnostic blocks without guessing bit use.

    A Verilator UNDRIVEN warning is an early diagnostic, not proof that every
    reported bit is used. Full Yosys checking and an exact independent
    disposition remain necessary, including for a legitimate macro interface.
    """
    warnings, errors, other, findings = [], [], [], []
    header = re.compile(r'^%(Warning|Error)(?:-([A-Za-z0-9_]+))?:')
    for stream_name, content in (('stdout', stdout), ('stderr', stderr)):
        blocks = []
        current = None
        for line in content.splitlines():
            match = header.match(line.strip())
            if match:
                if current is not None: blocks.append(current)
                current = [match.group(1), match.group(2), [line]]
            elif current is not None:
                current[2].append(line)
        if current is not None: blocks.append(current)
        for level, code, lines in blocks:
            message = '\n'.join(lines).rstrip('\n')
            if level == 'Error':
                errors.append(message)
            else:
                warnings.append(message)
                if code in LINT_TARGET_CODES:
                    identity = {'config_sha256': config_sha256, 'tool': 'verilator',
                                'code': code, 'message': message}
                    findings.append({'finding_id': digest(identity), 'category': 'warning',
                                     'classification': 'verilator_' + code, 'message': message,
                                     'stream': stream_name, 'code': code, 'tool': 'verilator'})
                else:
                    other.append(message)
    return {'warnings': warnings, 'errors': errors, 'other_warnings': other, 'findings': findings}


def _comments_removed(text):
    """Small lexer for dependency policy only, not an RTL correctness checker."""
    out, pos, state = [], 0, 'normal'
    while pos < len(text):
        char = text[pos]
        pair = text[pos:pos + 2]
        if state == 'line':
            if char == '\n':
                state = 'normal'; out.append(char)
            else:
                out.append(' ')
        elif state == 'block':
            if pair == '*/':
                out.extend('  '); pos += 1; state = 'normal'
            else:
                out.append('\n' if char == '\n' else ' ')
        elif state == 'string':
            out.append(char)
            if char == '\\' and pos + 1 < len(text):
                pos += 1; out.append(text[pos])
            elif char == '"':
                state = 'normal'
        elif pair == '//':
            out.extend('  '); pos += 1; state = 'line'
        elif pair == '/*':
            out.extend('  '); pos += 1; state = 'block'
        else:
            out.append(char)
            if char == '"': state = 'string'
        pos += 1
    return ''.join(out)


def check_dependency_policy(config):
    """Reject unbounded file dependencies instead of claiming a closed check.

    Direct literal include/readmem inputs are supported within source_root.
    Macro-computed filenames and escaped/dynamic filenames require a future
    trusted frontend integration and are explicitly unsupported here.
    """
    root = Path(config['source_root'])
    pending, seen = list(config['sources']), set()
    include_roots = [root] + [root / name for name in config['include_dirs']]
    references = []
    while pending:
        name = pending.pop(0)
        if name in seen: continue
        seen.add(name)
        path = root / name
        text = _comments_removed(path.read_text(encoding='utf-8'))
        intrinsic_name = r'((?:VERILATOR_|YOSYS_|__VERILATOR|__VLT|SV_COV_)[A-Za-z0-9_]*)'
        for match in re.finditer(r'`(?:ifdef|ifndef|elsif)\s+' + intrinsic_name + r'|`' + intrinsic_name, text):
            intrinsic = match.group(1) or match.group(2)
            if intrinsic not in VERILATOR_DEFAULT_MACROS and intrinsic not in config['defines']:
                raise ValueError('unsupported tool-specific macro branch in ' + name + ': ' + intrinsic)
        # Track strings, so text such as "$readmemh(...)" is not interpreted.
        tokens = re.compile(r'"(?:\\.|[^"\\])*"|`include\b|\$readmem[hb]\s*\(')
        for match in tokens.finditer(text):
            if match.group().startswith('"'): continue
            tail = text[match.end():].lstrip()
            literal = re.match(r'"([^"\\\r\n]+)"', tail)
            if not literal:
                raise ValueError('unsupported nonliteral file dependency in ' + name)
            target = literal.group(1)
            if Path(target).is_absolute():
                raise ValueError('unsupported absolute file dependency in ' + name)
            roots = [path.parent] + include_roots
            found = None
            for directory in roots:
                candidate = (directory / target).resolve()
                try: candidate.relative_to(root)
                except ValueError:
                    raise ValueError('external file dependency in ' + name)
                if candidate.is_file():
                    found = candidate; break
            if found is None:
                raise ValueError('unresolved file dependency in ' + name + ': ' + target)
            relative = str(found.relative_to(root))
            references.append({'from': name, 'path': relative,
                               'kind': 'include' if match.group().startswith('`') else 'readmem'})
            if match.group().startswith('`'): pending.append(relative)
    return references


def _forward_stderr(raw):
    """Flush already emitted tool bytes to the host-owned stderr log."""
    if not raw: return
    stream = getattr(sys.stderr, 'buffer', None)
    if stream is not None:
        stream.write(raw); stream.flush()
    else:
        sys.stderr.write(raw.decode('utf-8', 'replace')); sys.stderr.flush()


def _stage(name, begin):
    _forward_stderr(('\n' + STAGE_MARKERS[name][0 if begin else 1] + '\n').encode())


def _execution(argv, timeout, cwd, *, timing_markers=None):
    """Capture both original streams while forwarding every received byte.

    Final stdout remains a single native JSON document. Stage markers and live
    diagnostics belong only to runner stderr; neither is release evidence by
    itself. The caller's native job deadline is not extended per tool phase.
    """
    begin = time.monotonic()
    observation = _ObservedYosysTiming(timing_markers, begin) if timing_markers is not None else None
    chunks = {'stdout': [], 'stderr': []}
    if timeout <= 0:
        return _execution_result(argv, None, True, begin, chunks, True, observation)
    process = subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    selector = selectors.DefaultSelector()
    for name in chunks:
        pipe = getattr(process, name)
        os.set_blocking(pipe.fileno(), False)
        selector.register(pipe, selectors.EVENT_READ, name)
    deadline, drain_deadline, timed_out, complete = begin + timeout, None, False, True
    try:
        while selector.get_map():
            current = time.monotonic()
            if not timed_out and current >= deadline:
                timed_out = True
                if process.poll() is None: process.kill()
                drain_deadline = current + 2
            if timed_out and current >= drain_deadline:
                complete = False
                break
            for key, _ in selector.select(.05):
                try: raw = os.read(key.fd, 65536)
                except BlockingIOError: continue
                if raw:
                    observed = time.monotonic()
                    chunks[key.data].append(raw)
                    _forward_stderr(raw)
                    if observation is not None and key.data == 'stdout':
                        previous = len(observation.events)
                        observation.feed(raw, observed)
                        for event in observation.events[previous:]:
                            _forward_stderr(('\n' + TIMING_OBSERVATION_PREFIX + json.dumps(
                                dict(event, markers_valid=observation.valid,
                                     kind='stdout_marker_receive_time', acceptance_effect='none'),
                                sort_keys=True) + '\n').encode())
                else:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
        remaining = max(.1, (drain_deadline if timed_out else deadline) - time.monotonic())
        try: code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill(); code = process.wait(timeout=2)
    except BaseException:
        # Self-cancellation cannot make a PASS. Already flushed diagnostics
        # remain in the server's protected stderr log, even without final JSON.
        if process.poll() is None: process.kill()
        try: process.wait(timeout=2)
        except subprocess.TimeoutExpired: pass
        raise
    finally:
        selector.close()
        for name in chunks:
            pipe = getattr(process, name)
            if pipe is not None: pipe.close()
    return _execution_result(argv, code, timed_out, begin, chunks, complete, observation)


def _execution_result(argv, code, timed_out, begin, chunks, complete, observation=None):
    stdout, stderr = b''.join(chunks['stdout']), b''.join(chunks['stderr'])
    result = {'argv': argv, 'exit_code': code, 'timed_out': timed_out,
            'capture_complete': complete, 'duration_seconds': time.monotonic() - begin,
            'stdout': stdout.decode('utf-8', 'replace'), 'stderr': stderr.decode('utf-8', 'replace'),
            'stdout_base64': base64.b64encode(stdout).decode('ascii'),
            'stderr_base64': base64.b64encode(stderr).decode('ascii'),
            'stdout_sha256': byte_sha(stdout), 'stderr_sha256': byte_sha(stderr)}
    if observation is not None: result['observed_phases'] = observation.result()
    return result


def _tool_version(tool_info, flag, deadline, temporary):
    tool = Path(tool_info['path'])
    timings = tool_info['identity_timings'] = _timings()
    tool_info.update({'resolved_path': str(tool.resolve(strict=True)),
                      'binary_sha256': _measure(timings, 'binary_hash_before_seconds', file_sha, tool)})
    version = _execution([str(tool), flag], min(30, deadline - time.monotonic()), temporary)
    timings['version_query_seconds'] = version['duration_seconds']
    tool_info.update({'version_stdout': version['stdout'], 'version_stderr': version['stderr'],
                      'version_exit_code': version['exit_code'], 'version_timed_out': version['timed_out']})
    return (version['exit_code'] == 0 and not version['timed_out']
            and version['capture_complete'] and bool(version['stdout'].strip()))


def _fast_lint(config, result, deadline, temporary):
    fast = result['fast_lint']
    _stage('FAST_LINT', True)
    try:
        if not _tool_version(fast['tool'], '--version', deadline, temporary):
            fast['status'], result['status'], result['reason'] = 'tool_failure', 'tool_failure', 'lint tool version query failed'
            return False
        actual = _execution(make_lint_argv(config, temporary), deadline - time.monotonic(), config['source_root'])
        result['timings']['fast_lint_seconds'] = actual['duration_seconds']
        fast['execution'] = actual
        fast['diagnostics'] = parse_lint_diagnostics(actual['stdout'], actual['stderr'], result['config_sha256'])
        if actual['timed_out']:
            fast['status'], result['status'] = 'timed_out', 'timed_out'
        elif not actual['capture_complete'] or actual['exit_code'] not in (0, 1):
            fast['status'], result['status'], result['reason'] = 'tool_failure', 'tool_failure', 'abnormal or incomplete lint execution'
        elif fast['diagnostics']['errors']:
            fast['status'], result['status'], result['reason'] = 'unsupported', 'unsupported', 'full-configuration lint did not compile; original diagnostics retained'
        elif actual['exit_code'] != 0:
            fast['status'], result['status'], result['reason'] = 'tool_failure', 'tool_failure', 'nonzero lint exit without a recognized compiler diagnostic'
        else:
            fast['completed'] = True
            fast['status'] = 'findings' if fast['diagnostics']['findings'] else 'passed'
            return True
        return False
    finally:
        _stage('FAST_LINT', False)


def _yosys(config, result, deadline, temporary):
    _stage('YOSYS', True)
    try:
        script_path = Path(temporary) / 'check.ys'
        script_path.write_text(result['script']['content'], encoding='utf-8')
        if not _tool_version(result['tool'], '-V', deadline, temporary):
            result['status'], result['reason'] = 'tool_failure', 'tool version query failed'
            return
        actual = _execution([config['tool_path'], '-s', str(script_path)], deadline - time.monotonic(),
                            config['source_root'], timing_markers=yosys_timing_markers(temporary))
        result['timings']['yosys_execution_seconds'] = actual['duration_seconds']
        result['timings']['yosys_phases'] = actual.get('observed_phases')
        result['execution'] = actual
        diagnostic = parse_diagnostics(actual['stdout'], actual['stderr'], result['config_sha256'])
        result['diagnostics'] = diagnostic
        try: result['compile'].update(parse_module_inventory(actual['stdout']))
        except ValueError as exc: result['compile']['module_inventory_error'] = str(exc)
        if actual['timed_out']:
            result['status'] = 'timed_out'
        elif not actual['capture_complete'] or actual['exit_code'] not in (0, 1):
            result['status'], result['reason'] = 'tool_failure', 'abnormal or incomplete tool exit'
        elif diagnostic['errors']:
            result['status'] = 'tool_failure'
        elif not result['compile']['module_inventory_complete']:
            result['status'], result['reason'] = 'tool_failure', 'actual module inventory missing'
        elif actual['exit_code'] == 1 and not (diagnostic['check_assert_errors'] and diagnostic['findings']):
            result['status'] = 'tool_failure'
        elif actual['exit_code'] == 0 and (diagnostic['check_assert_errors'] or COMPLETE_MARKER not in actual['stdout'].splitlines()):
            result['status'], result['reason'] = 'tool_failure', 'completed check marker missing or contradictory check error'
        elif diagnostic['warnings'] or result['fast_lint']['diagnostics']['findings']:
            result['status'] = 'findings'
        elif actual['exit_code'] == 0:
            result['status'], result['ok'] = 'passed', True
        else:
            result['status'], result['reason'] = 'tool_failure', 'completed check marker missing'
    finally:
        _stage('YOSYS', False)


def run_config(config):
    timings = _timings()
    try:
        config = _measure(timings, 'normalize_seconds', normalize_config, config.get('source_root'), config)
    except UnsupportedConfiguration as exc:
        return {'schema': VERSION, 'version': VERSION, 'kind': KIND, 'ok': False,
                'status': 'unsupported', 'reason': str(exc), 'execution': None, 'source_stable': False,
                'timings': timings}
    config_sha256 = digest(config)
    compile_info = compile_configuration(config)
    result = {'schema': VERSION, 'version': VERSION, 'kind': KIND, 'ok': False, 'status': 'invalid',
              'config_sha256': config_sha256, 'binding': config['binding'], 'timings': timings,
              'script': {'content': None, 'sha256': None},
              'tool': {'path': config['tool_path']}, 'execution': None, 'compile': dict(compile_info),
              'diagnostics': {'warnings': [], 'errors': [], 'check_assert_errors': [], 'undriven': [], 'unclassified_warning_count': 0, 'findings': []},
              'source_stable': False,
              'fast_lint': {'schema': 1, 'kind': 'verilator_full_config_lint', 'config_sha256': config_sha256,
                            'compile_config_sha256': digest(compile_info), 'compile': dict(compile_info),
                            'profile': synthesis_profile(config),
                            'completed': False, 'status': 'not_run', 'tool': {'path': config['lint_tool_path']},
                            'execution': None, 'diagnostics': {'warnings': [], 'errors': [], 'other_warnings': [], 'findings': []}}}
    result['compile'].update({'blackbox_modules': [], 'whitebox_modules': [], 'module_inventory_complete': False})
    before = _measure(timings, 'source_inventory_before_seconds', closure_inventory, config['source_root'])
    result['compile']['source_inventory_sha256'] = digest(before)
    result['compile']['source_files_sha256'] = {name: entry['sha256'] for name, entry in before.items() if entry['type'] == 'file'}
    expected = config['binding'].get('source_inventory_sha256')
    if expected is not None and expected != digest(before):
        result['reason'] = 'source inventory does not match trusted binding'
        return result
    try:
        result['compile']['file_dependencies'] = _measure(timings, 'dependency_policy_seconds', check_dependency_policy, config)
    except (ValueError, UnicodeError) as exc:
        result['status'], result['reason'] = 'unsupported', str(exc)
        result['source_stable'] = before == _measure(timings, 'source_inventory_after_seconds', closure_inventory, config['source_root'])
        return result
    deadline = time.monotonic() + config['timeout_seconds']
    try:
        # The job owns a private /tmp. Do not inherit a possibly whitespace-
        # containing TMPDIR when constructing tokens for the Yosys parser.
        with tempfile.TemporaryDirectory(prefix='harness-rtl-structure-', dir='/tmp') as directory:
            temporary = str(Path(directory).resolve(strict=True))
            aliases = include_aliases(config, temporary)
            for item in aliases:
                Path(item['path']).symlink_to(item['target'], target_is_directory=True)
            _verify_aliases(aliases)
            result['compile']['include_aliases'] = aliases
            result['compile']['include_aliases_stable'] = False
            script = make_script(config, temporary)
            result['script'] = {'content': script, 'sha256': byte_sha(script.encode())}
            try:
                if _fast_lint(config, result, deadline, temporary):
                    if (config.get('execution_plan') == 'diagnostic_first'
                            and result['fast_lint']['diagnostics']['findings']):
                        # Negative evidence only. Preserve the complete raw lint
                        # transcript, then finish all ordinary integrity checks.
                        # This cannot satisfy the unchanged full-result gate.
                        result.update(status='findings', reason=(
                            'Completed full-configuration lint has target diagnostics; '
                            'diagnostic_first stopped before Yosys. Bitwise coverage remains unclosed; '
                            'inspect and repair or request execution_plan complete for disposition.'))
                        result['coverage'] = {'kind': 'diagnostic_only', 'fast_lint': 'complete',
                                              'bitwise': 'unclosed', 'yosys': 'not_run',
                                              'structural_release': False}
                    else:
                        _yosys(config, result, deadline, temporary)
            finally:
                try:
                    _verify_aliases(aliases)
                    result['compile']['include_aliases_stable'] = True
                except (OSError, ValueError) as exc:
                    result['status'], result['ok'], result['reason'] = 'invalid', False, str(exc)
    except (OSError, ValueError) as exc:
        result['status'], result['ok'], result['reason'] = 'tool_failure', False, str(exc)
    after = _measure(timings, 'source_inventory_after_seconds', closure_inventory, config['source_root'])
    result['source_stable'] = before == after
    result['compile']['source_after_inventory_sha256'] = digest(after)
    if not result['source_stable']:
        result['status'], result['ok'], result['reason'] = 'invalid', False, 'source tree changed during check'
    for tool_info in (result['tool'], result['fast_lint']['tool']):
        if tool_info.get('binary_sha256'):
            try:
                tool_info['binary_after_sha256'] = _measure(tool_info.setdefault('identity_timings', _timings()),
                                                           'binary_hash_after_seconds', file_sha, tool_info['path'])
                tool_info['binary_stable'] = tool_info['binary_sha256'] == tool_info['binary_after_sha256']
            except OSError: tool_info['binary_stable'] = False
            if not tool_info['binary_stable']:
                result['status'], result['ok'], result['reason'] = 'invalid', False, 'tool binary changed during check'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    try:
        config = json.loads(Path(args.config).read_text(encoding='utf-8'))
        result = run_config(config)
    except Exception as exc:
        result = {'schema': VERSION, 'version': VERSION, 'kind': KIND, 'status': 'invalid', 'ok': False,
                  'reason': type(exc).__name__ + ': ' + str(exc)}
    print(json.dumps(result, sort_keys=True, ensure_ascii=True))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
