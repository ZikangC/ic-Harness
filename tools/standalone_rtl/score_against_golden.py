#!/usr/bin/env python3
"""Score an RTL result binary using the harness cosine/near-tie contract."""
from __future__ import annotations

import argparse
import json
import struct
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from reference import ModelConfig, NanoReference, gen_tokens, gen_weights  # noqa: E402

COS_PASS, ARGMAX_PASS, TIE_REL = 0.98, 0.99, 0.01


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--result", type=Path, required=True, help="full memory dump or [logits..status] slice")
    ap.add_argument("--meta", type=Path, required=True)
    ap.add_argument("--seed", type=int, default=20260706)
    ap.add_argument("--prompt", type=int, default=1, choices=(0, 1))
    ap.add_argument("--group-size", type=int, choices=(64, 128), default=128)
    ap.add_argument("--slice", action="store_true", help="result starts at out_logits_addr")
    args = ap.parse_args()
    meta = json.loads(args.meta.read_text())
    raw = args.result.read_bytes()
    base = 0 if args.slice else meta["out_logits_addr"]
    seq, stride = meta["seq_len"], meta["logit_stride"]
    logits = np.stack([np.frombuffer(raw, dtype="<i4", count=512, offset=base + row * stride)
                       for row in range(seq)])
    arg_off = (meta["out_argmax_addr"] if not args.slice else meta["out_argmax_addr"] - meta["out_logits_addr"])
    chip_argmax = np.frombuffer(raw, dtype="<u4", count=seq, offset=arg_off).astype(np.int64)
    status_off = (meta["out_status_addr"] if not args.slice else meta["out_status_addr"] - meta["out_logits_addr"])
    status = struct.unpack_from("<I", raw, status_off)[0]
    cfg = replace(ModelConfig(), group_size=args.group_size)
    ref_logits, _ = NanoReference(cfg, gen_weights(cfg, args.seed)).forward_sequence(gen_tokens(cfg, args.seed, args.prompt))
    if seq > len(ref_logits):
        raise ValueError(f"image seq_len={seq} exceeds reference prompt length={len(ref_logits)}")
    # A reduced standard image may deliberately contain only the first N prompt
    # tokens.  Score exactly the rows the image requests rather than silently
    # broadcasting against the complete generated prompt.
    ref_logits = ref_logits[:seq]
    a, b = logits.astype(np.float64), ref_logits.astype(np.float64)
    cosine = (a * b).sum(1) / np.maximum(np.linalg.norm(a, axis=1) * np.linalg.norm(b, axis=1), 1e-30)
    picked = b[np.arange(seq), np.clip(chip_argmax, 0, 511)]
    agree = picked >= b.max(1) - TIE_REL * b.std(1)
    passed = status == 0x600DD00D and cosine.min() >= COS_PASS and agree.mean() >= ARGMAX_PASS
    print(json.dumps({"status": f"0x{status:08x}", "cos_min": float(cosine.min()),
                      "cos_mean": float(cosine.mean()), "argmax_agree": float(agree.mean()),
                      "pass": bool(passed)}, indent=2))
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
