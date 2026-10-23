#!/usr/bin/env python3
"""Standalone float32 golden model for the Nano-KPU task.

This file intentionally contains the complete model path: deterministic
weight/token synthesis, int4 dequantization, KDA, NoPE MLA, MoE and LM head.
It does not import ``nano-kpu/reference`` at runtime.  Its formulas and
operation ordering match that normative implementation.

Examples:
  python3 reference.py
  python3 reference.py --seed 20260706 --prompt 1 --output /tmp/golden.npz
  python3 reference.py --group-size 64 --seq-stretch 1.25
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np


F32 = np.float32
RMS_EPS = 1e-5
L2_EPS = 1e-6
KDA_GATE_LOWER_BOUND = -5.0
ROUTER_SCALE = 2.828


@dataclass
class ModelConfig:
    name: str = "nano"
    d_model: int = 64
    n_layers: int = 2
    layer_pattern: str = "LF"  # L=KDA, F=MLA
    n_heads: int = 2
    mla_dk: int = 32
    mla_dr: int = 16
    mla_dv: int = 32
    mla_dc: int = 128
    kda_heads: int = 2
    kda_dim: int = 32
    conv_kernel: int = 4
    n_experts: int = 8
    top_k: int = 2
    n_shared: int = 1
    d_ff: int = 32
    attn_res_block: int = 2
    vocab: int = 512
    max_seq: int = 64
    group_size: int = 128
    seq_lens: tuple[int, int] = (48, 16)

    @property
    def layer_types(self) -> list[str]:
        return list(self.layer_pattern * (self.n_layers // len(self.layer_pattern)))


# ---------------------------------------------------------------------------
# Deterministic SplitMix64 PRNG.  Every generated value is addressed by the
# global seed plus a tensor-specific name, just as it is in the task harness.

_M64 = np.uint64(0xFFFFFFFFFFFFFFFF)
_GAMMA = np.uint64(0x9E3779B97F4A7C15)
_C1 = np.uint64(0xBF58476D1CE4E5B9)
_C2 = np.uint64(0x94D049BB133111EB)


def fnv1a64(text: str) -> int:
    h = 0xCBF29CE484222325
    for byte in text.encode("utf-8"):
        h ^= byte
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


def rand_int_range(global_seed: int, stream: str, n: int, lo: int, hi: int) -> np.ndarray:
    """Normative modulo-mapped SplitMix64 integers in inclusive [lo, hi]."""
    seed = np.uint64((global_seed ^ fnv1a64(stream)) & 0xFFFFFFFFFFFFFFFF)
    span = np.uint64(hi - lo + 1)
    index = np.arange(1, n + 1, dtype=np.uint64)
    with np.errstate(over="ignore"):
        value = seed + index * _GAMMA
        value = (value ^ (value >> np.uint64(30))) * _C1
        value = (value ^ (value >> np.uint64(27))) * _C2
        value = value ^ (value >> np.uint64(31))
    return (value % span).astype(np.int64) + lo


# ---------------------------------------------------------------------------
# Weight layout and deterministic image-weight synthesis.

def matmul_shapes(c: ModelConfig) -> dict[str, tuple[int, int]]:
    kd = c.kda_heads * c.kda_dim
    shapes: dict[str, tuple[int, int]] = {}
    for layer, kind in enumerate(c.layer_types):
        p = f"L{layer}."
        if kind == "F":
            shapes.update({
                p + "mla.wc": (c.d_model, c.mla_dc),
                p + "mla.wq_c": (c.d_model, c.n_heads * c.mla_dk),
                p + "mla.wq_r": (c.d_model, c.n_heads * c.mla_dr),
                p + "mla.wk_c": (c.mla_dc, c.n_heads * c.mla_dk),
                p + "mla.wk_r": (c.d_model, c.mla_dr),
                p + "mla.wv": (c.mla_dc, c.n_heads * c.mla_dv),
                p + "mla.o": (c.n_heads * c.mla_dv, c.d_model),
            })
        else:
            shapes.update({
                p + "kda.q": (c.d_model, kd), p + "kda.k": (c.d_model, kd),
                p + "kda.v": (c.d_model, kd), p + "kda.b": (c.d_model, c.kda_heads),
                p + "kda.f1": (c.d_model, c.kda_dim), p + "kda.f2": (c.kda_dim, kd),
                p + "kda.g1": (c.d_model, c.kda_dim), p + "kda.g2": (c.kda_dim, kd),
                p + "kda.o": (kd, c.d_model),
            })
        shapes[p + "moe.router"] = (c.d_model, c.n_experts)
        for expert in ["shared"] + [f"e{i}" for i in range(c.n_experts)]:
            shapes[p + f"moe.{expert}.g"] = (c.d_model, c.d_ff)
            shapes[p + f"moe.{expert}.u"] = (c.d_model, c.d_ff)
            shapes[p + f"moe.{expert}.d"] = (c.d_ff, c.d_model)
    shapes["head"] = (c.d_model, c.vocab)
    return shapes


def small_params(c: ModelConfig) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for layer, kind in enumerate(c.layer_types):
        p = f"L{layer}."
        out += [(p + "res_attn.norm.w", "n16"), (p + "res_attn.proj", "a16"),
                (p + "norm1.w", "n16")]
        if kind == "F":
            out.append((p + "mla.knorm.w", "n16"))
        else:
            out += [(p + "kda.qconv", "a16"), (p + "kda.kconv", "a16"),
                    (p + "kda.vconv", "a16"), (p + "kda.A", "u16"),
                    (p + "kda.dtb", "a16"), (p + "kda.onorm.w", "n16")]
        out += [(p + "res_mlp.norm.w", "n16"), (p + "res_mlp.proj", "a16"),
                (p + "norm2.w", "n16"), (p + "moe.bias", "a16")]
    return out + [("res_out.norm.w", "n16"), ("res_out.proj", "a16"),
                  ("final_norm.w", "n16")]


def small_param_shape(c: ModelConfig, name: str) -> tuple[int, ...]:
    kd = c.kda_heads * c.kda_dim
    if ".mla.knorm" in name:
        return (c.mla_dc,)
    if ".kda.onorm" in name:
        return (c.kda_dim,)
    if name.endswith((".norm1.w", ".norm2.w", ".norm.w")) or name == "final_norm.w":
        return (c.d_model,)
    if name.endswith(".proj"):
        return (c.d_model,)
    if name.endswith("conv"):
        return (kd, c.conv_kernel)
    if name.endswith(".kda.A"):
        return (c.kda_heads,)
    if name.endswith(".kda.dtb"):
        return (kd,)
    if name.endswith(".moe.bias"):
        return (c.n_experts,)
    raise KeyError(name)


_SIGMA_Q_X1000 = 4610
_FUDGE_Q2 = {"b": -6, "f2": -6}


def _scale_q12(fan_in: int, kind: str) -> int:
    value = (4096 * 1000 * 1024) // (_SIGMA_Q_X1000 * math.isqrt(fan_in * 1024 * 1024))
    if kind in _FUDGE_Q2:
        value = int(value * (2.0 ** (_FUDGE_Q2[kind] / 4.0)))
    return max(1, min(32767, value))


def gen_weights(c: ModelConfig, seed: int) -> dict[str, np.ndarray]:
    """Create the exact synthetic packed tensors placed in a k3demo image."""
    weights: dict[str, np.ndarray] = {}
    shapes = matmul_shapes(c)

    # Embedding is special: token-major, two hidden values packed per byte.
    names: list[tuple[str, tuple[int, ...], str]] = [
        ("emb.q", (c.vocab, c.d_model // 2), "q4"),
        ("emb.s", (c.vocab, 1), "s16"),
        ("emb.z", (c.vocab, 1), "z8"),
    ]
    for name, (k, n) in shapes.items():
        group = min(c.group_size, k)
        names += [(name + ".q", (k // 2, n), "q4"),
                  (name + ".s", ((k + group - 1) // group, n), "s16"),
                  (name + ".z", ((k + group - 1) // group, n), "z8")]
    names += [(name, small_param_shape(c, name), "p16") for name, _ in small_params(c)]

    for name, shape, tag in names:
        n = int(np.prod(shape))
        stream = f"{c.name}/{name}"
        if tag == "q4":
            lo = rand_int_range(seed, stream + "/lo", n, 0, 15)
            hi = rand_int_range(seed, stream + "/hi", n, 0, 15)
            weights[name] = ((hi << 4) | lo).reshape(shape)
        elif tag == "z8":
            weights[name] = rand_int_range(seed, stream, n, 6, 9).reshape(shape)
        elif tag == "s16":
            base = name[:-2]
            fan_in = c.d_model if base == "emb" else shapes[base][0]
            scale = _scale_q12(fan_in, base.split(".")[-1])
            jitter = rand_int_range(seed, stream, n, -scale // 4, scale // 4)
            weights[name] = np.maximum(1, scale + jitter).reshape(shape)
        else:
            if name.endswith(".kda.A"):
                limits = (128, 1024)
            elif name.endswith("conv"):
                limits = (-1024, 1023)
            elif name.endswith((".proj", ".moe.bias")):
                limits = (-512, 511)
            elif name.endswith(".kda.dtb"):
                limits = (-2048, 2047)
            else:
                limits = (-2048, 2047)
            weights[name] = rand_int_range(seed, stream, n, *limits).reshape(shape)
    return weights


def gen_tokens(c: ModelConfig, seed: int, prompt: int) -> np.ndarray:
    return rand_int_range(seed, f"{c.name}/tokens/{prompt}", c.seq_lens[prompt], 0, c.vocab - 1)


# ---------------------------------------------------------------------------
# Quantization and numerical primitives.

def unpack_int4(packed: np.ndarray, k: int, n: int) -> np.ndarray:
    packed = packed.reshape(k // 2, n)
    out = np.empty((k, n), dtype=np.uint8)
    out[0::2] = packed & 0x0F
    out[1::2] = packed >> 4
    return out


def dequant_w4(packed, scales, zeros, k: int, n: int, group_size: int) -> np.ndarray:
    group = min(group_size, k)
    q = unpack_int4(np.asarray(packed, np.uint8), k, n).astype(np.int32)
    groups = np.arange(k) // group
    scale = np.asarray(scales, np.int64)[groups].astype(np.int32) * F32(2.0 ** -12)
    zero = np.asarray(zeros, np.uint8)[groups].astype(np.int32)
    return ((q - zero).astype(F32) * scale.astype(F32)).astype(F32)


def dequant_emb(packed, scales, zeros, vocab: int, d_model: int, group_size: int) -> np.ndarray:
    group = min(group_size, d_model)
    packed = np.asarray(packed, np.uint8).reshape(vocab, d_model // 2)
    q = np.empty((vocab, d_model), dtype=np.uint8)
    q[:, 0::2] = packed & 0x0F
    q[:, 1::2] = packed >> 4
    groups = np.arange(d_model) // group
    scale = np.asarray(scales, np.int64)[:, groups].astype(np.int32) * F32(2.0 ** -12)
    zero = np.asarray(zeros, np.uint8)[:, groups].astype(np.int32)
    return ((q.astype(np.int32) - zero).astype(F32) * scale.astype(F32)).astype(F32)


def rmsnorm(x: np.ndarray, weight: np.ndarray) -> np.ndarray:
    x = x.astype(F32)
    mean_square = np.mean(x * x, axis=-1, keepdims=True, dtype=F32)
    return (x / np.sqrt(mean_square + F32(RMS_EPS)) * (F32(1.0) + weight)).astype(F32)


def l2norm(x: np.ndarray) -> np.ndarray:
    norm = np.sqrt(np.sum(x * x, axis=-1, keepdims=True, dtype=F32) + F32(L2_EPS))
    return (x / norm).astype(F32)


def sigmoid(x: np.ndarray) -> np.ndarray:
    return (1.0 / (1.0 + np.exp(-x, dtype=F32))).astype(F32)


def silu(x: np.ndarray) -> np.ndarray:
    return (x / (1.0 + np.exp(-x, dtype=F32))).astype(F32)


def softmax(x: np.ndarray) -> np.ndarray:
    e = np.exp(x - x.max(), dtype=F32)
    return (e / e.sum(dtype=F32)).astype(F32)


# ---------------------------------------------------------------------------
# Model execution.  State is reset once per teacher-forced token sequence.

class NanoReference:
    def __init__(self, cfg: ModelConfig, packed_weights: dict[str, np.ndarray]):
        self.cfg = cfg
        self.w: dict[str, np.ndarray] = {}
        for name, (k, n) in matmul_shapes(cfg).items():
            self.w[name] = dequant_w4(packed_weights[name + ".q"], packed_weights[name + ".s"],
                                      packed_weights[name + ".z"], k, n, cfg.group_size)
        self.w["emb"] = dequant_emb(packed_weights["emb.q"], packed_weights["emb.s"],
                                    packed_weights["emb.z"], cfg.vocab, cfg.d_model, cfg.group_size)
        for name, kind in small_params(cfg):
            scale = {"n16": 2.0 ** -13, "a16": 2.0 ** -11, "u16": 2.0 ** -8}[kind]
            self.w[name] = (np.asarray(packed_weights[name], np.int64) * scale).astype(F32)
        self.dump: dict[str, np.ndarray] | None = None
        self.reset()

    def reset(self) -> None:
        c = self.cfg
        self.pos = 0
        self.kv_k: dict[int, list[np.ndarray]] = {}
        self.kv_v: dict[int, list[np.ndarray]] = {}
        self.kda_state: dict[int, np.ndarray] = {}
        self.conv_hist: dict[int, np.ndarray] = {}
        for layer, kind in enumerate(c.layer_types):
            if kind == "F":
                self.kv_k[layer], self.kv_v[layer] = [], []
            else:
                self.kda_state[layer] = np.zeros((c.kda_heads, c.kda_dim, c.kda_dim), dtype=F32)
                self.conv_hist[layer] = np.zeros((3, c.conv_kernel - 1,
                                                  c.kda_heads * c.kda_dim), dtype=F32)

    def _dump(self, key: str, value: np.ndarray) -> None:
        if self.dump is not None:
            self.dump[f"p{self.pos}.{key}"] = np.asarray(value, F32).copy()

    def _res_mix(self, prefix: np.ndarray, blocks: list[np.ndarray], name: str) -> np.ndarray:
        candidates = list(blocks) + [prefix]
        logits = np.array([rmsnorm(v, self.w[f"{name}.norm.w"]) @ self.w[f"{name}.proj"]
                           for v in candidates], dtype=F32)
        probability = softmax(logits)
        self._dump(f"{name}.p", probability)
        return sum(probability[i] * candidates[i] for i in range(len(candidates))).astype(F32)

    def _conv(self, layer: int, stream: int, x: np.ndarray) -> np.ndarray:
        # stream 0/1/2 selects the independent q/k/v pre-conv history.
        name = "qkv"[stream]
        kernel = self.w[f"L{layer}.kda.{name}conv"]       # [64, 4]
        history = self.conv_hist[layer][stream]             # [3, 64], old -> new
        window = np.concatenate([history, x[None, :]], axis=0)  # [4, 64]
        result = silu((window.T * kernel).sum(axis=-1, dtype=F32))
        self.conv_hist[layer][stream] = np.concatenate([history[1:], x[None, :]], axis=0)
        return result

    def _kda(self, layer: int, h: np.ndarray) -> np.ndarray:
        c = self.cfg
        heads, dh = c.kda_heads, c.kda_dim
        p = f"L{layer}.kda."

        # Current-token projections and three independent 4-tap causal convs.
        q = self._conv(layer, 0, h @ self.w[p + "q"])
        k = self._conv(layer, 1, h @ self.w[p + "k"])
        v = self._conv(layer, 2, h @ self.w[p + "v"])
        beta = sigmoid(h @ self.w[p + "b"])                     # [2]
        f = ((h @ self.w[p + "f1"]) @ self.w[p + "f2"]).reshape(heads, dh)
        z = ((h @ self.w[p + "g1"]) @ self.w[p + "g2"]).reshape(heads, dh)
        alpha = np.exp(F32(KDA_GATE_LOWER_BOUND) * sigmoid(
            self.w[p + "A"][:, None] * (f + self.w[p + "dtb"].reshape(heads, dh))), dtype=F32)

        out_heads = []
        for head in range(heads):
            qh = l2norm(q[head * dh:(head + 1) * dh])
            kh = l2norm(k[head * dh:(head + 1) * dh])
            vh = v[head * dh:(head + 1) * dh]
            state = alpha[head][:, None] * self.kda_state[layer][head]  # decay rows first
            error = vh - kh @ state
            state = state + np.outer(kh * beta[head], error).astype(F32)
            self.kda_state[layer][head] = state.astype(F32)
            head_out = (qh @ state) * F32(1.0 / np.sqrt(dh))
            head_out = rmsnorm(head_out, self.w[p + "onorm.w"]) * sigmoid(z[head])
            out_heads.append(head_out)
        self._dump(f"L{layer}.kda.core", np.stack(out_heads))
        return (np.concatenate(out_heads).astype(F32) @ self.w[p + "o"]).astype(F32)

    def _mla(self, layer: int, h: np.ndarray) -> np.ndarray:
        c = self.cfg
        p = f"L{layer}.mla."
        latent = rmsnorm(h @ self.w[p + "wc"], self.w[p + "knorm.w"])  # [128]
        qc = (h @ self.w[p + "wq_c"]).reshape(c.n_heads, c.mla_dk)
        qr = (h @ self.w[p + "wq_r"]).reshape(c.n_heads, c.mla_dr)
        kc = (latent @ self.w[p + "wk_c"]).reshape(c.n_heads, c.mla_dk)
        kr = h @ self.w[p + "wk_r"]                                  # [16], head-shared
        value = (latent @ self.w[p + "wv"]).reshape(c.n_heads, c.mla_dv)
        query = np.concatenate([qc, qr], axis=-1)                       # [2, 48]
        key = np.concatenate([kc, np.broadcast_to(kr, (c.n_heads, c.mla_dr))], axis=-1)

        # Causal cache includes the current position before its attention read.
        self.kv_k[layer].append(key.astype(F32))
        self.kv_v[layer].append(value.astype(F32))
        keys = np.stack(self.kv_k[layer])                               # [T,2,48]
        values = np.stack(self.kv_v[layer])                             # [T,2,32]
        contexts = []
        scale = F32(1.0 / np.sqrt(c.mla_dk + c.mla_dr))
        for head in range(c.n_heads):
            probability = softmax((keys[:, head, :] @ query[head]) * scale)
            contexts.append(probability @ values[:, head, :])
        self._dump(f"L{layer}.mla.ctx", np.stack(contexts))
        return (np.concatenate(contexts) @ self.w[p + "o"]).astype(F32)

    def _swiglu(self, h: np.ndarray, prefix: str) -> np.ndarray:
        gate = h @ self.w[prefix + "g"]
        up = h @ self.w[prefix + "u"]
        return ((silu(gate) * up) @ self.w[prefix + "d"]).astype(F32)

    def _moe(self, layer: int, h: np.ndarray) -> np.ndarray:
        c = self.cfg
        p = f"L{layer}.moe."
        scores = sigmoid(h @ self.w[p + "router"])                     # [8]
        selected = np.argsort(-(scores + self.w[p + "bias"]), kind="stable")[:c.top_k]
        route_weight = scores[selected]
        route_weight = (route_weight / route_weight.sum(dtype=F32) * F32(ROUTER_SCALE)).astype(F32)
        self._dump(f"L{layer}.moe.sel", np.stack([selected.astype(F32), route_weight]))
        output = self._swiglu(h, p + "shared.")                         # always-on shared expert
        for expert, weight in zip(selected, route_weight):
            output = output + weight * self._swiglu(h, p + f"e{expert}.")
        return output.astype(F32)

    def step(self, token: int) -> np.ndarray:
        """Run one causal decoder position and return its float32 logits [512]."""
        c = self.cfg
        if self.pos >= c.max_seq:
            raise ValueError(f"position {self.pos} exceeds max_seq={c.max_seq}")
        x = self.w["emb"][token].astype(F32)
        blocks: list[np.ndarray] = []       # reset for every token; not cross-token state
        for layer, kind in enumerate(c.layer_types):
            boundary = layer % c.attn_res_block == 0
            prefix = x
            if boundary:
                blocks.append(x)
            mix_in = blocks[:-1] if boundary else blocks
            if mix_in:
                x = self._res_mix(x, mix_in, f"L{layer}.res_attn")
            mixer_input = rmsnorm(x, self.w[f"L{layer}.norm1.w"])
            attention = self._mla(layer, mixer_input) if kind == "F" else self._kda(layer, mixer_input)
            prefix = attention if boundary else (prefix + attention).astype(F32)
            x = self._res_mix(prefix, blocks, f"L{layer}.res_mlp")
            mlp = self._moe(layer, rmsnorm(x, self.w[f"L{layer}.norm2.w"]))
            x = (prefix + mlp).astype(F32)
            self._dump(f"L{layer}.out", x)
        x = self._res_mix(x, blocks, "res_out")
        logits = (rmsnorm(x, self.w["final_norm.w"]) @ self.w["head"]).astype(F32)
        self.pos += 1
        return logits

    def forward_sequence(self, tokens: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        self.reset()
        logits = np.stack([self.step(int(token)) for token in tokens])
        return logits, np.argmax(logits, axis=-1).astype(np.int64)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Standalone Nano-KPU float32 golden model")
    parser.add_argument("--seed", type=int, default=20260706)
    parser.add_argument("--prompt", type=int, default=0, choices=(0, 1))
    parser.add_argument("--group-size", type=int, choices=(64, 128), default=None)
    parser.add_argument("--seq-stretch", type=float, default=1.0)
    parser.add_argument("--output", type=Path, help="write tokens/logits/argmax as .npz")
    parser.add_argument("--dump-intermediates", action="store_true",
                        help="also store residual probabilities, states and selections in --output")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.dump_intermediates and args.output is None:
        raise SystemExit("--dump-intermediates requires --output FILE.npz")
    base = ModelConfig()
    lengths = tuple(max(8, min(base.max_seq, int(round(n * args.seq_stretch)))) for n in base.seq_lens)
    cfg = dataclasses.replace(base, group_size=args.group_size or base.group_size, seq_lens=lengths)
    tokens = gen_tokens(cfg, args.seed, args.prompt)
    model = NanoReference(cfg, gen_weights(cfg, args.seed))
    if args.dump_intermediates:
        model.dump = {}
    logits, argmax = model.forward_sequence(tokens)

    fingerprint = hashlib.sha256(argmax.astype("<i4").tobytes()).hexdigest()
    rms = float(np.sqrt((logits.astype(np.float64) ** 2).mean()))
    print(f"config=nano seed={args.seed} prompt={args.prompt} group_size={cfg.group_size} seq_len={len(tokens)}")
    print(f"tokens={tokens.tolist()}")
    print(f"logits.shape={logits.shape} logits_rms={rms:.8g}")
    print(f"argmax={argmax.tolist()}")
    print(f"argmax_sha256={fingerprint}")

    if args.output:
        if args.output.suffix != ".npz":
            raise SystemExit("--output must use the .npz suffix")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        arrays = {"tokens": tokens, "logits": logits.astype(F32), "argmax": argmax}
        if args.dump_intermediates:
            arrays.update(model.dump)
        np.savez_compressed(args.output, **arrays)
        print(f"wrote {args.output} ({len(arrays)} arrays)")


if __name__ == "__main__":
    main()
