"""Source-only typed witnesses. Shapes/scopes are interpreted, never inferred."""
import argparse
import ast
import copy
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys


# Technical execution-size guard, not a limit on source work or hardware size.
# Primitive validation and aggregation visit each declared step linearly.
MAX_PRIMITIVE_STEPS = 4096


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def load_engine(path):
    spec = importlib.util.spec_from_file_location('audit_shared_calculator', path)
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    return engine



def _evidence_module():
    spec = importlib.util.spec_from_file_location('audit_source_scope', Path(__file__).with_name('validate_review.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def references(rows, sources, label, bindings, original_only=True, citations=()):
    # Keep semantic choice with the auditor. The host only binds the chosen span.
    spec = importlib.util.spec_from_file_location('audit_source_evidence', Path(__file__).with_name('validate_review.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    if not isinstance(rows, list) or not rows:
        raise ValueError(label + ' needs original-input references')
    for ref in rows:
        bound = module.resolve_citation(ref, sources, citations, input_only=original_only)
        errors = []
        # Reuse the fixed original-source quote validation, including ambiguity.
        if not isinstance(bound, dict) or (original_only and not module.is_original_source(bound.get('file'))) or bound.get('file') not in sources:
            raise ValueError(label + ' reference is outside original inputs')
        quote = bound.get('quote'); text = sources[bound['file']]
        if not isinstance(quote, str) or not quote or quote not in text:
            raise ValueError(label + ' needs an exact original-input quote')
        if text.count(quote) != 1:
            a, b = bound.get('start'), bound.get('end', bound.get('start'))
            if quote not in module.source_window(text, a, b):
                errors.append('ambiguous excerpt needs correct bounds')
        if errors: raise ValueError(label + ': ' + '; '.join(errors))
        bindings.append({'location': label, 'reference': copy.deepcopy(ref), 'resolved': bound})


def shape(values, facts, engine):
    if not isinstance(values, list) or not values or len(values) > 8:
        raise ValueError('shape needs 1..8 dimensions')
    dims = [engine.expression(v, facts) for v in values]
    if any(v <= 0 or v != int(v) for v in dims):
        raise ValueError('shape dimensions must be positive integers')
    return [int(v) for v in dims]


def primitive(step, facts, engine):
    """Operation formulas are fixed here, not supplied as an arbitrary total."""
    if not isinstance(step, dict): raise ValueError('step must be an object')
    kind = step.get('kind')
    if kind in ('gemv','dot'):
        fields={'id','kind','input_elements','output_elements'} if kind=='gemv' else {'id','kind','width'}
        if set(step)!=fields:raise ValueError(kind+' requires exactly '+','.join(sorted(fields)))
        def extent(key):
            value=engine.expression(step[key],facts)
            if value<=0 or value!=int(value):raise ValueError(key+' must be a positive integer extent')
            return int(value)
        inner=extent('input_elements' if kind=='gemv' else 'width')
        rows=extent('output_elements') if kind=='gemv' else 1
        return {'operations':inner*rows,'unit':'MAC','input_elements':inner,'output_elements':rows}
    if kind == 'matmul':
        if set(step) != {'id', 'kind', 'lhs', 'rhs'}:
            raise ValueError('matmul step needs id/kind/lhs/rhs only')
        lhs, rhs = shape(step['lhs'], facts, engine), shape(step['rhs'], facts, engine)
        if len(lhs) > 2 or len(rhs) > 2:
            raise ValueError('matmul witness uses rank 1/2; put batches in scope multiplicity')
        if lhs[-1] != rhs[0]: raise ValueError('matmul inner dimensions disagree')
        m = lhs[0] if len(lhs) == 2 else 1
        n = rhs[1] if len(rhs) == 2 else 1
        return {'operations': m * lhs[-1] * n, 'unit': 'MAC', 'lhs': lhs, 'rhs': rhs}
    if kind == 'elementwise':
        if set(step) != {'id', 'kind', 'shape', 'ops_per_element'}:
            raise ValueError('elementwise step needs id/kind/shape/ops_per_element only')
        dims = shape(step['shape'], facts, engine)
        per = engine.expression(step['ops_per_element'], facts)
        if per < 1 or per != int(per): raise ValueError('ops_per_element must be a positive integer')
        return {'operations': math.prod(dims) * int(per), 'unit': 'ops', 'shape': dims}
    if kind == 'reduction':
        if set(step) != {'id', 'kind', 'shape', 'axis'}:
            raise ValueError('reduction step needs id/kind/shape/axis only')
        dims = shape(step['shape'], facts, engine); axis = step['axis']
        if type(axis) is not int or not 0 <= axis < len(dims): raise ValueError('invalid reduction axis')
        return {'operations': math.prod(dims) // dims[axis] * (dims[axis] - 1),
                'unit': 'ops', 'shape': dims, 'axis': axis}
    raise ValueError('supported source primitive kinds: matmul|elementwise|reduction; explain unsupported semantics in limits')


def _source_script_files(directory):
    return {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(directory.rglob('*')) if path.is_file()
            and '__pycache__' not in path.parts and path.suffix != '.pyc' and path.name != '.DS_Store'}


def source_execution_binding(model, sources, engine):
    sources = _evidence_module().original_sources(sources, strict=True)
    calculator = _source_script_files(Path(engine.__file__).parent)
    checker = _source_script_files(Path(__file__).parent)
    return {'source_sha256s': {key: hashlib.sha256(value.encode()).hexdigest() for key, value in sources.items()},
            'model_sha256': fingerprint(model),
            'calculator_files_sha256': calculator, 'calculator_bundle_sha256': fingerprint(calculator),
            'checker_files_sha256': checker, 'checker_bundle_sha256': fingerprint(checker)}


def _source_dependencies(row):
    """Names only; arithmetic and dimensional rules remain with the shared engine."""
    class Names(ast.NodeVisitor):
        def __init__(self): self.names = set()
        def visit_Name(self, node): self.names.add(node.id)
        def visit_Call(self, node):
            for argument in node.args: self.visit(argument)
            for argument in node.keywords: self.visit(argument.value)
    dependencies = set()
    if isinstance(row, dict) and 'formula' in row:
        names = Names(); names.visit(ast.parse(str(row['formula']), mode='eval'))
        dependencies.update(names.names)
    for key in ('convert', 'converted_from'):
        if isinstance(row, dict) and isinstance(row.get(key), dict) and isinstance(row[key].get('fact'), str):
            dependencies.add(row[key]['fact'])
    return dependencies


def _source_row_dependencies(row):
    """Track only declared numeric recipe fields; scope axes are labels."""
    expressions = list(row.get('shape', []))
    steps = row.get('steps', [])
    if not isinstance(steps, list): raise ValueError('steps must be an array')
    for step in steps:
        if not isinstance(step, dict): raise ValueError('step must be an object')
        for field in ('lhs', 'rhs', 'shape'): expressions.extend(step.get(field, []))
        for field in ('input_elements', 'output_elements', 'width', 'ops_per_element'):
            if field in step: expressions.append(step[field])
    fields = row.get('fields') or []
    if not isinstance(fields, list): raise ValueError('fields must be an array')
    for field in fields:
        if not isinstance(field, dict): raise ValueError('field must be an object')
        expressions.extend(field.get('shape', []))
        if 'element_bits' in field: expressions.append(field['element_bits'])
    if isinstance(row.get('scope'), dict):
        expressions.extend(row['scope'][key] for key in ('multiplicity', 'first', 'step') if key in row['scope'])
    dependencies = set()
    for expression in expressions: dependencies.update(_source_dependencies({'formula': expression}))
    return dependencies - {'workload_elements'}


def analyze(model, sources, engine):
    """Commit source declaration transactions with their exact typed outputs.

    Partial source interpretation can support a bound local counterexample. It
    cannot establish a complete baseline, cache eligibility or design acceptance.
    """
    errors, bindings, values = [], [], {}
    result = {'ok': False, 'errors': errors, 'values': values, 'calculation_ids': [],
              'workloads': {}, 'workload_units': {}, 'computations': {}, 'states': {}, 'flows': {},
              'fact_values': {}, 'fact_units': {}, 'source_bindings': bindings,
              'subreceipts': [], 'missing_declarations': [], 'coverage_complete': False,
              'execution_status': 'failed', 'model_sha256': fingerprint(model),
              'scope': 'fixed algebra over auditor-selected original-source declarations; successful local receipts survive independent failures, but are not a Python compiler, semantic completeness proof, architecture or universal operation lower bound'}
    binding = None
    exception_types = (KeyError, TypeError, ValueError, SyntaxError, ZeroDivisionError, OverflowError)

    def record(section, index, declaration, *, key=None, workload=None, dependencies=(),
               output=None, numbers=None, local_bindings=(), failure=None):
        rid = 'source/' + section + '/' + str(index)
        if workload is not None: rid += '/' + str(workload)
        receipt = {'id': rid, 'section': section,
                   'status': 'declaration_failed' if failure else 'executed',
                   'errors': [failure] if failure else [], 'checks': [], 'observations': [],
                   'values': {}, 'calculation_ids': [], 'citation_bindings': [],
                   'binding': binding, 'declaration_sha256': fingerprint(declaration),
                   'applicability': {'section': section, 'declaration_id': key,
                                     'dependencies': sorted(set(dependencies))}}
        if workload is not None: receipt['applicability']['workload'] = workload
        if failure:
            errors.append(failure)
            missing = {'collection': section, 'key': key, 'index': index, 'reason': failure}
            if workload is not None: missing['workload'] = workload
            result['missing_declarations'].append(missing)
        else:
            receipt['output'] = copy.deepcopy(output)
            receipt['values'] = {rid + '::' + name: value for name, value in (numbers or {}).items()}
            if any(type(value) not in (int, float) or not math.isfinite(value) for value in receipt['values'].values()):
                raise ValueError('source receipt contains nonfinite or nonnumeric calculation')
            receipt['calculation_ids'] = list(receipt['values'])
            receipt['citation_bindings'] = copy.deepcopy(list(local_bindings))
            values.update(receipt['values']); bindings.extend(receipt['citation_bindings'])
        receipt['receipt_sha256'] = fingerprint(receipt)
        result['subreceipts'].append(receipt)
        return receipt

    def collection(name):
        rows = model.get(name)
        if not isinstance(rows, list) or len(rows) > 200:
            record(name, 'declaration', rows, failure=name + ' must be a bounded array')
            return []
        return rows

    def identifier(row, section, duplicates):
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id'].strip() or row['id'] in duplicates:
            raise ValueError(section + ' requires a unique nonempty id within its collection')
        if not isinstance(row.get('basis'), str) or not row['basis'].strip(): raise ValueError('basis required')
        local = []; references(row.get('references'), sources, section + '/' + row['id'], local)
        return local

    def duplicate_ids(rows):
        counts = {}
        for row in rows:
            name = row.get('id') if isinstance(row, dict) else None
            if isinstance(name, str): counts[name] = counts.get(name, 0) + 1
        return {key for key, count in counts.items() if count > 1}

    try:
        sources = _evidence_module().original_sources(sources, strict=True)
        binding = source_execution_binding(model, sources, engine); result['binding'] = binding
        required = {'facts', 'workloads', 'computations', 'states', 'flows', 'limits'}
        if not isinstance(model, dict): raise ValueError('analysis requires an object')
        result['ignored_annotation_fields'] = sorted(set(model) - required)
        limits = model.get('limits')
        if not ((isinstance(limits, str) and limits.strip()) or
                (isinstance(limits, list) and limits and all(isinstance(x, str) and x.strip() for x in limits))):
            record('limits', 'declaration', limits, failure='analysis limits must be nonempty text or a nonempty list of textual limitations')
        all_rows = {name: collection(name) for name in ('facts', 'workloads', 'computations', 'states', 'flows')}
        for name in ('workloads', 'computations'):
            if not all_rows[name]:
                record(name, 'required', all_rows[name], failure='nonempty ' + name + ' required')

        # Validate each fact against successful dependency declarations only.
        # Shared validate_requirements may return a value alongside an error;
        # such a value is never committed here or used by downstream rows.
        facts = result['fact_values']; successful = []
        raw_facts = all_rows['facts']; duplicates = duplicate_ids(raw_facts)
        ordered, _ = engine.dependency_order(raw_facts)
        original_indices = {}
        for index, row in enumerate(raw_facts): original_indices.setdefault(id(row), []).append(index)
        citation_spec = importlib.util.spec_from_file_location('source_fact_citations', Path(__file__).with_name('validate_review.py'))
        citation_module = importlib.util.module_from_spec(citation_spec); citation_spec.loader.exec_module(citation_module)
        for raw in ordered:
            index = original_indices[id(raw)].pop(0); key = raw.get('id') if isinstance(raw, dict) else None
            local, dependencies = [], set()
            try:
                if not isinstance(key, str) or not key.isidentifier() or key in duplicates:
                    raise ValueError('fact requires a unique identifier string')
                dependencies = _source_dependencies(raw)
                absent = dependencies - set(facts)
                if absent: raise ValueError('fact depends on failed or unresolved declarations: ' + ', '.join(sorted(absent)))
                declaration = copy.deepcopy(raw)
                if 'citations' in declaration and not isinstance(declaration['citations'], list):
                    raise ValueError('fact citations must be an array')
                for ref_index, ref in enumerate(declaration.get('citations', [])):
                    resolved = citation_module.resolve_citation(ref, sources, (), input_only=True)
                    local.append({'location': 'fact/' + key + '/citations/' + str(ref_index),
                                  'reference': copy.deepcopy(ref), 'resolved': resolved})
                    declaration['citations'][ref_index] = resolved
                request = {'facts': successful + [declaration], 'constraints': [], 'runtime_parameters': []}
                prepared = engine.prepare_requirements(request, sources)
                computed, failed = engine.validate_requirements(prepared['requirements'], sources)
                problems = list(dict.fromkeys(prepared['errors'] + failed))
                if problems: raise ValueError('; '.join(problems))
                if key not in computed: raise ValueError('fact did not produce a validated value')
                materialized = next(row for row in prepared['requirements']['facts'] if row['id'] == key)
                local.extend(item for item in prepared['bindings'] if item.get('fact') == key)
                value, unit = computed[key], materialized.get('unit')
                record('fact', index, raw, key=key, dependencies=dependencies,
                       output={'collection': 'facts', 'key': key, 'data': {'value': value, 'unit': unit}},
                       numbers={key + '__value': value}, local_bindings=local)
                facts[key] = value; result['fact_units'][key] = unit; successful.append(declaration)
            except exception_types as error:
                record('fact', index, raw, key=key, dependencies=dependencies,
                       failure='fact/' + str(key) + ': ' + str(error))

        workload_rows = {}; duplicates = duplicate_ids(all_rows['workloads'])
        for index, row in enumerate(all_rows['workloads']):
            key = row.get('id') if isinstance(row, dict) else None
            try:
                local = identifier(row, 'workload', duplicates)
                n = engine.expression(row['quantity'], facts)
                if n <= 0 or n != int(n) or not isinstance(row.get('unit'), str) or not row['unit'].strip():
                    raise ValueError('positive integer quantity and unit required')
                dependencies = _source_dependencies({'formula': row['quantity']})
                record('workload', index, row, key=key, dependencies=dependencies,
                       output={'collection': 'workloads', 'key': key, 'data': {'quantity': int(n), 'unit': row['unit']}},
                       numbers={key + '__quantity': int(n)}, local_bindings=local)
                result['workloads'][key] = int(n); result['workload_units'][key] = row['unit']; workload_rows[key] = row
            except exception_types as error:
                record('workload', index, row, key=key, failure='workload/' + str(key) + ': ' + str(error))

        failed_profiles = sorted({row['id'] for row in all_rows['workloads'] if isinstance(row, dict)
                                  and isinstance(row.get('id'), str) and row['id'].strip() and row['id'] not in workload_rows})

        def scope(row, profile, callback):
            declaration = row.get('scope')
            if 'scopes' in row: raise ValueError('source analysis uses one scope recipe across its profiles; scopes mapping was removed')
            if not isinstance(declaration, dict): raise ValueError('source scope recipe required')
            n = result['workloads'][profile]
            trace = engine._work.scope_count(declaration, n, facts, engine.expression)
            names = dict(facts)
            if trace['kind'] == 'task': names['workload_elements'] = n
            else:
                axis_names = _source_dependencies({'formula': workload_rows[profile]['quantity']})
                for value in callback(None):
                    engine._work.exclude_workload_elements(value, trace['kind'] + ' shape')
                    if axis_names & _source_dependencies({'formula': value}):
                        raise ValueError('per-occurrence shape repeats workload-axis identifier; use task scope for a whole-task shape')
            return callback(names), trace

        for section, name in (('computation', 'computations'), ('state', 'states'), ('flow', 'flows')):
            duplicates = duplicate_ids(all_rows[name])
            for index, row in enumerate(all_rows[name]):
                key = row.get('id') if isinstance(row, dict) else None
                try:
                    local = identifier(row, section, duplicates)
                    dependencies = _source_row_dependencies(row)
                    if section == 'computation':
                        if not row.get('semantics'): raise ValueError('source computation semantics required')
                        steps = row['steps']
                        if not isinstance(steps, list) or not steps or len(steps) > MAX_PRIMITIVE_STEPS:
                            raise ValueError('1..' + str(MAX_PRIMITIVE_STEPS) + ' primitive steps required (execution-size limit)')
                        if any(not isinstance(step, dict) or not isinstance(step.get('id'), str) or not step['id'].strip() for step in steps) or len({step['id'] for step in steps}) != len(steps):
                            raise ValueError('unique nonempty step ids required')
                    elif section == 'state':
                        if not isinstance(row.get('lifecycle'), str) or not row['lifecycle'].strip(): raise ValueError('logical lifecycle required')
                        if set(row) - {'id', 'shape', 'lifecycle', 'basis', 'references'}:
                            raise ValueError('source state has logical shape/lifecycle; internal bitwidth, banks and replicas belong to the proposal')
                    else:
                        if row.get('direction') not in ('read', 'write'): raise ValueError('flow direction read|write required')
                        if row.get('encoding') not in ('fixed_external', 'abi_dependent'): raise ValueError('flow encoding fixed_external|abi_dependent required')
                        if row['encoding'] == 'abi_dependent':
                            if row.get('fields') not in ([], None): raise ValueError('ABI-dependent source flow does not choose a physical encoding')
                            data = {'direction': row['direction'], 'encoding': row['encoding'], 'totals': {}}
                            record(section, index, row, key=key, dependencies=dependencies, output={'collection': name, 'key': key, 'data': data}, local_bindings=local)
                            result[name][key] = data
                            continue
                        fields = row['fields']
                        if not isinstance(fields, list) or not fields: raise ValueError('fixed external fields required')
                    if not workload_rows and not failed_profiles: raise ValueError('row has no declared workload profile')
                except exception_types as error:
                    record(section, index, row, key=key, failure=section + '/' + str(key) + ': ' + str(error))
                    continue
                for profile in failed_profiles:
                    record(section, index, {'row': row, 'workload': profile}, key=key, workload=profile, dependencies=dependencies,
                           failure=section + '/' + str(key) + '/' + profile + ': workload declaration failed; its quantity cannot be used')
                for profile in workload_rows:
                    try:
                        numbers = {}
                        if section == 'computation':
                            def calculate(names):
                                if names is None:
                                    return [value for step in steps for field in ('lhs', 'rhs', 'shape') for value in step.get(field, [])] + [step[field] for step in steps for field in ('ops_per_element', 'input_elements', 'output_elements', 'width') if field in step]
                                derived = [dict(primitive(step, names, engine), id=step['id']) for step in steps]
                                if len({step['unit'] for step in derived}) != 1: raise ValueError('split MAC and scalar-ops into separate computations')
                                return derived
                            derived, trace = scope(row, profile, calculate)
                            single = sum(step['operations'] for step in derived); total = single * trace['total']; unit = derived[0]['unit']
                            data = {'total': total, 'per_occurrence': single, 'steps': derived, 'scope': trace, 'unit': unit}
                            numbers[key + '__' + profile] = total
                        elif section == 'state':
                            dims = shape(row['shape'], dict(facts, workload_elements=result['workloads'][profile]), engine)
                            data = {'shape': dims, 'elements': math.prod(dims), 'lifecycle': row['lifecycle']}
                            numbers[key + '__' + profile + '__elements'] = data['elements']
                        else:
                            def field_bits(names):
                                if names is None: return [value for field in fields for value in field['shape']] + [field['element_bits'] for field in fields]
                                bits = 0
                                for field in fields:
                                    if set(field) != {'shape', 'element_bits'}: raise ValueError('field shape/element_bits only')
                                    width = engine.expression(field['element_bits'], names)
                                    if width <= 0 or width != int(width): raise ValueError('external width must be a positive integer')
                                    bits += math.prod(shape(field['shape'], names, engine)) * int(width)
                                return bits
                            single, trace = scope(row, profile, field_bits)
                            total = {'bits': single * trace['total'], 'bytes': math.ceil(single / 8) * trace['total']}
                            data = {'direction': row['direction'], 'encoding': row['encoding'], 'total': total,
                                    'per_occurrence_bits': single, 'scope': trace}
                            numbers[key + '__' + profile + '__bytes'] = total['bytes']
                        record(section, index, {'row': row, 'workload': profile}, key=key, workload=profile, dependencies=dependencies,
                               output={'collection': name, 'key': key, 'workload': profile, 'data': data},
                               numbers=numbers, local_bindings=local)
                        if section == 'computation':
                            aggregate = result[name].setdefault(key, {'totals': {}, 'per_occurrence': {}, 'steps': {}, 'scopes': {}, 'unit': data['unit']})
                            aggregate['totals'][profile] = data['total']; aggregate['per_occurrence'][profile] = data['per_occurrence']
                            aggregate['steps'][profile] = data['steps']; aggregate['scopes'][profile] = data['scope']
                        elif section == 'state':
                            aggregate = result[name].setdefault(key, {'profiles': {}, 'lifecycle': data['lifecycle']})
                            aggregate['profiles'][profile] = {'shape': data['shape'], 'elements': data['elements']}
                        else:
                            aggregate = result[name].setdefault(key, {'direction': data['direction'], 'encoding': data['encoding'], 'totals': {}, 'per_occurrence_bits': {}, 'scopes': {}})
                            aggregate['totals'][profile] = data['total']; aggregate['per_occurrence_bits'][profile] = data['per_occurrence_bits']; aggregate['scopes'][profile] = data['scope']
                    except exception_types as error:
                        record(section, index, {'row': row, 'workload': profile}, key=key, workload=profile, dependencies=dependencies,
                               failure=section + '/' + str(key) + '/' + profile + ': ' + str(error))
        result['ok'] = not errors
        result['coverage_complete'] = not errors and not result['missing_declarations']
        result['execution_status'] = 'executed' if result['coverage_complete'] else 'partial' if any(row['status'] == 'executed' for row in result['subreceipts']) else 'failed'
        result['calculation_ids'] = list(values)
    except exception_types as error:
        errors.append(str(error))
        result['execution_status'] = 'partial' if values else 'failed'
    return result


def source_view(baseline, raw_model, sources, engine, execution_dependencies=None):
    """Expose only transaction-verified source data to proposal comparisons.

    The raw source result remains the evidence identity. This internal view keeps
    incomplete source coverage explicit and never changes a failed result to ok.
    """
    original = _evidence_module().original_sources(sources)
    if execution_dependencies is None:
        trusted = source_execution_binding(raw_model, original, engine)
        execution_dependencies = {kind + '_bundle_sha256': trusted[kind + '_bundle_sha256'] for kind in ('checker', 'calculator')}
    spec = importlib.util.spec_from_file_location('audit_verified_source_view', Path(__file__).with_name('validate_review.py'))
    validator = importlib.util.module_from_spec(spec); spec.loader.exec_module(validator)
    selected = validator.executed_source_values(baseline, original, raw_model, execution_dependencies)
    view = copy.deepcopy(selected['verified_data'])
    def declared(collection):
        declarations = raw_model.get(collection, []) if isinstance(raw_model, dict) else []
        if not isinstance(declarations, list): return []
        return list(dict.fromkeys(row['id'] for row in declarations if isinstance(row, dict)
                                  and isinstance(row.get('id'), str) and row['id'].strip()))
    view.update(ok=baseline.get('ok') is True if isinstance(baseline, dict) else False,
                errors=copy.deepcopy(baseline.get('errors', [])) if isinstance(baseline, dict) else [],
                source_receipts_verified=not selected['errors'],
                source_coverage_complete=selected['coverage_complete'],
                source_integrity_errors=copy.deepcopy(selected['errors']),
                declared_workloads=declared('workloads'), declared_computations=declared('computations'),
                missing_source_declarations=copy.deepcopy(baseline.get('missing_declarations', [])) if isinstance(baseline, dict) else [],
                source_analysis_sha256=fingerprint(baseline))
    return view


def profile_mapping_error(source_quantity, source_unit, candidate_quantity, candidate_unit):
    """Reject unequal task extents only for explicitly identical workload units."""
    if (isinstance(source_unit, str) and isinstance(candidate_unit, str)
            and source_unit.strip() and source_unit.strip() == candidate_unit.strip()
            and source_quantity != candidate_quantity):
        return ('profile mapping has unequal quantities for the same explicit unit: source '
                + str(source_quantity) + ' ' + source_unit.strip() + ', candidate '
                + str(candidate_quantity) + ' ' + candidate_unit.strip())
    return None


def compare_work(model, baseline, proposal, sources, engine, citations=()):
    """Independent row transactions; partial correspondence is never full coverage."""
    errors, observations, bindings, covered, credited, values = [], [], [], set(), set(), {}
    result = {'ok': False, 'errors': errors, 'observations': observations,
              'values': values, 'calculation_ids': [], 'subreceipts': [],
              'missing_count_checks': [], 'missing_profiles': [],
              'citation_bindings': bindings, 'coverage_complete': False,
              'scope': 'executed local work correspondence only; partial or failed declarations do not establish full profile/path coverage; source interpretation and transformed equivalence remain independently reviewable'}
    try:
        if not isinstance(baseline, dict) or baseline.get('source_receipts_verified') is not True or baseline.get('source_integrity_errors'):
            raise ValueError('transaction-verified independent source view required; raw or fields-only baselines are not accepted')
        declared_profiles = baseline['declared_workloads']
        declared_paths = baseline['declared_computations']
        result['source_coverage_complete'] = baseline['source_coverage_complete']
        result['missing_source_declarations'] = copy.deepcopy(baseline['missing_source_declarations'])
        requirements = proposal.get('requirements', {})
        facts, failed = engine.validate_requirements(requirements, _evidence_module().original_sources(sources))
        work = engine._work.validate(proposal.get('work_model'), facts, sources, engine.expression)
        if failed or not work['ok']:
            raise ValueError('; '.join(failed + work['errors']))
        work_items = {item['id']: item for item in proposal['work_model']['items']}
        if not isinstance(model, dict):
            raise ValueError('work correspondence model must be an object')
        raw_mapping = model.get('workload_map', {})
        if not isinstance(raw_mapping, dict):
            raise ValueError('workload_map must be an object')
        mapping = {}
        for sid, pid in raw_mapping.items():
            if not isinstance(sid, str) or not isinstance(pid, str) or sid not in baseline['workloads'] or pid not in work['workloads']:
                errors.append('profile/' + str(sid) + ': mapping needs existing source and candidate profiles')
            else:
                error = profile_mapping_error(baseline['workloads'][sid], baseline['workload_units'][sid],
                                              work['workloads'][pid], work['workload_units'][pid])
                if error:
                    errors.append('profile/' + sid + ' -> ' + pid + ': ' + error)
                else:
                    mapping[sid] = pid
        result['missing_profiles'] = ([{'side': 'source', 'workload': sid} for sid in declared_profiles if sid not in mapping]
            + [{'side': 'candidate', 'workload': pid} for pid in work['workloads'] if pid not in mapping.values()])
        rows = model.get('count_checks', [])
        if not isinstance(rows, list) or len(rows) > 400:
            raise ValueError('count_checks must be a bounded array')
        frozen = {'design_sha256': fingerprint(engine.design_data(proposal)),
                  'baseline_sha256': baseline['source_analysis_sha256'], 'model_sha256': fingerprint(model),
                  'source_sha256s': {k: hashlib.sha256(v.encode()).hexdigest() for k, v in sources.items()},
                  'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  'calculator_sha256': hashlib.sha256(Path(engine.__file__).read_bytes()).hexdigest()}
        def physical_recipe(row, pid, targets, local_bindings):
            declaration = row.get('physical_realization')
            outcome = {'status': 'unverified', 'semantic_equivalence_status': 'unverified',
                       'groups': [], 'missing_cost_groups': [], 'uncovered_work_ids': list(targets),
                       'coverage_complete': False,
                       'scope': 'typed realization recipe against selected actual cost groups/resources/schedule; not source equivalence or a universal source operation lower bound'}
            if declaration is None:
                outcome['reason'] = 'transformed realization has no typed physical cost witness'
                return outcome
            if not isinstance(declaration, dict) or not declaration.get('basis'):
                raise ValueError('physical_realization needs groups and basis')
            declarations = declaration.get('groups')
            if not isinstance(declarations, list) or not declarations or len(declarations) > 200:
                raise ValueError('physical_realization needs bounded nonempty groups')
            fresh = engine.evaluate(proposal, _evidence_module().original_sources(sources))
            selected = fresh.get('candidates', {}).get(proposal.get('selected'))
            if selected is None:
                raise ValueError('selected actual cost ledger is unavailable')
            names = selected['values']
            groups = {g['id']: g for g in selected['ledger'].get('cost_groups', [])}
            candidate = next(c for c in proposal['candidates'] if c['id'] == proposal['selected'])
            resources = ({g['name'] for g in candidate.get('macros', [])}
                         | {g['id'] for key in ('resource_checks', 'area_components') for g in candidate.get(key, [])})
            profile = selected['workloads'][pid]
            claimed, supported = set(), set()
            for physical in declarations:
                gid = physical['cost_group']
                if gid in claimed or gid not in groups or groups[gid]['workload'] != pid:
                    raise ValueError('physical group needs unique actual group in mapped candidate workload')
                claimed.add(gid)
                group = groups[gid]
                begin = len(local_bindings)
                references(physical.get('references'), sources, 'physical/' + gid,
                           local_bindings, original_only=False, citations=citations)
                if not physical.get('basis') or not any(b['resolved'].get('file') in ('design.json', 'architecture.md') for b in local_bindings[begin:]):
                    raise ValueError('physical recipe needs proposal-bound realization basis')
                steps = physical['steps']
                if not isinstance(steps, list) or len(steps) > MAX_PRIMITIVE_STEPS:
                    raise ValueError('physical steps must be an array of at most ' + str(MAX_PRIMITIVE_STEPS) + ' entries (execution-size limit)')
                if len({step.get('id') for step in steps if isinstance(step, dict)}) != len(steps):
                    raise ValueError('physical step ids must be unique')
                scope = engine._work.scope_count(physical['scope'], work['workloads'][pid], names, engine.expression)
                step_names = dict(names)
                if scope['kind'] == 'task':
                    step_names['workload_elements'] = work['workloads'][pid]
                else:
                    for step in steps:
                        for key in ('lhs', 'rhs', 'shape'):
                            for value in step.get(key, []):
                                engine._work.exclude_workload_elements(value, 'physical per-occurrence shape')
                        for key in ('input_elements', 'output_elements', 'width', 'ops_per_element'):
                            if key in step:
                                engine._work.exclude_workload_elements(step[key], 'physical per-occurrence extent')
                derived = [dict(primitive(step, step_names, engine), id=step['id']) for step in steps]
                by_unit = {}
                for step in derived:
                    by_unit[step['unit']] = by_unit.get(step['unit'], 0) + step['operations'] * scope['total']
                schedule = [part['id'] for part in profile['components'] if gid in part['references']]
                structural = (len(schedule) == 1 and set(group.get('resources', [])) <= resources and bool(group.get('resources')))
                detail = {'cost_group': gid, 'work_ids': list(group['work_ids']),
                          'steps': derived, 'scope_trace': scope, 'physical_by_unit': by_unit,
                          'actual_operation_unit': group.get('operation_unit'),
                          'actual_cost_operations': group.get('operations', 0),
                          'scheduled_cycles': group['scheduled_cycles'],
                          'resources': list(group.get('resources', [])), 'schedule_references': schedule,
                          'basis': physical['basis'], 'status': 'unverified'}
                if group.get('model') == 'compute':
                    units = engine.expression(group['units'], names)
                    rate = engine.expression(group['operations_per_unit_cycle'], names)
                    raw_group = next(g for g in candidate['cost_groups'] if g['id'] == gid)
                    overhead = engine.expression(raw_group['overhead_cycles'], names)
                    detail.update(units=units, operations_per_unit_cycle=rate, overhead_cycles=overhead)
                    if not steps and group.get('operations', 0) > 0:
                        raise ValueError('nonzero arithmetic group cannot omit its physical steps')
                    if set(by_unit) == {group.get('operation_unit')}:
                        physical_ops = by_unit[group['operation_unit']]
                        required = math.ceil(physical_ops / (units * rate)) + overhead
                        detail.update(required_cycles=required, physical_operations=physical_ops)
                        detail['status'] = 'pass' if structural and physical_ops == group['operations'] and required <= group['scheduled_cycles'] else 'fail'
                    else:
                        detail['reason'] = 'physical primitive units do not match declared group units; custom unit/rate mapping needs independent hardware evidence'
                elif not steps and structural and group.get('model') == 'cycles':
                    detail['status'] = 'pass'
                    detail['reason'] = 'explicit non-arithmetic group; movement and resource service still require their own witness'
                else:
                    detail['reason'] = 'physical arithmetic recipe cannot be charged to this non-compute/unsupported group'
                outcome['groups'].append(detail)
                supported.update(group['work_ids'])
            required_groups = {gid for gid, group in groups.items() if group['workload'] == pid and set(targets) & set(group['work_ids'])}
            outcome['missing_cost_groups'] = sorted(required_groups - claimed)
            outcome['uncovered_work_ids'] = sorted(set(targets) - supported)
            statuses = [g['status'] for g in outcome['groups']]
            outcome['status'] = 'fail' if 'fail' in statuses else 'unverified' if outcome['missing_cost_groups'] or outcome['uncovered_work_ids'] or 'unverified' in statuses else 'pass'
            outcome['coverage_complete'] = not outcome['missing_cost_groups'] and not outcome['uncovered_work_ids']
            return outcome

        row_ids = set()
        for index, row in enumerate(rows):
            label = str(row.get('id', '?')) if isinstance(row, dict) else '?'
            receipt = {'id': 'count/' + label + '/' + str(index), 'section': 'count',
                       'status': 'declaration_failed', 'errors': [], 'observations': [],
                       'values': {}, 'calculation_ids': [], 'binding': frozen,
                       'declaration_sha256': fingerprint(row), 'citation_bindings': []}
            local_bindings = []
            try:
                if not isinstance(row, dict):
                    raise ValueError('count row must be an object')
                if not isinstance(row.get('id'), str) or not row['id'].strip() or row['id'] in row_ids:
                    raise ValueError('unique nonempty count check id required')
                row_ids.add(row['id'])
                references(row.get('references'), sources, 'count/' + label, local_bindings, original_only=False, citations=citations)
                sid = row['workload']
                if sid not in mapping:
                    raise ValueError('count workload lacks a valid explicit profile mapping')
                pid = mapping[sid]
                paths, targets = row['source_paths'], row['candidate_work_ids']
                if not isinstance(paths, list) or not isinstance(targets, list) or not paths or not targets or len(set(paths)) != len(paths) or len(set(targets)) != len(targets):
                    raise ValueError('unique nonempty source_paths and candidate_work_ids required')
                if any(path not in baseline['computations'] or sid not in baseline['computations'][path].get('totals', {}) for path in paths):
                    raise ValueError('source path/profile has no successful original-source transaction')
                source_rows = [baseline['computations'][x] for x in paths]
                selections = row.get('candidate_terms', {})
                if not isinstance(selections, dict) or set(selections) - set(targets):
                    raise ValueError('candidate_terms must map listed candidate_work_ids to sum term indices only')
                target_rows, candidate_selection, target_credit = [], [], set()
                for target in targets:
                    parent = work['counts'][pid][target]
                    item = work_items[target]
                    recipe = item['count']
                    terms = engine._work.sum_terms(recipe) if recipe.get('kind') == 'sum' else None
                    indices = list(range(len(terms))) if terms is not None else None
                    if target in selections:
                        indices = selections[target]
                        if terms is None:
                            raise ValueError('candidate_terms requires an existing sum work item: ' + target)
                        if (not isinstance(indices, list) or not indices
                                or any(type(index) is not int or index < 0 or index >= len(terms) for index in indices)
                                or len(indices) != len(set(indices))):
                            raise ValueError('candidate_terms needs unique nonempty zero-based indices within frozen sum: ' + target)
                        indices = sorted(indices)
                    operations = parent['operations']
                    if target in selections:
                        per_occurrence, trace = engine._work.scoped_count(
                            dict(recipe, terms=[terms[index] for index in indices]), item['scopes'][pid],
                            work['workloads'][pid], facts, engine.expression)
                        operations = per_occurrence * trace['total']
                    target_rows.append({'operations': operations, 'unit': parent['unit']})
                    candidate_selection.append({'work_id': target, 'term_indices': indices,
                        'full_work_item': indices is None or len(indices) == len(terms),
                        'parent_operations': parent['operations'], 'operations': operations,
                        'unit': parent['unit'], 'scope_trace': copy.deepcopy(parent['scope_trace']),
                        'parent_recipe_sha256': fingerprint(recipe)})
                    target_credit.update((pid, target, index) for index in (indices if indices is not None else [-1]))
                selected_candidate = next(c for c in proposal['candidates'] if c['id'] == proposal['selected'])
                actual_transformed_groups = []
                for actual_group in selected_candidate.get('cost_groups', []):
                    if actual_group.get('workload') != pid or actual_group.get('realization', 'direct') != 'transformed':
                        continue
                    actual_ids = engine._work.resolve_work_ids(actual_group['work_ids'], pid, work)
                    if set(targets) & set(actual_ids):
                        actual_transformed_groups.append(actual_group['id'])
                realization = row['realization']
                if actual_transformed_groups and realization != 'transformed':
                    raise ValueError('audit direct declaration targets actual transformed cost groups '
                                     + str(actual_transformed_groups)
                                     + '; declare transformed and bind native physical cost plus independent equivalence review')
                if realization not in ('direct', 'transformed') or not row.get('basis'):
                    raise ValueError('realization direct|transformed and correspondence basis required')
                source_units, target_units = {}, {}
                for item in source_rows:
                    source_units[item['unit']] = source_units.get(item['unit'], 0) + item['totals'][sid]
                if any(item.get('operations') is None for item in target_rows):
                    raise ValueError('candidate operation count is unresolved')
                for item in target_rows:
                    target_units[item['unit']] = target_units.get(item['unit'], 0) + item['operations']
                if realization == 'direct' and (len(source_units) != 1 or set(source_units) != set(target_units)):
                    raise ValueError('direct correspondence requires matching primitive units; mixed-unit fusion needs transformed equivalence evidence')
                if any((sid, x) in covered for x in paths) or target_credit & credited:
                    raise ValueError('duplicate source/candidate count credit; combine fused paths in one comparison')
                if realization == 'transformed' and not row.get('equivalence_evidence'):
                    raise ValueError('transformed counts require explicit equivalence evidence')
                expected = next(iter(source_units.values())) if len(source_units) == 1 else source_units
                actual = next(iter(target_units.values())) if len(target_units) == 1 else target_units
                observation = {'id': label, 'source_paths': paths, 'workload': sid,
                    'candidate_workload': pid, 'candidate_work_ids': targets,
                    'candidate_selection': candidate_selection,
                    'expected': expected, 'actual': actual,
                    'unit': next(iter(source_units)) if realization == 'direct' else 'by_primitive_unit',
                    'source_by_unit': source_units, 'candidate_by_unit': target_units,
                    'source_root_quantity': baseline['workloads'][sid], 'candidate_root_quantity': work['workloads'][pid],
                    'realization': realization, 'actual_transformed_cost_groups': actual_transformed_groups,
                    'matches': actual == expected if realization == 'direct' else None,
                    'equivalence_evidence': row.get('equivalence_evidence'), 'receipt_id': receipt['id']}
                if realization == 'transformed':
                    physical = physical_recipe(row, pid, targets, local_bindings)
                    observation['physical_realization'] = physical
                    observation['physical_status'] = physical['status']
                    observation['semantic_equivalence_status'] = 'unverified'
                receipt['status'] = 'executed'
                receipt['observations'] = [observation]
                for suffix, value in (('source', expected), ('proposal', actual)):
                    if type(value) in (int, float):
                        receipt['values'][label + '__' + suffix] = value
                receipt['calculation_ids'] = list(receipt['values'])
                receipt['citation_bindings'] = local_bindings
                observations.append(observation)
                covered.update((sid, x) for x in paths)
                credited.update(target_credit)
                values.update(receipt['values'])
                bindings.extend(local_bindings)
            except (KeyError, TypeError, ValueError, SyntaxError, ZeroDivisionError, OverflowError) as error:
                message = 'count/' + label + ': ' + str(error)
                errors.append(message)
                receipt['errors'].append(message)
            receipt['receipt_sha256'] = fingerprint(receipt)
            result['subreceipts'].append(receipt)
        result['missing_count_checks'] = [{'workload': sid, 'source_path': path}
            for sid in declared_profiles for path in declared_paths if (sid, path) not in covered]
        result['calculation_ids'] = list(values)
        result['coverage_complete'] = (baseline['source_coverage_complete'] is True and not errors
                                       and not result['missing_profiles'] and not result['missing_count_checks'])
        result['execution_status'] = 'partial' if errors or not result['coverage_complete'] else 'executed'
        result['ok'] = not errors
    except (KeyError, TypeError, ValueError, SyntaxError, ZeroDivisionError, OverflowError) as error:
        errors.append(str(error))
        result['execution_status'] = 'failed'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--calculator', required=True)
    args = parser.parse_args(); data = json.load(sys.stdin); engine = load_engine(args.calculator)
    if data.get('mode') == 'source': result = analyze(data.get('model'), data['sources'], engine)
    else: raise ValueError('source_model supports mode source only')
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2); sys.stdout.write('\n')


if __name__ == '__main__': main()
