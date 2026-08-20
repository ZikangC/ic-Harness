#!/usr/bin/env python3
"""Build the exact Python-reference image, independent of nano_kpu RTL.

Weights and tokens come from this workspace's standalone ``reference.py``.
The descriptor/data/header byte layout follows the documented image contract;
the generated metadata is consumed by score_against_golden.py and a future
golden-compatible RTL testbench.
"""
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
from reference import ModelConfig, fnv1a64, gen_tokens, gen_weights, matmul_shapes, small_params, small_param_shape  # noqa: E402


def align16(value: int) -> int:
    return (value + 15) & ~15


def fnv1a32(name: str) -> int:
    return fnv1a64(name) & 0xFFFFFFFF


def tensor_list(cfg: ModelConfig):
    shapes = matmul_shapes(cfg)
    out = [("emb.q", (cfg.vocab, cfg.d_model // 2), "q4"),
           ("emb.s", (cfg.vocab, 1), "s16"),
           ("emb.z", (cfg.vocab, 1), "z8")]
    for name, (fan_in, fan_out) in shapes.items():
        groups = (fan_in + min(cfg.group_size, fan_in) - 1) // min(cfg.group_size, fan_in)
        out += [(name + ".q", (fan_in // 2, fan_out), "q4"),
                (name + ".s", (groups, fan_out), "s16"),
                (name + ".z", (groups, fan_out), "z8")]
    out += [(name, small_param_shape(cfg, name), "p16") for name, _ in small_params(cfg)]
    return out


def as_bytes(array: np.ndarray, tag: str) -> bytes:
    if tag in ("q4", "z8"):
        return np.asarray(array, np.uint8).tobytes()
    return np.asarray(array, dtype="<i2").tobytes()


def build(cfg: ModelConfig, seed: int, prompt: int):
    weights = gen_weights(cfg, seed)
    tokens = gen_tokens(cfg, seed, prompt)
    entries = []
    shapes = matmul_shapes(cfg)
    for name, _, tag in tensor_list(cfg):
        aux = cfg.d_model if name.startswith("emb.q") else 0
        if name.endswith(".q") and not name.startswith("emb."):
            aux = shapes[name[:-2]][0]
        entries.append((name, as_bytes(weights[name], tag), aux))
    desc_addr = 256
    cursor = align16(desc_addr + 16 * len(entries))
    chunks, records = [], []
    for name, data, aux in entries:
        addr = cursor
        chunks.append((addr, data))
        records.append((fnv1a32(name), addr, len(data), aux))
        cursor = align16(cursor + len(data))
    tokens_addr = cursor
    chunks.append((tokens_addr, np.asarray(tokens, dtype="<u4").tobytes()))
    cursor = align16(cursor + 4 * len(tokens))
    stride = align16(cfg.vocab * 4)
    logits_addr = cursor
    cursor = align16(cursor + len(tokens) * stride)
    argmax_addr = cursor
    cursor = align16(cursor + 4 * len(tokens))
    status_addr = cursor
    cursor = align16(cursor + 16)
    layer_bitmap = sum((1 << i) for i, kind in enumerate(cfg.layer_types) if kind == "F")
    words = [0x4B444143, 1, cfg.n_layers, cfg.d_model, cfg.n_heads, cfg.mla_dk,
             cfg.mla_dr, cfg.mla_dv, cfg.mla_dc, cfg.kda_heads, cfg.kda_dim,
             cfg.conv_kernel, cfg.n_experts, cfg.top_k, cfg.n_shared, cfg.d_ff,
             cfg.attn_res_block, cfg.vocab, cfg.max_seq, len(tokens), cfg.group_size,
             layer_bitmap, len(entries), desc_addr, tokens_addr, logits_addr,
             argmax_addr, status_addr, 0, 0, stride, len(tokens)]
    chunks += [(0, struct.pack("<32I", *words) + bytes(128)),
               (desc_addr, b"".join(struct.pack("<4I", *record) for record in records))]
    image = bytearray(cursor)
    for addr, data in chunks:
        image[addr:addr + len(data)] = data
    meta = {"seed": seed, "prompt": prompt, "seq_len": len(tokens), "group_size": cfg.group_size,
            "out_logits_addr": logits_addr, "out_argmax_addr": argmax_addr,
            "out_status_addr": status_addr, "logit_stride": stride, "image_bytes": len(image)}
    return bytes(image), meta


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=Path, required=True, help="byte-per-line readmemh file")
    ap.add_argument("--binary", type=Path, help="optional packed image for scoring/debug")
    ap.add_argument("--meta", type=Path, required=True)
    ap.add_argument("--seed", type=int, default=20260706)
    ap.add_argument("--prompt", type=int, default=1, choices=(0, 1))
    ap.add_argument("--group-size", type=int, choices=(64, 128), default=128)
    args = ap.parse_args()
    cfg = replace(ModelConfig(), group_size=args.group_size)
    image, meta = build(cfg, args.seed, args.prompt)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(f"{byte:02x}\n" for byte in image))
    if args.binary:
        args.binary.write_bytes(image)
    args.meta.write_text(json.dumps(meta, indent=2) + "\n")
    print(f"wrote {len(image)} B, {meta['seq_len']} tokens, {len(tensor_list(cfg))} descriptors")


if __name__ == "__main__":
    main()
