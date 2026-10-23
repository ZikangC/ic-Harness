"""Strict JSON framing and content conservation for syntax-only model repair."""
import json
import math
import re
from decimal import Decimal


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON object key: ' + key)
        result[key] = value
    return result


def finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError('non-finite JSON number')
    return value


def reject_constant(text):
    raise ValueError('non-JSON numeric constant: ' + text)


def json_decoder():
    return json.JSONDecoder(object_pairs_hook=unique_object,
                            parse_float=finite_float, parse_constant=reject_constant)


def serialization_frame(text):
    """One complete object, optionally one leading protocol marker and JSON fence."""
    body = text.strip()
    prefix = suffix = ''
    match = re.match(r'<<<[A-Z][A-Z0-9_]*>>>\s*', body)
    if match:
        prefix = match.group()
        body = body[match.end():]
    if body.startswith('```'):
        match = re.match(r'```(?:json)?[ \t]*\n', body)
        if not match or not body.rstrip().endswith('\n```'):
            raise ValueError('unclosed or invalid JSON fence')
        prefix += match.group()
        body = body[match.end():].rstrip()[:-3].rstrip()
        suffix = '\n```'
    if not body.startswith('{') or not body.endswith('}'):
        raise ValueError('repair requires a complete single JSON object')
    return prefix, body, suffix


def parse_json(text):
    clean = text.strip()
    # Plain JSON parsing has no protocol semantics.
    if clean.startswith('```'):
        prefix, clean, suffix = serialization_frame(clean)
        if '<<<' in prefix:
            raise ValueError('unexpected JSON protocol marker')
    result, end = json_decoder().raw_decode(clean)
    if clean[end:].strip():
        raise ValueError('ambiguous JSON suffix')
    if not isinstance(result, dict):
        raise ValueError('expected one JSON object')
    return result


def serialization_tokens(text):
    """Scan every character and require a complete object with only separator errors.

    Commas/colons and whitespace may change. Values, key order and all nesting
    are retained, with exact decimal comparison and equivalent hex integers.
    Missing values, unknown tokens, duplicate keys and truncation cannot repair.
    """
    tokens = []
    pos = 0
    numeric = re.compile(r'-?(?:0[xX][0-9a-fA-F]+|(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)')
    while pos < len(text):
        ch = text[pos]
        if ch in ' \t\r\n,:':
            pos += 1
            continue
        if ch in '{}[]':
            tokens.append(('structure', ch))
            pos += 1
            continue
        if ch == '"':
            start = pos
            pos += 1
            while pos < len(text):
                if text[pos] == '\\':
                    pos += 2
                elif text[pos] == '"':
                    pos += 1
                    break
                else:
                    pos += 1
            else:
                raise ValueError('unclosed JSON string')
            # Raw newline/tab may be escaped only if the decoded value agrees.
            tokens.append(('string', json.loads(text[start:pos], strict=False)))
            continue
        match = numeric.match(text, pos)
        if match:
            word = match.group()
            pos = match.end()
            if pos < len(text) and text[pos] not in ' \t\r\n,:{}[]"':
                raise ValueError('ambiguous or unknown numeric token')
            value = Decimal(int(word, 16)) if 'x' in word.lower() else Decimal(word)
            if not value.is_finite():
                raise ValueError('non-finite repair number')
            tokens.append(('number', value))
            continue
        literal = next((word for word in ('true', 'false', 'null')
                        if text.startswith(word, pos)), None)
        if literal:
            pos += len(literal)
            if pos < len(text) and text[pos] not in ' \t\r\n,:{}[]"':
                raise ValueError('unknown literal token')
            tokens.append(('literal', literal))
            continue
        raise ValueError('unknown character in JSON repair payload at ' + str(pos))

    index = 0

    def value():
        nonlocal index
        if index >= len(tokens):
            raise ValueError('missing JSON value')
        kind, token = tokens[index]
        index += 1
        if kind != 'structure':
            return
        if token == '{':
            keys = set()
            while index < len(tokens) and tokens[index] != ('structure', '}'):
                key_kind, key = tokens[index]
                if key_kind != 'string' or key in keys:
                    raise ValueError('object key missing or duplicated')
                keys.add(key)
                index += 1
                value()
            closing = '}'
        elif token == '[':
            while index < len(tokens) and tokens[index] != ('structure', ']'):
                value()
            closing = ']'
        else:
            raise ValueError('missing value or mismatched nesting')
        if index >= len(tokens) or tokens[index] != ('structure', closing):
            raise ValueError('truncated or mismatched JSON nesting')
        index += 1

    if not tokens or tokens[0] != ('structure', '{'):
        raise ValueError('repair requires an object')
    value()
    if index != len(tokens):
        raise ValueError('multiple objects or extra content')
    return tokens
