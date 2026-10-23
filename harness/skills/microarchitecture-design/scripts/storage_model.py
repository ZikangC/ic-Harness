"""Derive storage demand over declared integer domains and live phases.

This checks declarations, not source coverage, address placement, bank conflicts,
or whether a proposed lifetime is achievable by the actual schedule.
"""
import ast
import itertools
import math


MAX_SCENARIOS = 4096
SCOPE = (
    'Exact enumeration of the projection of validated integer domains onto all '
    'declared numeric capacity expressions, across every static declared live '
    'phase, with per-object representation, copies and padding. Unreferenced '
    'parameters cannot change these declared capacity expressions. This does '
    'not prove source-domain coverage, actual lifetimes, address layout, ports or '
    'physical implementation.'
)


def _integer(value, label, minimum=None):
    if type(value) not in (int, float) or not math.isfinite(value) or value != int(value):
        raise ValueError(label + ' must evaluate to an integer')
    result = int(value)
    if minimum is not None and result < minimum:
        raise ValueError(label + ' must be >= ' + str(minimum))
    return result


def _identifier(value, label):
    if not isinstance(value, str) or not value:
        raise ValueError(label + ' must be a nonempty string')
    return value


def _basis(row, label):
    if not isinstance(row.get('basis'), str) or not row['basis'].strip():
        raise ValueError(label + ' needs a nonempty basis')


def _parameter_domains(requirements, values):
    """Only cited input fact IDs define domains; runtime IDs never shadow facts."""
    rows = requirements.get('runtime_parameters', [])
    if not isinstance(rows, list):
        raise ValueError('runtime_parameters must be an array')
    facts = {row.get('id') for row in requirements.get('facts', []) if isinstance(row, dict)}
    domains = {}
    total = 1

    def fact_value(identifier):
        if not isinstance(identifier, str) or identifier not in facts or identifier not in values:
            raise ValueError('runtime domain value must reference an input fact ID')
        return _integer(values[identifier], 'domain fact ' + identifier)

    for row in rows:
        if not isinstance(row, dict) or set(row) != {'id', 'domain', 'basis', 'citations'}:
            raise ValueError('runtime parameter requires exactly id/domain/basis/citations')
        identifier = _identifier(row['id'], 'runtime parameter id')
        if not identifier.isidentifier():
            raise ValueError('runtime parameter id must be an expression identifier')
        if identifier in domains or identifier in values or identifier in facts:
            raise ValueError('runtime parameter ID duplicates or overwrites a fact/choice: ' + identifier)
        _basis(row, 'runtime parameter ' + identifier)
        if not isinstance(row['citations'], list) or not row['citations']:
            raise ValueError('runtime parameter needs original-input citations: ' + identifier)
        if any(not isinstance(c, dict) for c in row['citations']):
            raise ValueError('runtime parameter citations must be objects: ' + identifier)
        domain = row['domain']
        if not isinstance(domain, dict):
            raise ValueError('runtime parameter domain must be an object: ' + identifier)
        if domain.get('kind') == 'range' and set(domain) == {'kind', 'lower', 'upper'}:
            lower, upper = fact_value(domain['lower']), fact_value(domain['upper'])
            if upper < lower:
                raise ValueError('runtime domain range is reversed: ' + identifier)
            count = upper - lower + 1
            members = range(lower, upper + 1)
        elif domain.get('kind') == 'set' and set(domain) == {'kind', 'values'}:
            if not isinstance(domain['values'], list) or not domain['values']:
                raise ValueError('runtime domain set needs a nonempty fact ID array: ' + identifier)
            members = [fact_value(f) for f in domain['values']]
            if len(members) != len(set(members)):
                raise ValueError('runtime domain set has duplicate values: ' + identifier)
            count = len(members)
        else:
            raise ValueError('runtime domain must be range(lower/upper fact IDs) or set(values fact IDs): ' + identifier)
        domains[identifier] = members
        total *= count
    return domains, total


def _expression_names(value):
    """Collect declared numeric dependencies; do not infer from parameter names."""
    class Names(ast.NodeVisitor):
        def __init__(self):self.names=set()
        def visit_Name(self,node):self.names.add(node.id)
        def visit_Call(self,node):
            if not isinstance(node.func,ast.Name) or node.func.id not in ('ceil','floor','min','max','abs') or node.keywords:
                raise ValueError('unsupported capacity expression call; dependencies cannot be projected')
            for argument in node.args:self.visit(argument)
    visitor=Names();visitor.visit(ast.parse(str(value),mode='eval'))
    return visitor.names


def derive(candidate, values, requirements, expression, inventory):
    """Return fixed storage arithmetic without mutating any input object.

    ``expression(value, names)`` is the host's safe arithmetic evaluator.
    ``inventory`` maps actual macro names to bits and optionally kind.
    A valid declaration with too many scenarios returns ok=True plus unknown;
    unknown blocks release and must never be interpreted as a capacity pass.
    """
    errors, unknown, buffers, macro_peaks = [], [], [], {}
    seen_errors = set()
    projection={'declared_domains':{},'relevant_domains':{},'omitted_domains':{},
                'declared_scenario_count':None,'scenario_count':None}

    def error(message, parameters=None):
        if message not in seen_errors:
            seen_errors.add(message)
            suffix = ('; example parameters=' + repr(parameters)) if parameters else ''
            errors.append(message + suffix)

    def result(checked):
        return {'ok': not errors, 'errors': errors, 'unknown': unknown,
                'buffers': buffers, 'macro_live_peaks': list(macro_peaks.values()),
                'scenarios_checked': checked, 'parameter_projection':projection,'scope': SCOPE}

    if not isinstance(candidate, dict) or not isinstance(values, dict) or not isinstance(requirements, dict) or not isinstance(inventory, dict):
        error('storage derivation requires candidate/values/requirements/inventory objects')
        return result(0)
    try:
        domains, scenario_count = _parameter_domains(requirements, values)
        sizes={name:members.stop-members.start if isinstance(members,range) else len(members)
               for name,members in domains.items()}
        projection['declared_domains']=dict(sizes)
        projection['declared_scenario_count']=scenario_count
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        error('storage runtime_parameters: ' + str(exc))
        return result(0)

    phase_rows = candidate.get('storage_phases')
    phases = []
    if not isinstance(phase_rows, list):
        error('storage_phases must be an array')
    else:
        for phase in phase_rows:
            try:
                if not isinstance(phase, dict) or set(phase) != {'id', 'basis'}:
                    raise ValueError('phase requires exactly id/basis')
                identifier = _identifier(phase['id'], 'phase id')
                _basis(phase, 'phase ' + identifier)
                if identifier in phases:
                    raise ValueError('duplicate storage phase: ' + identifier)
                phases.append(identifier)
            except (KeyError, TypeError, ValueError) as exc:
                error('storage phase: ' + str(exc))

    rows = candidate.get('buffers')
    if not isinstance(rows, list):
        error('buffers must be an array')
        return result(0)
    if rows and not phases:
        error('nonempty buffers require declared storage phases')
    prepared = []
    buffer_ids, object_ids = set(), set()
    macro_kinds = {m.get('name'): m.get('kind') for m in candidate.get('macros', []) if isinstance(m, dict)}
    for row in rows:
        try:
            if not isinstance(row, dict):
                raise ValueError('buffer must be an object')
            if 'required_bits' in row or 'allocated_bits' in row:
                raise ValueError('required_bits/allocated_bits are program outputs; omit them')
            allowed = {'id', 'kind', 'count', 'depth', 'width', 'storage_reference', 'basis', 'objects'}
            if set(row) - allowed or not (allowed - {'storage_reference'}) <= set(row):
                raise ValueError('buffer needs id/kind/count/depth/width/basis/objects and optional register storage_reference')
            identifier = _identifier(row['id'], 'buffer id')
            if identifier in buffer_ids:
                raise ValueError('duplicate buffer id: ' + identifier)
            buffer_ids.add(identifier)
            _basis(row, 'buffer ' + identifier)
            if row['kind'] not in ('register', 'SRAM', 'ROM'):
                raise ValueError('buffer kind must be register|SRAM|ROM')
            dimensions = {key: _integer(expression(row[key], values), 'buffer ' + key, 1)
                          for key in ('count', 'depth', 'width')}
            allocation = dimensions['count'] * dimensions['depth'] * dimensions['width']
            reference = row.get('storage_reference')
            if row['kind'] == 'register':
                if reference is not None:
                    raise ValueError('register buffer has no macro storage_reference')
            else:
                if not isinstance(reference, str) or reference not in inventory:
                    raise ValueError('buffer storage_reference must name an actual macro')
                macro = inventory[reference]
                macro_bits = _integer(macro['bits'], 'macro inventory bits', 1)
                if macro.get('kind', macro_kinds.get(reference)) != row['kind']:
                    raise ValueError('buffer and referenced macro kinds disagree')
                if allocation > macro_bits:
                    raise ValueError('individual buffer allocation exceeds referenced macro')
                macro_peaks.setdefault(reference, {
                    'storage_reference': reference, 'required_bits': None,
                    'allocated_bits': macro_bits, 'peak_phase': None,
                    'peak_parameters': None, 'buffers': []})
            if not isinstance(row['objects'], list):
                raise ValueError('buffer objects must be an array')
            objects = []
            for obj in row['objects']:
                if not isinstance(obj, dict) or set(obj) != {'id', 'shape', 'element_bits', 'copies', 'alignment_bits', 'metadata_bits', 'live_in', 'basis'}:
                    raise ValueError('object needs exactly id/shape/element_bits/copies/alignment_bits/metadata_bits/live_in/basis')
                oid = _identifier(obj['id'], 'storage object id')
                if oid in object_ids:
                    raise ValueError('storage object id must be globally unique: ' + oid)
                object_ids.add(oid)
                _basis(obj, 'object ' + oid)
                if not isinstance(obj['shape'], list):
                    raise ValueError('object shape must be an array; [] denotes a scalar')
                live = obj['live_in']
                if not isinstance(live, list) or not live or any(not isinstance(p, str) for p in live):
                    raise ValueError('object live_in must be a nonempty phase ID array')
                if len(live) != len(set(live)) or any(p not in phases for p in live):
                    raise ValueError('object live_in must contain unique declared phase IDs')
                objects.append(obj)
            output = {'id': identifier, 'kind': row['kind'], 'storage_reference': reference,
                      **dimensions, 'allocated_bits': allocation, 'required_bits': None,
                      'peak_phase': None, 'peak_parameters': None, 'object_sizes': []}
            buffers.append(output)
            prepared.append((row, objects, output))
        except (KeyError, TypeError, ValueError, SyntaxError, ZeroDivisionError, OverflowError) as exc:
            error('storage buffer/' + str(row.get('id', '?') if isinstance(row, dict) else '?') + ': ' + str(exc))
    if errors:
        return result(0)
    try:
        dependencies=set()
        for row,objects,output in prepared:
            for obj in objects:
                expressions=list(obj['shape'])+[obj[key] for key in ('element_bits','copies','alignment_bits','metadata_bits')]
                for value in expressions:dependencies.update(_expression_names(value))
        missing=dependencies-set(domains)-set(values)
        if missing:raise ValueError('undefined capacity identifiers: '+', '.join(sorted(missing)))
        names=[name for name in domains if name in dependencies]
        projection['relevant_domains']={name:sizes[name] for name in names}
        projection['omitted_domains']={name:sizes[name] for name in domains if name not in dependencies}
        scenario_count=math.prod(sizes[name] for name in names)
        projection['scenario_count']=scenario_count
    except (KeyError,TypeError,ValueError,SyntaxError,OverflowError) as exc:
        error('storage capacity dependencies: '+str(exc))
        return result(0)
    if scenario_count > MAX_SCENARIOS:
        unknown.append('storage/runtime_parameters: ' + str(scenario_count) +
                       ' combinations exceed exact-enumeration limit ' + str(MAX_SCENARIOS))
        return result(0)

    checked = 0
    for members in itertools.product(*(domains[n] for n in names)):
        parameters = dict(zip(names, members))
        scenario_values = {**values, **parameters}
        sizes_by_buffer = {}
        valid = True
        for row, objects, output in prepared:
            sizes = []
            for obj in objects:
                try:
                    shape = [_integer(expression(v, scenario_values), 'object shape dimension', 0) for v in obj['shape']]
                    element = _integer(expression(obj['element_bits'], scenario_values), 'object element_bits', 1)
                    copies = _integer(expression(obj['copies'], scenario_values), 'object copies', 1)
                    alignment = _integer(expression(obj['alignment_bits'], scenario_values), 'object alignment_bits', 1)
                    metadata = _integer(expression(obj['metadata_bits'], scenario_values), 'object metadata_bits', 0)
                    payload = math.prod(shape) * element
                    before_padding = payload + metadata
                    per_copy = ((before_padding + alignment - 1) // alignment) * alignment
                    sizes.append({'id': obj['id'], 'shape': shape, 'element_bits': element,
                                  'copies': copies, 'alignment_bits': alignment,
                                  'metadata_bits': metadata, 'payload_bits': payload,
                                  'padded_bits_per_copy': per_copy, 'bits': per_copy * copies,
                                  'live_in': list(obj['live_in'])})
                except (KeyError, TypeError, ValueError, SyntaxError, ZeroDivisionError, OverflowError) as exc:
                    valid = False
                    error('storage object/' + obj['id'] + ': ' + str(exc), parameters)
            sizes_by_buffer[row['id']] = sizes
        checked += 1
        if not valid:
            continue
        for phase in phases:
            macro_live = {ref: [] for ref in macro_peaks}
            for row, objects, output in prepared:
                sizes = sizes_by_buffer[row['id']]
                live_bits = sum(obj['bits'] for obj in sizes if phase in obj['live_in'])
                if output['required_bits'] is None or live_bits > output['required_bits']:
                    output.update(required_bits=live_bits, peak_phase=phase,
                                  peak_parameters=dict(parameters),
                                  object_sizes=[{**obj, 'live_at_peak': phase in obj['live_in']} for obj in sizes])
                if row['kind'] != 'register':
                    macro_live[row['storage_reference']].append({'id': row['id'], 'live_bits': live_bits})
            for reference, demands in macro_live.items():
                total = sum(d['live_bits'] for d in demands)
                peak = macro_peaks[reference]
                if peak['required_bits'] is None or total > peak['required_bits']:
                    peak.update(required_bits=total, peak_phase=phase,
                                peak_parameters=dict(parameters), buffers=demands)
    for output in buffers:
        if output['required_bits'] is not None and output['required_bits'] > output['allocated_bits']:
            error('storage buffer/' + output['id'] + ': maximum simultaneous live data ' +
                  str(output['required_bits']) + ' bits exceeds allocation ' + str(output['allocated_bits']) +
                  ' bits at phase ' + str(output['peak_phase']), output['peak_parameters'])
    for reference, peak in macro_peaks.items():
        if peak['required_bits'] is not None and peak['required_bits'] > peak['allocated_bits']:
            error('storage macro/' + reference + ': simultaneous aliased buffer demand ' +
                  str(peak['required_bits']) + ' bits exceeds physical inventory ' + str(peak['allocated_bits']) +
                  ' bits at phase ' + str(peak['peak_phase']), peak['peak_parameters'])
    return result(checked)
