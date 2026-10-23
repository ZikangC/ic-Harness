"""Read-only structural triage of sealed RTL. This is not a Verilog frontend.

Counts describe source syntax, not inferred hardware or time. Suggestions are
falsifiable experiments; none is an acceptance gate or a required RTL rewrite.
"""
from __future__ import annotations

import ast
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
import operator
from pathlib import Path
import re
import math

from core import digest, load_variant, load_workspace, read_json, under, variant_tree


_TOKEN = re.compile(r"\\[^\s]+|(?:\d+)?'[sS]?[bBoOdDhH][0-9a-fA-F_xXzZ?]+|\d+|`?[A-Za-z_$][\w$]*|<=|>=|\+\+|--|\+=|-=|<<|>>|==|!=|&&|\|\||[^\s]")
_HIDDEN = re.compile(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"', re.S)
_COMMENT = re.compile(r'//[^\n]*|/\*.*?\*/', re.S)
_BRANCH = {"if", "case", "casex", "casez", "for", "while", "repeat"}
_PROCEDURAL = {"always", "always_comb", "always_ff", "always_latch", "initial"}
_LIMIT = 10 * 1024 * 1024


@dataclass(frozen=True)
class Token:
    text: str
    line: int
    offset: int


def _mask(match):
    return "".join("\n" if c == "\n" else " " for c in match.group())


def _tokens(text):
    clean = _HIDDEN.sub(_mask, text)
    starts = [-1] + [m.start() for m in re.finditer("\n", clean)]
    return [Token(m.group(), bisect_right(starts, m.start()), m.start()) for m in _TOKEN.finditer(clean)]


def _pairs(tokens):
    """Match only delimiters needed by our explicitly limited lexical scan."""
    result, stacks = {}, {"(": [], "[": [], "{": [], "begin": [], "case": []}
    closing = {")": "(", "]": "[", "}": "{", "end": "begin", "endcase": "case"}
    for i, token in enumerate(tokens):
        text = token.text
        key = "case" if text in ("casex", "casez") else text
        if key in stacks:
            stacks[key].append(i)
        elif text in closing and stacks[closing[text]]:
            j = stacks[closing[text]].pop()
            result[j] = i
    return result


def _expr(text, constants):
    """Bounded integer arithmetic only; unknown syntax returns None."""
    def literal(m):
        digits = m.group(3).replace("_", "")
        if re.search(r"[xXzZ?]", digits):
            raise ValueError("four-state constant")
        value = int(digits, {"b": 2, "o": 8, "d": 10, "h": 16}[m.group(2).lower()])
        if m.group(1):
            width = int(m.group(1))
            if not 0 < width <= 63:
                raise ValueError("literal width")
            value &= (1 << width) - 1
        return str(value)
    try:
        # Signed sized-expression rules require an elaborated type context.
        if re.search(r"\d*'[sS]", text):
            return None
        text = re.sub(r"(\d*)'[sS]?([bBoOdDhH])([0-9a-fA-F_xXzZ?]+)", literal, text)
        tree = ast.parse(text.strip(), mode="eval")
        def divide(a, b):
            return (abs(a) // abs(b)) * (-1 if (a < 0) != (b < 0) else 1)
        binary = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
                  ast.FloorDiv: divide, ast.Div: divide,
                  ast.Mod: lambda a, b: a - divide(a, b) * b, ast.LShift: operator.lshift,
                  ast.RShift: operator.rshift, ast.BitAnd: operator.and_,
                  ast.BitOr: operator.or_, ast.BitXor: operator.xor}
        def visit(node, depth=0):
            if depth > 20:
                raise ValueError("expression depth")
            if isinstance(node, ast.Constant) and type(node.value) is int:
                value = node.value
            elif isinstance(node, ast.Name) and type(constants.get(node.id)) is int:
                value = constants[node.id]
            elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
                value = visit(node.operand, depth + 1) * (-1 if isinstance(node.op, ast.USub) else 1)
            elif isinstance(node, ast.BinOp) and type(node.op) in binary:
                a, b = visit(node.left, depth + 1), visit(node.right, depth + 1)
                if isinstance(node.op, (ast.LShift, ast.RShift)) and not 0 <= b <= 63:
                    raise ValueError("shift range")
                value = binary[type(node.op)](a, b)
            else:
                raise ValueError("unsupported expression")
            if abs(value) > 2**63:
                raise ValueError("integer range")
            return value
        return visit(tree.body)
    except (ValueError, TypeError, SyntaxError, ZeroDivisionError, OverflowError):
        return None


def _constants(text):
    pattern = r"\b(?:localparam|parameter)\s+(?:(?:integer|int|logic|bit|signed|unsigned|longint)\s+|\[[^\]]+\]\s*)*([A-Za-z_]\w*)\s*=\s*([^,;)]+)"
    declarations = re.findall(pattern, _HIDDEN.sub(_mask, text))
    # Duplicate names can be in different module/function scopes. Do not guess
    # which declaration applies without a real frontend.
    values, conflicts = {}, {name for name, count in Counter(name for name, _ in declarations).items() if count > 1}
    for _ in range(len(declarations) + 1):
        changed = False
        for name, expression in declarations:
            value = _expr(expression, values)
            if value is None or name in conflicts:
                continue
            if name in values and values[name] != value:
                conflicts.add(name)
                del values[name]
            elif name not in values:
                values[name] = value
                changed = True
        if not changed:
            break
    return values


def _statement_end(tokens, start, pairs, depth=0):
    if start >= len(tokens) or depth > 80:
        return None
    text = tokens[start].text
    if text in ("begin", "case", "casex", "casez"):
        return pairs.get(start, -2) + 1 if start in pairs else None
    if text in ("if", "for", "while", "repeat"):
        opening = start + 1
        if opening >= len(tokens) or tokens[opening].text != "(" or opening not in pairs:
            return None
        end = _statement_end(tokens, pairs[opening] + 1, pairs, depth + 1)
        if text == "if" and end is not None and end < len(tokens) and tokens[end].text == "else":
            end = _statement_end(tokens, end + 1, pairs, depth + 1)
        return end
    for i in range(start, len(tokens)):
        if tokens[i].text == ";":
            return i + 1
        if tokens[i].text in ("endmodule", "endfunction", "endtask"):
            return None
    return None


def _processes(tokens, pairs):
    result = []
    for i, token in enumerate(tokens):
        if token.text not in _PROCEDURAL:
            continue
        start = i + 1
        if start < len(tokens) and tokens[start].text == "@":
            start += 1
            if start < len(tokens) and tokens[start].text == "(":
                start = pairs.get(start, len(tokens)) + 1
            else:
                start += 1
        end = _statement_end(tokens, start, pairs)
        result.append((i, end))
    return result


def _functions(path, tokens):
    result = []
    for i, token in enumerate(tokens):
        if token.text != "function":
            continue
        finish = next((j for j in range(i + 1, len(tokens)) if tokens[j].text == "endfunction"), None)
        if finish is None:
            continue
        header = next((j for j in range(i + 1, finish) if tokens[j].text in ("(", ";")), None)
        if header is None or header == i + 1:
            continue
        name = tokens[header - 1].text
        if not re.fullmatch(r"[A-Za-z_$][\w$]*", name):
            continue
        body = tokens[header:finish]
        result.append({"name": name, "path": path, "line": token.line,
                       "token_count": len(body), "branch_count": sum(t.text in _BRANCH for t in body)})
    return result


def _loop_count(header, constants):
    parts = header.split(";")
    if len(parts) != 3:
        return None
    initial = re.fullmatch(r"\s*(?:(?:int|integer|genvar|longint)\s+)?([A-Za-z_]\w*)\s*=\s*(.+)", parts[0])
    if not initial:
        return None
    name = initial[1]
    condition = re.fullmatch(r"\s*" + re.escape(name) + r"\s*(<=|>=|<|>)\s*(.+)", parts[1])
    if not condition:
        return None
    begin, bound = _expr(initial[2], constants), _expr(condition[2], constants)
    increment = parts[2].strip().replace(" ", "")
    if increment in (name + "++", "++" + name):
        step = 1
    elif increment in (name + "--", "--" + name):
        step = -1
    else:
        m = re.fullmatch(re.escape(name) + r"(\+=|-=)(.+)", increment)
        step = _expr(m[2], constants) if m else None
        if m and m[1] == "-=" and step is not None:
            step = -step
        if not m:
            m = re.fullmatch(re.escape(name) + "=" + re.escape(name) + r"([+-])(.+)", increment)
            step = _expr(m[2], constants) if m else None
            if m and m[1] == "-" and step is not None:
                step = -step
    if begin is None or bound is None or step is None or step == 0:
        return None
    op = condition[1]
    if op in ("<", "<=") and step > 0:
        distance = bound - begin + (op == "<=")
        count = max(0, (distance + step - 1) // step)
    elif op in (">", ">=") and step < 0:
        distance = begin - bound + (op == ">=")
        count = max(0, (distance - step - 1) // -step)
    else:
        return None
    return count if count <= 10**9 else None


def _source_closure(tree, ws, unknowns):
    texts, pending = {}, list(ws["sources"])
    while pending:
        rel = pending.pop(0)
        if rel in texts:
            continue
        path = under(tree, rel)
        if rel not in ws["files"] or not path.is_file():
            unknowns.append({"code": "source_not_in_frozen_closure", "path": rel})
            continue
        if path.stat().st_size > _LIMIT:
            unknowns.append({"code": "source_scan_size_limit", "path": rel, "limit_bytes": _LIMIT})
            texts[rel] = ""
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeError:
            unknowns.append({"code": "non_utf8_source", "path": rel})
            texts[rel] = ""
            continue
        texts[rel] = text
        clean = _HIDDEN.sub(lambda m: m.group() if m.group().startswith('"') else _mask(m), text)
        for match in re.finditer(r'^\s*`include\s+([^\r\n]+)', clean, re.M):
            inc = re.match(r'"([^"\n]+)"', match[1].strip())
            if not inc:
                unknowns.append({"code": "nonliteral_include", "path": rel})
                continue
            candidates = [Path(rel).parent / inc[1], Path(inc[1]), *(Path(d) / inc[1] for d in ws["include_dirs"])]
            normalized = []
            for candidate in candidates:
                try:
                    normalized.append((tree / candidate).resolve().relative_to(tree.resolve()).as_posix())
                except ValueError:
                    continue
            found = next((p for p in normalized if p in ws["files"]), None)
            if found is None:
                unknowns.append({"code": "unresolved_include", "path": rel, "include": inc[1]})
            elif found not in texts:
                pending.append(found)
        macro_clean = _HIDDEN.sub(_mask, text)
        conditional = sorted(set(re.findall(r'`(ifdef|ifndef|elsif|else|endif)\b', macro_clean)))
        if conditional:
            unknowns.append({"code": "conditional_preprocessing_not_evaluated", "path": rel,
                             "directives": conditional})
        directives = {"include", "ifdef", "ifndef", "elsif", "else", "endif", "timescale", "default_nettype", "resetall"}
        macros = sorted(set(re.findall(r'`([A-Za-z_]\w*)', macro_clean)) - directives)
        if macros:
            unknowns.append({"code": "macro_expansion_not_evaluated", "path": rel, "names": macros})
    return texts


def _job_reports(workspace, ws, unknowns):
    """Read owned job reports as attributed measurements, never proof flags."""
    result = []
    jobs = workspace / "jobs"
    if not jobs.is_dir():
        return result
    for directory in sorted(jobs.iterdir()):
        if not directory.is_dir() or directory.is_symlink():
            continue
        try:
            paths = [under(directory, "request.json"), under(directory, "result.json")]
            if any(not p.is_file() for p in paths):
                continue
            if any(p.stat().st_size > 2 * 1024 * 1024 for p in paths):
                unknowns.append({"code": "job_report_size_limit", "job_id": directory.name})
                continue
            request, report = [read_json(p) for p in paths]
            if request.get("workspace_id") != ws["workspace_id"] or report.get("workspace_id") != ws["workspace_id"]:
                unknowns.append({"code": "foreign_job_ignored", "job_id": directory.name})
                continue
            if not report.get("request_identity") or report.get("request_identity") != request.get("request_identity"):
                unknowns.append({"code": "unbound_job_report_ignored", "job_id": directory.name})
                continue
            unsigned = {key: value for key, value in request.items() if key not in ("request_identity", "job_id")}
            if digest(unsigned) != request["request_identity"] or request.get("job_id") != directory.name:
                unknowns.append({"code": "job_request_identity_invalid", "job_id": directory.name})
                continue
            if report.get("variant_identity") != request.get("variant_identity"):
                unknowns.append({"code": "mismatched_job_variant_ignored", "job_id": directory.name})
                continue
            selected = load_variant(workspace, request.get("variant", "baseline"), verify=True)
            if selected["identity"] != request.get("variant_identity"):
                unknowns.append({"code": "stale_job_variant_ignored", "job_id": directory.name})
                continue
            item = {"job_id": directory.name, "evidence": f"jobs/{directory.name}/result.json",
                    "provenance": "job_reported_not_revalidated_by_analyzer",
                    "variant_identity": report.get("variant_identity"), "kind": report.get("kind"),
                    "status": report.get("status"), "measurement_key": report.get("measurement_key")}
            for key in ("elapsed_seconds", "peak_rss_bytes"):
                value = report.get(key)
                if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                    item[key] = value
            statistics = report.get("statistics")
            if isinstance(statistics, dict):
                item["statistics"] = statistics
            result.append(item)
        except (ValueError, OSError) as error:
            unknowns.append({"code": "unreadable_job_report", "job_id": directory.name,
                             "detail": str(error)[:300]})
    return result


def analyze(workspace: Path) -> dict:
    """Analyze verified baseline source and existing job reports; execute nothing."""
    workspace = Path(workspace)
    ws, baseline = load_workspace(workspace), load_variant(workspace, "baseline", verify=True)
    unknowns, observations, hypotheses = [], [], []
    texts = _source_closure(variant_tree(workspace, "baseline"), ws, unknowns)
    scans = {path: (_tokens(text), _constants(text)) for path, text in texts.items()}
    functions = [fn for path, (tokens, _) in scans.items() for fn in _functions(path, tokens)]
    by_name = {}
    for fn in functions:
        by_name.setdefault(fn["name"], []).append(fn)
    ambiguous = sorted(name for name, definitions in by_name.items() if len(definitions) > 1)
    if ambiguous:
        unknowns.append({"code": "function_scope_resolution_unsupported", "names": ambiguous})
    helpers = {name: definitions[0] for name, definitions in by_name.items() if len(definitions) == 1}

    def observe(kind, path, line, **values):
        obj = {"id": digest([kind, path, line, values])[:16], "kind": kind,
               "evidence_kind": "lexical_heuristic", "path": path, "line": line, **values}
        observations.append(obj)
        return obj["id"]

    def propose(kind, evidence, score, reason):
        identity = digest([kind, evidence])[:16]
        hypotheses.append({"id": identity, "kind": kind, "evidence": evidence,
                           "priority_score": score, "confidence": "heuristic", "claim": reason,
                           "not_proven": ["inferred_hardware_replication", "elapsed_time_attribution", "area_or_timing_failure"],
                           "experiment_id": "probe-" + identity})

    for path, (tokens, constants) in scans.items():
        pairs = _pairs(tokens)
        widest, wide_refs = 0, []
        seen_ranges = set()
        for i, token in enumerate(tokens):
            if token.text not in ("wire", "reg", "logic", "bit", "input", "output", "inout"):
                continue
            j = i + 1
            while j < len(tokens) and tokens[j].text in ("wire", "reg", "logic", "bit", "signed", "unsigned", "var"):
                j += 1
            if j >= len(tokens) or tokens[j].text != "[" or j not in pairs or j in seen_ranges:
                continue
            seen_ranges.add(j)
            expression = " ".join(t.text for t in tokens[j + 1:pairs[j]])
            halves = expression.split(":")
            a, b = (_expr(x, constants) for x in halves) if len(halves) == 2 else (None, None)
            width = abs(a - b) + 1 if a is not None and b is not None else None
            ref = observe("packed_range", path, token.line, expression=expression,
                          default_lexical_width_bits=width,
                          source_character_offset=tokens[j].offset)
            if width is None:
                unknowns.append({"code": "packed_range_unresolved", "path": path, "line": token.line,
                                 "expression": expression})
            elif width >= 256:
                widest = max(widest, width)
                wide_refs.append(ref)

        for start, end in _processes(tokens, pairs):
            token = tokens[start]
            if end is None:
                unknowns.append({"code": "procedural_statement_extent_unknown", "path": path, "line": token.line})
                continue
            body = tokens[start:end]
            branches = sum(t.text in _BRANCH for t in body)
            call_counts = Counter(body[i].text for i in range(len(body) - 1)
                                  if body[i].text in helpers and body[i + 1].text == "(")
            ref = observe("procedural_block", path, token.line, procedure=token.text,
                          token_count=len(body), control_keyword_count=branches,
                          helper_callsite_counts=dict(sorted(call_counts.items())))
            if len(body) >= 1500 or branches >= 32:
                evidence = [ref] + wide_refs[:4]
                propose("large_control_process", evidence, len(body) + branches * 20,
                        "A large lexical control body may make process lowering expensive; wide declarations in the same file are context only.")
            for name, count in call_counts.items():
                helper = helpers[name]
                if count >= 8 and (helper["token_count"] >= 64 or helper["branch_count"] >= 2):
                    helper_ref = observe("repeated_helper_callsites", path, token.line, helper=name,
                                         lexical_callsites=count, helper_definition=helper)
                    propose("repeated_helper_expansion", [ref, helper_ref], count * helper["token_count"],
                            "Repeated helper callsites may expand frontend or lowering work; sharing and final cells require measurement.")

        for i, token in enumerate(tokens):
            if token.text != "for" or i + 1 not in pairs or tokens[i + 1].text != "(":
                continue
            close = pairs[i + 1]
            end = _statement_end(tokens, close + 1, pairs)
            if end is None:
                unknowns.append({"code": "loop_body_extent_unknown", "path": path, "line": token.line})
                continue
            header = " ".join(t.text for t in tokens[i + 2:close])
            iterations = _loop_count(header, constants)
            body = tokens[close + 1:end]
            calls = Counter(body[j].text for j in range(len(body) - 1)
                            if body[j].text in helpers and body[j + 1].text == "(")
            if not calls:
                continue
            ref = observe("loop_helper_calls", path, token.line, header=header,
                          default_lexical_iterations=iterations, helper_callsite_counts=dict(sorted(calls.items())),
                          nested_loop_count=sum(t.text == "for" for t in body))
            significant = [name for name in calls if helpers[name]["token_count"] >= 32 or helpers[name]["branch_count"]]
            if iterations is None:
                unknowns.append({"code": "loop_iterations_unresolved", "path": path, "line": token.line, "header": header})
            if significant and (iterations is None or iterations >= 4):
                for name in significant:
                    helper_ref = observe("called_helper_definition", helpers[name]["path"], helpers[name]["line"], **{k: v for k, v in helpers[name].items() if k not in ("path", "line")})
                    score = (iterations if iterations is not None else 1) * calls[name] * helpers[name]["token_count"]
                    propose("loop_helper_expansion", [ref, helper_ref], min(score, 10**9),
                            "A helper inside a loop may cause substantial static expansion; the iteration estimate uses source defaults and does not establish hardware instances or runtime.")

    hypotheses.sort(key=lambda item: (-item["priority_score"], item["id"]))
    experiments = [{"id": h["experiment_id"], "hypothesis_id": h["id"],
                    "scope": {"top": ws["top"], "baseline_identity": baseline["identity"]},
                    "config_template": {"kind": "yosys", "stage": "proc", "timeout_seconds": 30, "memory_mib": 2048},
                    "procedure": "Measure a bounded baseline elaboration/proc probe first. Only if needed, make a sealed, behavior-checked variant addressing this one structure and repeat with identical tool, parameters and budgets.",
                    "falsification": "Comparable complete probes show no material lowering cost or no reproducible improvement. Timeout/incomplete probes identify a budget limit, not improvement or a hardware failure.",
                    "decision": "Retaining the baseline is valid; no sharing transformation is required."}
                   for h in hypotheses]
    jobs = _job_reports(workspace, ws, unknowns)
    return {"api_version": 2, "workspace_id": ws["workspace_id"], "baseline_identity": baseline["identity"],
            "scope": {"variant": "baseline", "top": ws["top"], "sources": ws["sources"], "scanned_files": sorted(texts),
                      "method": "lexical_heuristic", "executes_eda": False,
                      "limits": ["Not a complete SystemVerilog parser or elaborator", "No hierarchy reachability or type/parameter override resolution", "Only simple constant arithmetic and canonical integer for loops are estimated", "Nested loop counts are not multiplied; includes are scanned once, not preprocessed", "Source counts are not cells, memory resources, area, timing or function CPU time", "Existing job reports are attributed inputs, not acceptance decisions"]},
            "heuristic_thresholds": {"wide_packed_range_bits": 256, "large_process_tokens": 1500,
                                     "large_process_control_keywords": 32, "repeated_helper_callsites": 8,
                                     "loop_iteration_candidate": 4},
            "observations": observations, "functions": functions, "hypotheses": hypotheses,
            "experiments": experiments, "unknowns": unknowns, "jobs_summary": jobs}
