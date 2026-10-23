"""Small explicit unit vocabulary and dimensional checks; no chip constants."""
import ast
import math
import re


def spelling(text):
    return re.sub(r'\s+', '', str(text)).replace('²', '2').replace('^2', '2').replace('μ', 'u').replace('µ', 'u')


def unit(text, prefix_base='decimal'):
    """Return dimensions and size in bits/mm2/seconds/items, or None if opaque."""
    t = spelling(text)
    area = {'mm2': 1, 'um2': 1e-6, 'm2': 1e6, 'cm2': 100}
    if t in area:
        return {'area': 1}, area[t]
    time = {'s': 1, 'ms': 1e-3, 'us': 1e-6, 'ns': 1e-9, 'ps': 1e-12}
    if t in time:
        return {'time': 1}, time[t]
    if t in ('Hz', 'kHz', 'MHz', 'GHz'):
        return {'time': -1}, {'Hz': 1, 'kHz': 1e3, 'MHz': 1e6, 'GHz': 1e9}[t]
    if t in ('item', 'items', 'token', 'tokens'):
        return {'item': 1}, 1
    if t in ('cycle', 'cycles', 'count', 'units', 'dimensionless', '1'):
        return {}, 1
    if t == '%':
        return {}, 0.01
    match = re.fullmatch(r'([kKMGT]?)(i?)(bit|bits|b|B|byte|bytes)', t)
    if match:
        prefix, binary, suffix = match.groups()
        power = {'': 0, 'k': 1, 'K': 1, 'M': 2, 'G': 3, 'T': 4}[prefix]
        base = 1024 if binary or prefix_base == 'binary' else 1000
        return {'storage': 1}, base**power * (8 if suffix in ('B', 'byte', 'bytes') else 1)
    if t.count('/') == 1:
        numerator, denominator = (unit(x, prefix_base) for x in t.split('/'))
        if numerator is not None and denominator is not None:
            return combine(numerator[0], denominator[0], -1), numerator[1] / denominator[1]
    return None


def combine(a, b, sign=1):
    result = dict(a)
    for k, v in b.items():
        result[k] = result.get(k, 0) + sign*v
    return {k: v for k, v in result.items() if v}


def _fact_rows(rows):
    if isinstance(rows, dict):
        return rows
    if isinstance(rows, (list, tuple)):
        return {row['id']: row for row in rows if isinstance(row, dict) and isinstance(row.get('id'), str)}
    raise ValueError('prior_fact_rows must be a fact array or ID-to-row object')


def _intrinsic_binary(text):
    return bool(re.search(r'[kKMGT]i(?:bit|bits|b|B|byte|bytes)', spelling(text)))


def fact_prefix_info(row, prior_fact_rows=None, seen=None):
    """Retain literal prefix context; intrinsic i belongs to its unit only.

    A plain storage prefix defaults to decimal. An explicit binary/decimal
    literal excerpt supplies context for subsequent plain-prefix conversions.
    Intrinsic MiB does not redefine an ordinary MB destination as binary MB.
    """
    rows = _fact_rows(prior_fact_rows or [])
    seen = set() if seen is None else set(seen)
    identifier = row.get('id')
    if identifier in seen:
        raise ValueError('conversion source cycle')
    seen.add(identifier)
    if 'convert' in row and 'converted_from' in row:
        raise ValueError('fact cannot have both raw and materialized conversion ancestry')
    conversion = row.get('convert', row.get('converted_from'))
    if conversion is not None:
        if not isinstance(conversion, dict) or set(conversion) != {'fact', 'to_unit'}:
            raise ValueError('convert requires exactly fact/to_unit')
        parent = rows.get(conversion['fact'])
        if parent is None:
            raise ValueError('conversion source fact is unavailable')
        context = fact_prefix_info(parent, rows, seen)['prefix_context']
        text = conversion['to_unit']
        if 'unit' in row and row['unit'] != text:
            raise ValueError('converted fact unit disagrees with convert.to_unit')
    else:
        text = row.get('unit')
        parsed = unit(text)
        context = None
        if parsed is not None and parsed[0].get('storage'):
            references = row.get('citations', [])
            if not isinstance(references, list):
                raise ValueError('source fact citations must be an array')
            quote = '\n'.join(c.get('quote', '') for c in references if isinstance(c, dict))
            binary = bool(re.search(r'二进制|binary', quote, re.I))
            decimal = bool(re.search(r'十进制|decimal', quote, re.I))
            if binary and decimal:
                raise ValueError('source excerpt has ambiguous prefix basis; use a precise excerpt')
            context = 'binary' if binary else 'decimal' if decimal else None
    intrinsic = _intrinsic_binary(text)
    if intrinsic and context == 'decimal':
        # The context specifies the ordinary-prefix convention. An explicitly
        # intrinsic i destination still has its own unambiguous binary scale.
        if conversion is None:
            raise ValueError('intrinsic binary source unit contradicts decimal citation')
    basis = 'binary' if intrinsic else context or 'decimal'
    if 'prefix_base' in row and row['prefix_base'] != basis:
        raise ValueError('source prefix_base contradicts its unit or literal evidence')
    return {'prefix_base': basis, 'prefix_context': context}


def _semantic_family(text):
    t = spelling(text)
    if t in ('cycle', 'cycles'):
        return 'cycles'
    if t in ('count', 'units', 'dimensionless', '1', '%'):
        return 'dimensionless'
    return None


def convert_fact(row, prior_fact_rows, prior_values):
    """Evaluate one explicit conversion over a previously evaluated quantity.

    Return value/unit/prefix metadata; never rewrite input rows. The caller
    validates actual source citations. Unit conversion cannot infer source
    semantics, rename opaque units, or turn a count into tokens or cycles.
    """
    if not isinstance(row, dict) or not {'id', 'convert'} <= set(row) or set(row) - {'id', 'convert', 'citations'}:
        raise ValueError('conversion fact requires id/convert and optional citations; unit is program-derived')
    if not isinstance(row['id'], str) or not row['id'].isidentifier():
        raise ValueError('conversion fact id must be an identifier string')
    conversion = row['convert']
    if not isinstance(conversion, dict) or set(conversion) != {'fact', 'to_unit'}:
        raise ValueError('convert requires exactly fact/to_unit')
    source_id, target = conversion['fact'], conversion['to_unit']
    if not isinstance(source_id, str) or not source_id.isidentifier() or not isinstance(target, str) or not target.strip():
        raise ValueError('conversion needs an actual source fact ID and nonempty target unit')
    if 'citations' in row and (not isinstance(row['citations'], list) or not row['citations'] or any(not isinstance(c, dict) for c in row['citations'])):
        raise ValueError('conversion fact requires original-input citations')
    rows = _fact_rows(prior_fact_rows)
    source = rows.get(source_id)
    if source is None or source_id not in prior_values or source_id == row['id']:
        raise ValueError('conversion source must be a previously evaluated fact')
    value = prior_values[source_id]
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('conversion source value must be finite numeric')
    source_info = fact_prefix_info(source, rows)
    source_conversion = source.get('convert', source.get('converted_from', {}))
    source_unit = source_conversion.get('to_unit', source.get('unit'))
    target_basis = 'binary' if _intrinsic_binary(target) else source_info['prefix_context'] or 'decimal'
    a, b = unit(source_unit, source_info['prefix_base']), unit(target, target_basis)
    if a is None or b is None:
        raise ValueError('conversion supports only explicit known units, not opaque semantic renaming')
    if a[0] != b[0]:
        raise ValueError('conversion source and destination dimensions differ')
    source_family, target_family = _semantic_family(source_unit), _semantic_family(target)
    if source_family != target_family and (source_family is not None or target_family is not None):
        raise ValueError('conversion cannot rename dimensionless counts as clock cycles or vice versa')
    converted = value * a[1] / b[1]
    if not math.isfinite(converted):
        raise ValueError('conversion result must be finite')
    if converted == int(converted):
        converted = int(converted)
    return {'value': converted, 'unit': target, 'prefix_base': target_basis,
            'source_fact': source_id, 'source_prefix_base': source_info['prefix_base'],
            'prefix_context': source_info['prefix_context'],
            'scope': 'explicit unit conversion of a prior declared quantity; source semantics require audit'}


def formula_quantity(text, names):
    """Return natural (dimensions, scale); numeric coefficients are dimensionless.

    Engineering formulas use already compatible units. They do not silently
    reinterpret a multiplication coefficient as a prefix conversion.
    """
    def same_scale(a, b):
        return math.isclose(a, b, rel_tol=1e-12, abs_tol=0)

    def literal(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = literal(node.operand)
            return None if value is None else (-value if isinstance(node.op, ast.USub) else value)
        return None

    def visit(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return {}, 1
        if isinstance(node, ast.Name):
            return names.get(node.id)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            return visit(node.operand)
        if isinstance(node, ast.BinOp):
            a, b = visit(node.left), visit(node.right)
            if a is None or b is None:
                return None
            if isinstance(node.op, (ast.Add, ast.Sub, ast.Mod)):
                if a[0] != b[0]:
                    raise ValueError('addition/subtraction/modulo uses incompatible dimensions')
                if not same_scale(a[1], b[1]):
                    raise ValueError('addition/subtraction/modulo uses incompatible unit scales; use explicit convert first')
                return a
            if isinstance(node.op, ast.Mult):
                return combine(a[0], b[0]), a[1] * b[1]
            if isinstance(node.op, (ast.Div, ast.FloorDiv)):
                return combine(a[0], b[0], -1), a[1] / b[1]
            if isinstance(node.op, ast.Pow):
                if b[0]:
                    raise ValueError('power exponent must be dimensionless')
                power = literal(node.right)
                if a == ({}, 1):
                    return {}, 1
                if power is None:
                    return None
                if power != int(power):
                    raise ValueError('dimensioned power needs a literal integer exponent')
                return {k: v * int(power) for k, v in a[0].items() if v * int(power)}, a[1] ** int(power)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            quantities = [visit(arg) for arg in node.args]
            if any(q is None for q in quantities):
                return None
            if not quantities:
                return None
            if any(q[0] != quantities[0][0] for q in quantities):
                raise ValueError('function arguments use incompatible dimensions')
            if any(not same_scale(q[1], quantities[0][1]) for q in quantities):
                raise ValueError('function arguments use incompatible unit scales; use explicit convert first')
            return quantities[0]
        return None
    return visit(ast.parse(str(text), mode='eval').body)


def fact_dimension_errors(facts):
    names, errors = {}, []
    for f in facts:
        if not isinstance(f, dict) or not isinstance(f.get('id'), str):
            continue
        try:
            info = fact_prefix_info(f, facts)
            origin = f.get('convert', f.get('converted_from', {}))
            text = origin.get('to_unit', f.get('unit', ''))
            parsed = unit(text, info['prefix_base'])
        except (KeyError, TypeError, ValueError) as e:
            errors.append('fact '+f['id']+' prefix evidence: '+str(e));parsed=None
        if 'formula' in f and parsed is not None:
            try:
                actual = formula_quantity(f['formula'], names)
                if actual is not None and actual[0] != parsed[0]:
                    errors.append('derived fact dimension mismatch: '+f['id']+'; check frequency/unit conversions against source')
                elif actual is not None and not math.isclose(actual[1], parsed[1], rel_tol=1e-12, abs_tol=0):
                    errors.append('derived fact unit scale mismatch: '+f['id']+'; use explicit convert before engineering formulas')
            except (ValueError, SyntaxError) as e:
                errors.append('derived fact '+f['id']+': '+str(e))
        names[f['id']] = parsed
    return errors


def macro_density(requirements, values):
    """Resolve a cited input density; never select or guess a platform density."""
    model = requirements.get('storage_area_model')
    if not isinstance(model, dict):
        raise ValueError('storage_area_model required: supplied density_fact or unspecified with reason')
    if model.get('status') == 'unspecified' and isinstance(model.get('reason'), str) and model['reason'].strip():
        return None
    if model.get('status') != 'supplied':
        raise ValueError('invalid storage_area_model status')
    fact = next((x for x in requirements['facts'] if x.get('id') == model.get('density_fact')), None)
    if fact is None or 'formula' in fact or 'convert' in fact or 'converted_from' in fact or type(fact.get('value')) not in (int, float):
        raise ValueError('storage density_fact '+str(model.get('density_fact'))+' must name a retained cited literal fact with value, unit and citations; never delete a referenced fact. Conversions are performed by the tool')
    quote = '\n'.join(x.get('quote', '') for x in fact.get('citations', []))
    base = fact_prefix_info(fact, requirements['facts'])['prefix_base']
    parsed = unit(fact['unit'], base)
    if parsed is None or parsed[0] != {'storage': 1, 'area': -1}:
        raise ValueError('density needs storage/area units, e.g. bit/mm2 or Mbit/mm^2')
    raw_unit = spelling(fact['unit'])
    if not re.search(r'(?<![A-Za-z])'+re.escape(raw_unit)+r'(?![A-Za-z0-9])', spelling(quote)):
        raise ValueError('density unit must occur in its exact input excerpt')
    numeric = r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?'
    supported = re.findall('('+numeric+r')\[?'+re.escape(raw_unit), spelling(quote))
    supported += re.findall(re.escape(raw_unit)+r'\]?(?:=|:|\|)('+numeric+')', spelling(quote))
    if not any(float(x)==fact['value'] for x in supported):
        raise ValueError('density value must be attached to its cited density unit; another number in the excerpt is not evidence')
    value = values[fact['id']]*parsed[1]
    if value <= 0:
        raise ValueError('storage density must be positive')
    return {'density_fact': fact['id'], 'bits_per_mm2': value, 'prefix_base': base,
            'scope': 'cited abstract density, not physical macro characterization'}
