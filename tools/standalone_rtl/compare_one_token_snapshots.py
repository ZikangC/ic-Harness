#!/usr/bin/env python3
"""Compare one-token RTL Q14 snapshots against the permitted float reference.

The testbench emits ``RTL_Q14`` vectors at contract boundaries.  This tool
replays exactly one reference token, reports comparable vector errors, and
flags router selection differences separately from continuous-vector cosine.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from reference import (F32, ModelConfig, NanoReference, ROUTER_SCALE, gen_tokens,
                       gen_weights, rmsnorm, sigmoid, softmax)  # noqa: E402

Q = float(1 << 14)


def parse_rtl(path: Path) -> dict[str, np.ndarray]:
    result: dict[str, np.ndarray] = {}
    for line in path.read_text(errors="replace").splitlines():
        words = line.split()
        if len(words) >= 3 and words[0] in ("RTL_Q14", "RTL_Q14_8"):
            result[words[1]] = np.asarray([int(v) for v in words[2:]], np.float32) / Q
    return result


def traced_reference(seed: int, prompt: int, group_size: int) -> tuple[dict[str, np.ndarray], dict[str, object]]:
    cfg = replace(ModelConfig(), group_size=group_size)
    model = NanoReference(cfg, gen_weights(cfg, seed))
    model.dump = {}
    token = int(gen_tokens(cfg, seed, prompt)[0])
    w, out = model.w, {}
    x = w["emb"][token].astype(F32)
    out["embedding"] = x
    blocks = [x]

    # L0 KDA. _kda also records p0.L0.kda.core.
    h = rmsnorm(x, w["L0.norm1.w"]); out["h_norm"] = h
    attention = model._kda(0, h)
    out["kda_core"] = model.dump["p0.L0.kda.core"].reshape(-1)
    out["kda_attention"] = attention

    # L0 residual/MLP.
    def mix(prefix: np.ndarray, name: str) -> np.ndarray:
        candidates = blocks + [prefix]
        logits = np.asarray([rmsnorm(v, w[name + ".norm.w"]) @ w[name + ".proj"]
                             for v in candidates], dtype=F32)
        probability = softmax(logits)
        return sum(probability[i] * candidates[i] for i in range(len(candidates))).astype(F32)

    l0_mlp_mix = mix(attention, "L0.res_mlp"); out["l0_mlp_mix"] = l0_mlp_mix
    l0_norm = rmsnorm(l0_mlp_mix, w["L0.norm2.w"]); out["l0_moe_norm"] = l0_norm
    l0_pre = l0_norm @ w["L0.moe.router"]; out["l0_router_pre"] = l0_pre

    def moe(layer: int, h_in: np.ndarray, prefix: str) -> tuple[np.ndarray, dict[str, object]]:
        scores = sigmoid(h_in @ w[prefix + "router"])
        selected = np.argsort(-(scores + w[prefix + "bias"]), kind="stable")[:2]
        route_weight = (scores[selected] / scores[selected].sum(dtype=F32) * F32(ROUTER_SCALE)).astype(F32)
        shared = model._swiglu(h_in, prefix + "shared.")
        e0 = model._swiglu(h_in, prefix + f"e{int(selected[0])}.")
        e1 = model._swiglu(h_in, prefix + f"e{int(selected[1])}.")
        return (shared + route_weight[0] * e0 + route_weight[1] * e1).astype(F32), {
            "selected": selected, "weight": route_weight, "shared": shared, "e0": e0, "e1": e1,
        }

    l0_moe, l0_route = moe(0, l0_norm, "L0.moe.")
    out.update({"l0_shared_expert": l0_route["shared"], "l0_selected_expert0": l0_route["e0"],
                "l0_selected_expert1": l0_route["e1"], "l0_moe_out": l0_moe})
    l0_out = (attention + l0_moe).astype(F32); out["l0_out"] = l0_out

    # L1 NoPE MLA, residual/MLP, then MoE.
    l1_attn_mixed = mix(l0_out, "L1.res_attn"); out["l1_attn_mixed"] = l1_attn_mixed
    l1_norm1 = rmsnorm(l1_attn_mixed, w["L1.norm1.w"]); out["l1_norm1"] = l1_norm1
    l1_attention = model._mla(1, l1_norm1)
    out["l1_context"] = model.dump["p0.L1.mla.ctx"].reshape(-1)
    out["l1_mla_attention"] = l1_attention
    l1_prefix = (l0_out + l1_attention).astype(F32); out["l1_prefix"] = l1_prefix
    l1_mlp_mix = mix(l1_prefix, "L1.res_mlp"); out["l1_mlp_mix"] = l1_mlp_mix
    l1_norm = rmsnorm(l1_mlp_mix, w["L1.norm2.w"]); out["l1_moe_norm"] = l1_norm
    l1_pre = l1_norm @ w["L1.moe.router"]; out["l1_router_pre"] = l1_pre
    l1_moe, l1_route = moe(1, l1_norm, "L1.moe.")
    out.update({"l1_shared_expert": l1_route["shared"], "l1_selected_expert0": l1_route["e0"],
                "l1_selected_expert1": l1_route["e1"], "l1_moe_out": l1_moe})
    l1_out = (l1_prefix + l1_moe).astype(F32); out["l1_out"] = l1_out
    out_res_mix = mix(l1_out, "res_out"); out["out_res_mix"] = out_res_mix
    final_hidden = rmsnorm(out_res_mix, w["final_norm.w"]); out["final_hidden"] = final_hidden
    diagnostics = {"l0": l0_route, "l1": l1_route}
    return out, diagnostics


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rtl-log", type=Path, required=True)
    ap.add_argument("--seed", type=int, default=20260706)
    ap.add_argument("--prompt", type=int, default=1, choices=(0, 1))
    ap.add_argument("--group-size", type=int, default=128, choices=(64, 128))
    args = ap.parse_args()
    rtl, ref, diag = parse_rtl(args.rtl_log), *traced_reference(args.seed, args.prompt, args.group_size)
    common = [name for name in ref if name in rtl]
    if not common:
        raise SystemExit("No RTL_Q14 snapshots found")
    print("boundary                         cosine      mae       max_abs  max_lane")
    worst = None
    for name in common:
        a, b = rtl[name].astype(np.float64), ref[name].astype(np.float64)
        if a.shape != b.shape:
            print(f"{name:30s} SHAPE RTL={a.shape} REF={b.shape}")
            continue
        err = np.abs(a - b); den = max(np.linalg.norm(a) * np.linalg.norm(b), 1e-30)
        cos = float(a @ b / den)
        lane = int(err.argmax())
        row = (cos, float(err.mean()), float(err.max()), name, lane)
        print(f"{name:30s} {cos: .7f} {err.mean(): .6g} {err.max(): .6g} {lane:8d}")
        if worst is None or cos < worst[0]:
            worst = row
    print(f"worst_continuous={worst[3]} cosine={worst[0]:.7f} max_lane={worst[4]}")
    for layer in ("l0", "l1"):
        d = diag[layer]
        print(f"{layer}_reference_route selected={d['selected'].tolist()} weight={d['weight'].tolist()}")


if __name__ == "__main__":
    main()
