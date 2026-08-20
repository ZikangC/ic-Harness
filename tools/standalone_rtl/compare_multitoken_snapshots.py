#!/usr/bin/env python3
"""Compare targeted per-token L0 and MLA cache-write snapshots to reference."""
from __future__ import annotations
import argparse, re, sys
from dataclasses import replace
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from reference import ModelConfig, NanoReference, gen_tokens, gen_weights, rmsnorm  # noqa: E402

PAT = re.compile(r"TOKEN_Q14(?:_(32|8))? pos=(\d+) (\S+) (.*)")

def parse(path: Path) -> dict[tuple[int, str], np.ndarray]:
    out = {}
    for line in path.read_text(errors="replace").splitlines():
        m = PAT.fullmatch(line)
        if m:
            out[(int(m.group(2)), m.group(3))] = np.asarray(m.group(4).split(), np.float64) / (1 << 14)
    return out

def expected(seed: int, prompt: int, group: int) -> dict[tuple[int, str], np.ndarray]:
    cfg = replace(ModelConfig(), group_size=group)
    model = NanoReference(cfg, gen_weights(cfg, seed))
    out: dict[tuple[int, str], np.ndarray] = {}
    original_moe, original_mla = model._moe, model._mla
    def trace_moe(layer: int, h: np.ndarray) -> np.ndarray:
        if layer == 0:
            p = model.pos
            out[(p, "l0_moe_norm")] = h.copy()
            out[(p, "l0_router_pre")] = (h @ model.w["L0.moe.router"]).copy()
        return original_moe(layer, h)
    def trace_mla(layer: int, h: np.ndarray) -> np.ndarray:
        if layer == 1:
            p, w = model.pos, model.w
            latent = rmsnorm(h @ w["L1.mla.wc"], w["L1.mla.knorm.w"])
            out[(p, "mla_write_kc")] = (latent @ w["L1.mla.wk_c"]).copy()
            out[(p, "mla_write_shared_kr")] = np.tile(h @ w["L1.mla.wk_r"], 2).copy()
            out[(p, "mla_write_value")] = (latent @ w["L1.mla.wv"]).copy()
        return original_mla(layer, h)
    model._moe = trace_moe  # type: ignore[method-assign]
    model._mla = trace_mla  # type: ignore[method-assign]
    model.dump = {}
    model.forward_sequence(gen_tokens(cfg, seed, prompt))
    for p in range(len(gen_tokens(cfg, seed, prompt))):
        out[(p, "kda_core")] = model.dump[f"p{p}.L0.kda.core"].reshape(-1)
        out[(p, "mla_context")] = model.dump[f"p{p}.L1.mla.ctx"].reshape(-1)
    return out

def main() -> None:
    ap = argparse.ArgumentParser(); ap.add_argument("--rtl-log", type=Path, required=True)
    ap.add_argument("--seed", type=int, default=20260706); ap.add_argument("--prompt", type=int, default=1, choices=(0,1))
    ap.add_argument("--group-size", type=int, default=128, choices=(64,128)); args = ap.parse_args()
    rtl, ref = parse(args.rtl_log), expected(args.seed, args.prompt, args.group_size)
    print("pos boundary                 cosine    max_abs")
    for key in sorted(set(rtl) & set(ref)):
        a, b = rtl[key], ref[key]
        if a.shape != b.shape: print(key, "SHAPE", a.shape, b.shape); continue
        cos = a @ b / max(np.linalg.norm(a)*np.linalg.norm(b), 1e-30)
        print(f"{key[0]:3d} {key[1]:24s} {cos: .6f} {np.max(np.abs(a-b)):.6f}")

if __name__ == "__main__": main()
