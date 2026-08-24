#!/usr/bin/env python3
"""Check every RTL token's L0/L1 top-2 decisions against the float reference."""
from __future__ import annotations
import argparse
import re
import sys
from dataclasses import replace
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from reference import ModelConfig, NanoReference, gen_tokens, gen_weights, sigmoid  # noqa: E402

LINE = re.compile(r"TOKEN_AUDIT pos=(\d+) L0_ROUTE=(\d+),(\d+) L1_ROUTE=(\d+),(\d+)")

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rtl-log", type=Path, required=True)
    ap.add_argument("--seed", type=int, default=20260706)
    ap.add_argument("--prompt", type=int, default=1, choices=(0, 1))
    ap.add_argument("--group-size", type=int, default=128, choices=(64, 128))
    ap.add_argument("--seq-len", type=int, help="audit only the first N positions of the selected prompt")
    args = ap.parse_args()
    got = {int(m.group(1)): ((int(m.group(2)), int(m.group(3))), (int(m.group(4)), int(m.group(5))))
           for m in map(LINE.search, args.rtl_log.read_text(errors="replace").splitlines()) if m}
    cfg = replace(ModelConfig(), group_size=args.group_size)
    model = NanoReference(cfg, gen_weights(cfg, args.seed))
    expected: list[tuple[tuple[int, int], tuple[int, int]]] = []
    original_moe = model._moe
    def traced_moe(layer: int, h: np.ndarray) -> np.ndarray:
        p = f"L{layer}.moe."
        score = sigmoid(h @ model.w[p + "router"])
        selected = np.argsort(-(score + model.w[p + "bias"]), kind="stable")[:2]
        if layer == 0:
            expected.append(((int(selected[0]), int(selected[1])), (-1, -1)))
        else:
            l0 = expected[-1][0]
            expected[-1] = (l0, (int(selected[0]), int(selected[1])))
        return original_moe(layer, h)
    model._moe = traced_moe  # type: ignore[method-assign]
    model.forward_sequence(gen_tokens(cfg, args.seed, args.prompt))
    if args.seq_len is not None:
        if not 1 <= args.seq_len <= len(expected):
            raise ValueError(f"--seq-len must be in [1, {len(expected)}]")
        expected = expected[:args.seq_len]
    missing = []
    mismatch = []
    for pos, want in enumerate(expected):
        if pos not in got: missing.append(pos)
        elif got[pos] != want: mismatch.append((pos, got[pos], want))
    print(f"tokens_expected={len(expected)} tokens_audited={len(got)}")
    for pos, got_pair, want_pair in mismatch:
        print(f"MISMATCH pos={pos} rtl={got_pair} reference={want_pair}")
    if missing: print(f"MISSING positions={missing}")
    if mismatch or missing:
        raise SystemExit(1)
    print("PASS all token L0/L1 top-2 selections match reference")

if __name__ == "__main__":
    main()
