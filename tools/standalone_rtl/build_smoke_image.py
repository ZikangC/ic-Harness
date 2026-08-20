#!/usr/bin/env python3
"""Build a deterministic random image for tb_nano_decoder_top.

The random streams deliberately follow nano_kpu/reference/prng.py: FNV-1a
64-bit stream names followed by SplitMix64.  The memory layout and descriptor
hashes are the independent ABI implemented in weight_stream_frontend.sv.
This is a control/data-movement smoke image, not a trained model or golden.
"""
from __future__ import annotations

import argparse
import math
import struct
from pathlib import Path

MASK64 = (1 << 64) - 1
GAMMA = 0x9E3779B97F4A7C15
C1 = 0xBF58476D1CE4E5B9
C2 = 0x94D049BB133111EB
SEED_DEFAULT = 20260706

OP_EMBED, OP_L0_NORM1, OP_L0_KDA, OP_L0_RESMIX, OP_L0_NORM2 = range(5)
OP_L0_MOE, OP_L1_RESA, OP_L1_NORM1, OP_L1_MLA, OP_L1_RESMIX = range(5, 10)
OP_L1_NORM2, OP_L1_MOE, OP_OUT_RESMIX, OP_FINAL_NORM, OP_HEAD = range(10, 15)
T_EMBED, T_KDA_Q, T_KDA_K, T_KDA_V, T_KDA_A, T_KDA_B, T_KDA_O = range(7)
T_MLA_Q, T_MLA_K, T_MLA_V, T_MLA_O = range(7, 11)
T_MOE_GATE, T_MOE_G, T_MOE_U, T_MOE_D, T_HEAD = range(11, 16)


def fnv1a64(name: str) -> int:
    h = 0xCBF29CE484222325
    for byte in name.encode("utf-8"):
        h = ((h ^ byte) * 0x100000001B3) & MASK64
    return h


class SplitMix:
    def __init__(self, global_seed: int, name: str):
        self.seed = (global_seed ^ fnv1a64(name)) & MASK64
        self.index = 0

    def u64(self) -> int:
        self.index += 1
        z = (self.seed + self.index * GAMMA) & MASK64
        z = ((z ^ (z >> 30)) * C1) & MASK64
        z = ((z ^ (z >> 27)) * C2) & MASK64
        return (z ^ (z >> 31)) & MASK64

    def integer(self, lo: int, hi: int) -> int:
        return lo + self.u64() % (hi - lo + 1)


def align16(value: int) -> int:
    return (value + 15) & ~15


def matrix_hash(op: int, tensor: int, expert: int, section: int) -> int:
    return 0x4D000000 | (op << 13) | (tensor << 5) | (expert << 2) | section


def small_hash(op: int, sub: int) -> int:
    return 0x53000000 | (op << 3) | sub


def pad16(data: bytes) -> bytes:
    return data + b"\0" * (align16(len(data)) - len(data))


def scale_q12(fan_in: int) -> int:
    # Same variance-oriented integer baseline as the reference generator.
    return max(1, min(32767, (4096 * 1000 * 1024) // (4610 * math.isqrt(fan_in * 1024 * 1024))))


def matrix_sections(seed: int, name: str, fan_in: int, fan_out: int) -> tuple[bytes, bytes, bytes]:
    lo, hi = SplitMix(seed, name + "/q/lo"), SplitMix(seed, name + "/q/hi")
    q = bytes((hi.integer(0, 15) << 4) | lo.integer(0, 15) for _ in range((fan_in // 2) * fan_out))
    scale0 = scale_q12(fan_in)
    scale_rng = SplitMix(seed, name + "/s")
    scales = [max(1, scale0 + scale_rng.integer(-scale0 // 4, scale0 // 4)) for _ in range(fan_out)]
    s = struct.pack("<" + "h" * fan_out, *scales)
    z_rng = SplitMix(seed, name + "/z")
    zeros = [z_rng.integer(6, 9) for _ in range(fan_out)]
    z = bytes(zeros[2 * i] | ((zeros[2 * i + 1] if 2 * i + 1 < fan_out else 0) << 4)
              for i in range((fan_out + 1) // 2))
    return pad16(q), pad16(s), pad16(z)


def embedding_sections(seed: int) -> tuple[bytes, bytes, bytes]:
    return matrix_sections(seed, "smoke/emb", 64, 512)  # transpose layout corrected below


def p16(seed: int, name: str, count: int, lo: int, hi: int) -> bytes:
    rng = SplitMix(seed, name)
    return pad16(struct.pack("<" + "h" * count, *(rng.integer(lo, hi) for _ in range(count))))


def build(seed: int, seq_len: int) -> bytes:
    entries: list[tuple[int, bytes, int]] = []

    def add_matrix(op: int, tensor: int, expert: int, name: str, fan_in: int, fan_out: int, embedding: bool = False) -> None:
        if embedding:
            lo, hi = SplitMix(seed, name + "/q/lo"), SplitMix(seed, name + "/q/hi")
            q = bytes((hi.integer(0, 15) << 4) | lo.integer(0, 15) for _ in range(512 * 32))
            scale0 = scale_q12(64)
            sr, zr = SplitMix(seed, name + "/s"), SplitMix(seed, name + "/z")
            s = struct.pack("<" + "h" * 512, *(max(1, scale0 + sr.integer(-scale0 // 4, scale0 // 4)) for _ in range(512)))
            z = bytes(zr.integer(6, 9) for _ in range(512))
            sections = (pad16(q), pad16(s), pad16(z))
        else:
            sections = matrix_sections(seed, name, fan_in, fan_out)
        for section, data in enumerate(sections):
            entries.append((matrix_hash(op, tensor, expert, section), data, fan_in if section == 0 else 0))

    def add_small(op: int, sub: int, name: str, count: int, lo: int, hi: int) -> None:
        entries.append((small_hash(op, sub), p16(seed, name, count, lo, hi), 0))

    add_matrix(OP_EMBED, T_EMBED, 0, "emb", 64, 512, embedding=True)
    add_small(OP_L0_NORM1, 0, "L0.norm1", 64, -2048, 2047)
    for sub, tensor, name in [(0,T_KDA_Q,"L0.kda.q"), (1,T_KDA_K,"L0.kda.k"), (2,T_KDA_V,"L0.kda.v"),
                              (3,T_KDA_A,"L0.kda.a"), (4,T_KDA_B,"L0.kda.b"), (5,T_KDA_O,"L0.kda.o")]:
        fan_out = 1 if tensor == T_KDA_B else 64
        add_matrix(OP_L0_KDA, tensor, 0, name, 64, fan_out)
    for sub, name in enumerate(("L0.kda.qconv", "L0.kda.kconv", "L0.kda.vconv")):
        add_small(OP_L0_KDA, sub, name, 64 * 4, -1024, 1023)
    add_small(OP_L0_RESMIX, 0, "L0.resmix", 8, -512, 511)
    add_small(OP_L0_NORM2, 0, "L0.norm2", 64, -2048, 2047)

    for op, prefix in ((OP_L0_MOE, "L0"), (OP_L1_MOE, "L1")):
        add_matrix(op, T_MOE_GATE, 0, prefix + ".moe.router", 64, 8)
        add_small(op, 0, prefix + ".moe.bias", 8, -512, 511)
        for expert in range(8):
            add_matrix(op, T_MOE_G, expert, f"{prefix}.moe.e{expert}.g", 64, 32)
            add_matrix(op, T_MOE_U, expert, f"{prefix}.moe.e{expert}.u", 64, 32)
            add_matrix(op, T_MOE_D, expert, f"{prefix}.moe.e{expert}.d", 32, 64)

    add_small(OP_L1_RESA, 0, "L1.resa", 8, -512, 511)
    add_small(OP_L1_NORM1, 0, "L1.norm1", 64, -2048, 2047)
    add_matrix(OP_L1_MLA, T_MLA_Q, 0, "L1.mla.q", 64, 96)
    add_matrix(OP_L1_MLA, T_MLA_K, 0, "L1.mla.k", 64, 96)
    add_matrix(OP_L1_MLA, T_MLA_V, 0, "L1.mla.v", 64, 64)
    add_matrix(OP_L1_MLA, T_MLA_O, 0, "L1.mla.o", 64, 64)
    add_small(OP_L1_RESMIX, 0, "L1.resmix", 8, -512, 511)
    add_small(OP_L1_NORM2, 0, "L1.norm2", 64, -2048, 2047)
    add_small(OP_OUT_RESMIX, 0, "out.resmix", 8, -512, 511)
    add_small(OP_FINAL_NORM, 0, "final.norm", 64, -2048, 2047)
    add_matrix(OP_HEAD, T_HEAD, 0, "head", 64, 512)

    desc_addr = 256
    cursor = align16(desc_addr + 16 * len(entries))
    chunks: list[tuple[int, bytes]] = []
    records: list[tuple[int, int, int, int]] = []
    for name_hash, data, aux in entries:
        addr = cursor
        chunks.append((addr, data))
        records.append((name_hash, addr, len(data), aux))
        cursor = align16(cursor + len(data))
    tokens_addr = cursor
    tokens = [SplitMix(seed, f"nano/tokens/{p}").integer(0, 511) for p in range(seq_len)]
    chunks.append((tokens_addr, struct.pack("<" + "I" * seq_len, *tokens)))
    cursor = align16(cursor + 4 * seq_len)
    logit_stride = 512 * 4
    logits_addr = cursor
    cursor = align16(cursor + seq_len * logit_stride)
    argmax_addr = cursor
    cursor = align16(cursor + seq_len * 4)
    status_addr = cursor
    cursor = align16(cursor + 16)
    header_words = [
        0x4B444143, 1, 2, 64, 2, 48, 0, 32, 0, 2, 32, 4, 8, 2, 0, 32,
        0, 512, 64, seq_len, 64, 0x2, len(entries), desc_addr,
        tokens_addr, logits_addr, argmax_addr, status_addr, 0, 0, logit_stride, seq_len,
    ]
    chunks.append((0, struct.pack("<32I", *header_words) + bytes(256 - 128)))
    chunks.append((desc_addr, b"".join(struct.pack("<4I", *record) for record in records)))
    image = bytearray(cursor)
    for addr, data in chunks:
        image[addr:addr + len(data)] = data
    return bytes(image)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--seed", type=int, default=SEED_DEFAULT)
    ap.add_argument("--seq-len", type=int, default=2, choices=range(1, 5))
    args = ap.parse_args()
    image = build(args.seed, args.seq_len)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(f"{byte:02x}\n" for byte in image))
    print(f"wrote {args.output}: {len(image)} bytes, seed={args.seed}, seq_len={args.seq_len}")


if __name__ == "__main__":
    main()
