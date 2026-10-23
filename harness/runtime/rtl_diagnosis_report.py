"""Strict, content-preserving framing for an ordinary diagnosis JSON report."""
import re

from harness.adapters.json_repair import json_decoder


def parse_report(text):
    """Accept one object, or one JSON fence with a short prose introduction/end.

    This selects framing only: JSON tokens and engineering values are never
    repaired, merged or rewritten. Ambiguous objects and fences remain errors.
    """
    if not isinstance(text, str) or len(text.encode('utf-8')) > 131072:
        raise ValueError('diagnosis report must be UTF-8 text of at most 128 KiB')
    body = text.strip()
    if not body.startswith('{'):
        fences = list(re.finditer(r'(?m)^[ \t]*```[^\r\n]*(?:\r?\n|$)', body))
        if len(fences) != 2:
            raise ValueError('diagnosis report requires one complete JSON object or one unambiguous JSON fence')
        opening, closing = fences
        if (re.fullmatch(r'[ \t]*```(?:json)?[ \t]*\r?\n', opening.group()) is None
                or re.fullmatch(r'[ \t]*```[ \t]*(?:\r?\n|$)', closing.group()) is None):
            raise ValueError('diagnosis report has an invalid or incomplete JSON fence')
        prose = body[:opening.start()] + body[closing.end():]
        if len(prose.encode('utf-8')) > 4096 or '```' in prose or any(char in prose for char in '{}[]'):
            raise ValueError('diagnosis report has excessive or ambiguous text outside its JSON fence')
        body = body[opening.end():closing.start()].strip()
    value, end = json_decoder().raw_decode(body)
    if body[end:].strip():
        raise ValueError('diagnosis report contains an ambiguous JSON suffix')
    if not isinstance(value, dict):
        raise ValueError('diagnosis report requires one JSON object')
    return value
