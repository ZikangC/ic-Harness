#!/usr/bin/env python3
"""Paired, source-bound functional proof for the mergev/mergev8 rewrite.

Reads the real function bodies from each selected tree, builds a *combinational*
miter with base/src/off/wid as free top-level inputs, and proves all output bits
equivalent with Yosys `sat -verify -prove-asserts` (a complete 2-state proof over
the whole function domain, not fixed windows).

Scope/assumptions recorded in the output JSON:
  - binary (2-state) semantics only; X/Z behaviour of the changed control is not
    modelled and is NOT claimed.
  - off, wid are free 7-bit legal control inputs (all 128*128 values); base and
    src are free full-width data (all 2**1024 / 2**512 values) for the proven
    function outputs.
  - the two functions are pure combinational and have no side effects; the rest
    of the file is byte-identical outside the changed function bodies.
"""
import argparse, hashlib, json, re, subprocess, sys

BASELINE_SHA = "3763cc95037a83d1d51c42cefe9abc795768e48c3138f372e4d20e56478cc79e"
CANDIDATE_SHA = "db019c536f5d60c9b9399ead1110c6aff235e1262d76098dd974c9e5170d8423"
MARGIN = 24  # bytes around the body kept for the residue identity check


def sha_bytes(b):
    return hashlib.sha256(b).hexdigest()


def extract(path, width, name):
    text = open(path, "r", encoding="utf-8").read()
    pat = re.compile(
        r"function\s+automatic\s+logic\s*\[" + str(width - 1) + r":0\]\s+"
        + re.escape(name) + r"\s*\([^;]*?\)\s*;",
        re.S,
    )
    m = pat.search(text)
    if not m:
        raise SystemExit("could not locate function %s width %d in %s" % (name, width, path))
    end = text.find("endfunction", m.end())
    if end < 0:
        raise SystemExit("unterminated function %s in %s" % (name, path))
    end += len("endfunction")
    return text, m.start(), end, text[m.start():end]


def renamed(body, old, new):
    out, n = re.subn(r"\b" + old + r"\b", new, body)
    if n < 1:
        raise SystemExit("no identifier %s in extracted body" % old)
    return out


def residue(text, spans):
    pieces, last = [], 0
    for s, e in spans:
        pieces.append(text[last:s])
        last = e
    pieces.append(text[last:])
    # ignore only whitespace-only differences introduced by the removed span
    return "".join(pieces).strip()


def module(name, body16, body8, f16, f8):
    return (
        "module %s (\n"
        "  input logic [1023:0] base,\n"
        "  input logic [1023:0] src,\n"
        "  input logic [6:0] off,\n"
        "  input logic [6:0] wid,\n"
        "  output logic [1023:0] y16,\n"
        "  output logic [511:0] y8\n"
        ");\n%s\n%s\n"
        "always_comb begin\n"
        "  y16 = %s(base, src, off, wid);\n"
        "  y8  = %s(base, src, off, wid);\n"
        "end\n"
        "endmodule\n"
    ) % (name, body16, body8, f16, f8)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--yosys", required=True)
    a = ap.parse_args()

    bpath = a.baseline + "/rtl/nano_core.sv"
    cpath = a.candidate + "/rtl/nano_core.sv"
    braw = open(bpath, "rb").read()
    craw = open(cpath, "rb").read()
    bsha, csha = sha_bytes(braw), sha_bytes(craw)
    if bsha != BASELINE_SHA:
        raise SystemExit("baseline nano_core.sv identity mismatch: %s" % bsha)
    if csha != CANDIDATE_SHA:
        raise SystemExit("candidate nano_core.sv identity mismatch: %s" % csha)

    btext, bs16, be16, bf16 = extract(bpath, 1024, "mergev")
    btext, bs8, be8, bf8 = extract(bpath, 512, "mergev8")
    ctext, cs16, ce16, cf16 = extract(cpath, 1024, "mergev")
    ctext, cs8, ce8, cf8 = extract(cpath, 512, "mergev8")

    b_res = residue(btext, [(bs16, be16), (bs8, be8)])
    c_res = residue(ctext, [(cs16, ce16), (cs8, ce8)])
    if b_res != c_res:
        raise SystemExit("files differ outside the two changed function bodies")

    gold = module("gold", renamed(bf16, "mergev", "mergev_g"), renamed(bf8, "mergev8", "mergev8_g"),
                  "mergev_g", "mergev8_g")
    gate = module("gate", renamed(cf16, "mergev", "mergev_c"), renamed(cf8, "mergev8", "mergev8_c"),
                  "mergev_c", "mergev8_c")

    gpath = a.outdir + "/gold_merge.sv"
    tpath = a.outdir + "/gate_merge.sv"
    ypath = a.outdir + "/proof.ys"
    open(gpath, "w").write(gold)
    open(tpath, "w").write(gate)
    open(ypath, "w").write(
        "read_verilog -sv %s\nread_verilog -sv %s\n"
        "proc\nopt_clean\n"
        "miter -equiv -flatten -make_assert gold gate miter\n"
        "hierarchy -top miter\nflatten\nopt_clean\n"
        "stat\n"
        "sat -verify -prove-asserts -show-inputs miter\n" % (gpath, tpath)
    )

    proc = subprocess.run([a.yosys, "-s", ypath], capture_output=True, text=True)
    log = (proc.stdout or "") + (proc.stderr or "")
    lp = a.outdir + "/proof.log"
    open(lp, "w").write(log)

    proved = "no model found: SUCCESS" in log
    assert_cells = len(re.findall(r"\$assert", log))
    no_dff = not re.search(r"\$dff", log)
    ok = proc.returncode == 0 and proved and assert_cells > 0 and no_dff

    report = {
        "kind": "mergev_pair_function_proof_v1",
        "scope": "complete combinational 2-state function contract of mergev/mergev8, "
                 "all 128x128 (off,wid) and all base/src data; no sequential state",
        "baseline_path": bpath, "candidate_path": cpath,
        "baseline_sha256": bsha, "candidate_sha256": csha,
        "baseline_function_sha256": {"mergev": sha_bytes(bf16.encode()), "mergev8": sha_bytes(bf8.encode())},
        "candidate_function_sha256": {"mergev": sha_bytes(cf16.encode()), "mergev8": sha_bytes(cf8.encode())},
        "rest_of_file_identical": True,
        "yosys": a.yosys, "yosys_script": ypath, "yosys_log": lp,
        "yosys_returncode": proc.returncode,
        "sat_success_marker": proved,
        "assert_references": assert_cells,
        "dff_in_proof": not no_dff,
        "assumptions": [
            "binary (2-state) semantics only; X/Z propagation is not modelled or claimed",
            "off and wid are free legal 7-bit function inputs; base and src are free full-width data",
            "functions are pure combinational and are applied to the cited original file bodies",
            "reset/state behaviour of the enclosing design is outside this proof",
        ],
        "proof_passed": bool(ok),
    }
    open(a.outdir + "/verify.json", "w").write(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if ok:
        print("MERGE_FUNCTION_PROOF_PASSED")
        return 0
    print("MERGE_FUNCTION_PROOF_FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
