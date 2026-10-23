"""Concrete format and output-encoding probes, not an accuracy model."""
from decimal import (Decimal, localcontext, ROUND_HALF_EVEN, ROUND_HALF_UP,
                     ROUND_DOWN, ROUND_FLOOR, ROUND_CEILING)
import math
import operator


ROUNDING = {'nearest_even': ROUND_HALF_EVEN, 'half_away': ROUND_HALF_UP,
            'toward_zero': ROUND_DOWN, 'floor': ROUND_FLOOR, 'ceil': ROUND_CEILING}
RELATIONS = {'==': operator.eq, '!=': operator.ne, '<': operator.lt,
             '<=': operator.le, '>': operator.gt, '>=': operator.ge}


def _decimal(value):
    return Decimal.from_float(value) if isinstance(value, float) else Decimal(value)


def _round(value, policy):
    if policy == 'nearest_ties_positive':
        return int((value + Decimal('.5')).to_integral_value(rounding=ROUND_FLOOR))
    return int(value.to_integral_value(rounding=ROUNDING[policy]))


def _quantize(raw, bits, frac, signed, rounding, overflow):
    low = -(1 << (bits - 1)) if signed else 0
    high = (1 << (bits - int(signed))) - 1
    step = Decimal(2) ** -frac
    rounded = _round(raw / step, rounding)
    overflowed = rounded < low or rounded > high
    if overflowed and overflow == 'reject':
        raise ValueError('rounded value exceeds chosen format under reject policy')
    code = min(max(rounded, low), high)
    return code, step, low, high, overflowed


def _encode_row(row, names, expression, bits, frac, signed):
    if frac != 0 or not signed:
        raise ValueError('encode_row needs a signed integer output format: fractional_bits=0, signed=true')
    inputs = row['input']
    if not isinstance(inputs, list) or not 1 <= len(inputs) <= 4096:
        raise ValueError('encode_row input must be an array of 1..4096 numeric expressions')
    original = [expression(value, names) for value in inputs]
    scale = expression(row['scale'], names)
    if scale <= 0:
        raise ValueError('encode_row scale must be a finite positive uniform row scale')
    codes, scaled, decoded, overflow_count = [], [], [], 0
    scale_decimal = _decimal(scale)
    for value in original:
        raw = _decimal(value) * scale_decimal
        code, _, _, _, overflowed = _quantize(raw, bits, 0, True, row['rounding'], row['overflow'])
        codes.append(code); scaled.append(float(raw)); decoded.append(float(Decimal(code) / scale_decimal))
        overflow_count += int(overflowed)
    ref_argmax = max(range(len(original)), key=lambda index: original[index])
    code_argmax = max(range(len(codes)), key=lambda index: codes[index])
    ref_norm, code_norm = math.hypot(*original), math.hypot(*codes)
    cosine = None
    if ref_norm and code_norm:
        cosine = max(-1., min(1., math.fsum((x / ref_norm) * (y / code_norm) for x, y in zip(original, codes))))
    # Uniform positive scaling plus these rounding/saturation policies is
    # monotone. Count information lost to *new* ties, not just equal inputs.
    groups = {}
    for value, code in zip(original, codes):
        group = groups.setdefault(code, {})
        group[value] = group.get(value, 0) + 1
    new_ties = 0
    for group in groups.values():
        total = sum(group.values())
        new_ties += (total * (total - 1) - sum(count * (count - 1) for count in group.values())) // 2
    results = {'count': len(codes), 'scale': scale, 'cosine': cosine,
               'cosine_defined': int(cosine is not None),
               'reference_argmax': ref_argmax, 'encoded_argmax': code_argmax,
               'argmax_match': int(ref_argmax == code_argmax),
               'encoded_max_ties': codes.count(codes[code_argmax]),
               'introduced_tie_pairs': new_ties, 'saturation_count': overflow_count,
               'overflow_count': overflow_count, 'zero_count': codes.count(0),
               'max_absolute_error': max(abs(x - y) for x, y in zip(original, decoded))}
    details = {'inputs': original, 'scaled_values': scaled, 'codes': codes, 'decoded_values': decoded,
               'argmax_rule': 'smallest index among equal maximal actual integer codes'}
    return results, bool(overflow_count), details


def derive(rows, names, expression):
    errors, probes, values = [], [], {}
    if not isinstance(rows, list) or len(rows) > 200:
        return {'ok': False, 'errors': ['numeric_probes must be an array of at most 200 rows'],
                'probes': [], 'values': {}, 'coverage_status': 'invalid',
                'assertion_count': 0, 'checked_probe_count': 0, 'informative_probe_count': 0}
    seen = set()
    for row in rows:
        label = str(row.get('id', '?')) if isinstance(row, dict) else '?'
        try:
            required = {'id', 'operation', 'input', 'format', 'rounding', 'overflow', 'assertions', 'basis'}
            if not isinstance(row, dict):
                raise ValueError('numeric probe must be an object')
            if row.get('operation') == 'encode_row': required.add('scale')
            if set(row) - (required | {'references'}) or not required <= set(row):
                raise ValueError('numeric probe needs exactly the documented fields, with optional references')
            if not isinstance(row['id'], str) or not row['id'].isidentifier() or row['id'] in seen or not row['basis']:
                raise ValueError('unique identifier and basis required')
            seen.add(row['id'])
            available = dict(names, **values)  # previous probes only: no forward references or cycles
            fmt = row['format']
            if not isinstance(fmt, dict) or set(fmt) != {'bits', 'fractional_bits', 'signed'} or type(fmt['signed']) is not bool:
                raise ValueError('format needs bits, fractional_bits and boolean signed')
            bits, frac = (expression(fmt[key], available) for key in ('bits', 'fractional_bits'))
            if any(type(x) not in (int, float) or x != int(x) for x in (bits, frac)) or not 1 <= bits <= 128 or not 0 <= frac <= 128:
                raise ValueError('integer bits 1..128 and fractional_bits 0..128 required')
            bits, frac = int(bits), int(frac)
            if row['rounding'] not in ROUNDING and row['rounding'] != 'nearest_ties_positive':
                raise ValueError('explicit supported rounding required')
            if row['overflow'] not in ('saturate', 'reject'):
                raise ValueError('explicit supported overflow required')
            with localcontext() as ctx:
                ctx.prec = max(200, bits + frac + 40)
                details = None
                op = row['operation']
                if op == 'encode_row':
                    result, overflowed, details = _encode_row(row, available, expression, bits, frac, fmt['signed'])
                else:
                    x = expression(row['input'], available)
                    if op == 'identity': raw = x
                    elif op == 'exp': raw = math.exp(x)
                    elif op == 'sigmoid':
                        if x >= 0: raw = 1 / (1 + math.exp(-x))
                        else:
                            z = math.exp(x); raw = z / (1 + z)
                    elif op == 'reciprocal': raw = 1 / x
                    elif op == 'sqrt': raw = math.sqrt(x)
                    elif op == 'rsqrt': raw = 1 / math.sqrt(x)
                    else: raise ValueError('operation must be identity|exp|sigmoid|reciprocal|sqrt|rsqrt|encode_row')
                    if not math.isfinite(raw): raise ValueError('non-finite operation result')
                    raw_decimal = _decimal(raw)
                    code, step, low, high, overflowed = _quantize(raw_decimal, bits, frac, fmt['signed'], row['rounding'], row['overflow'])
                    quantized = Decimal(code) * step
                    result = {'raw': raw, 'integer': code, 'quantized': float(quantized),
                              'lsb': float(step), 'minimum': float(Decimal(low) * step),
                              'maximum': float(Decimal(high) * step),
                              'abs_error': float(abs(raw_decimal - quantized)),
                              'overflow_count': int(overflowed), 'zero_count': int(code == 0)}
            checks = []
            if not isinstance(row['assertions'], list): raise ValueError('assertions must be an array; an empty array is an informative probe only')
            for assertion in row['assertions']:
                if not isinstance(assertion, dict) or set(assertion) != {'metric', 'relation', 'expected'}:
                    raise ValueError('assertion needs metric, relation, expected')
                metric, relation = assertion['metric'], assertion['relation']
                if metric not in result or relation not in RELATIONS: raise ValueError('unknown metric or relation')
                expected = expression(assertion['expected'], available)
                # Undefined cosine is an executed failed requirement, never an
                # invented zero cosine or an accidental TypeError/pass.
                passed = result[metric] is not None and RELATIONS[relation](result[metric], expected)
                checks.append({'metric': metric, 'relation': relation, 'actual': result[metric],
                               'expected': expected, 'pass': bool(passed)})
            numeric_results = {key: value for key, value in result.items()
                               if type(value) in (int, float) and math.isfinite(value)}
            output_names = {row['id'] + '__' + metric for metric in numeric_results}
            if output_names & (set(names) | set(values)): raise ValueError('numeric probe output collides with an existing identifier')
            passed = all(check['pass'] for check in checks) if checks else None
            output = {'id': row['id'], 'results': result, 'overflowed': overflowed,
                      'assertions': checks, 'pass': passed,
                      'status': 'computed_only' if passed is None else 'checked' if passed else 'failed'}
            if details is not None: output['encoding'] = details
            probes.append(output)
            values.update({row['id'] + '__' + metric: value for metric, value in numeric_results.items()})
        except (KeyError, TypeError, ValueError, SyntaxError, ZeroDivisionError, OverflowError) as error:
            errors.append(label + ': ' + str(error))
    informative = sum(probe['pass'] is None for probe in probes)
    checked = sum(probe['pass'] is not None for probe in probes)
    status = ('invalid' if errors else 'failed' if any(probe['pass'] is False for probe in probes)
              else 'not_checked' if not probes else 'computed_only' if informative else 'checked')
    return {'ok': not errors, 'errors': errors, 'probes': probes, 'values': values,
            'coverage_status': status, 'assertion_count': sum(len(probe['assertions']) for probe in probes),
            'checked_probe_count': checked, 'informative_probe_count': informative,
            'scope': 'Declared concrete points, quantization and output-row conversion only; nonlinear functions use binary64. Checked means submitted assertions passed, not source-risk coverage, reachability, a bit-accurate hardware algorithm, approximation-error bounds or end-to-end accuracy evidence.'}
