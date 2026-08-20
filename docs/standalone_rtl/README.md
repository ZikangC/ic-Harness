# Standalone Nano Decoder RTL

这是一个独立实现的 Nano decoder RTL golden 对齐工程。golden 路径只参考本工程
`reference.py`、image ABI、harness/scorer 与文档公式；不读取或复用 `nano_kpu` 的
RTL/SystemVerilog 实现。

## 计算与 ABI

golden 数据流为：

```text
token id
  -> untied int4 embedding [64]
  -> L0: residual-mix -> RMSNorm -> KDA -> residual-mix -> RMSNorm -> MoE
  -> L1: residual-mix -> RMSNorm -> NoPE MLA -> residual-mix -> RMSNorm -> MoE
  -> output residual-mix -> final RMSNorm -> untied LM head [512]
  -> logits[512] + lowest-index argmax
```

所有 golden tensor 通过标准 image descriptor 读取：`name_hash` 是真实 tensor 名的
`FNV-1a 64` 低 32 位。矩阵使用 `<name>.q/.s/.z`：int4 沿 fan-in 打包，`s` 是
int16 Q3.12，`z` 是每 group、每输出通道一个原始 uint8 byte；小参数以 p16 读取。
不得在 golden 路径使用旧的私有 `0x4d.../0x53...` hash ABI。

标准 header 的 `descriptor_addr/count`、`tokens_addr`、`seq_len`、`group_size`、
`logits/argmax/status` 地址均由 `nano_golden_decoder_top` 实际使用。

## 两条顶层路径

| 路径 | 顶层 | 用途 |
|---|---|---|
| 保留 smoke | `rtl/nano_decoder_top.sv` | 旧私有 smoke ABI 的回归测试；不代表 golden 对齐。 |
| golden | `rtl/nano_golden_decoder_top.sv` | 标准 FNV descriptor ABI、完整 decoder 计算与 result 写回。 |

以下旧 smoke 模块必须保留且不应被 golden 改动破坏：

- `rtl/nano_decoder_top.sv`
- `rtl/nano_datapath_adapter.sv`
- `rtl/weight_stream_frontend.sv`

## Golden 关键模块

| 模块 | 责任 |
|---|---|
| `image_header_reader.sv` | 读取标准 image header。 |
| `nano_golden_microop_controller.sv` | token read、token begin、logit write 与最终 status 时序。 |
| `nano_golden_datapath_adapter.sv` | 串行复用 descriptor/DMA master，加载 named q/s/z 或 p16 tensor。 |
| `nano_golden_datapath_fsm.sv` | 调度 embedding、L0 KDA/MoE、L1 MLA/MoE、final norm/head。 |
| `residual_mix2_contract.sv` | block snapshot + prefix 的独立 RMSNorm/score/softmax/raw-mix 合同。 |
| `kda_recurrence_contract.sv` / `kda_delta_head.sv` | 三路 causal conv、KDA decay/delta state/update-readout/onorm/gate。 |
| `mla_latent_stage.sv` / `mla_cache_attention_contract.sv` | NoPE latent MLA、current-token cache write、causal attention read。 |
| `moe_shared_top2_contract.sv` | shared expert 加 top-2；bias 只参与选择，raw sigmoid score 决定权重。 |
| `logit_writer.sv` | 逐 token 写 logits、argmax，最后写 `0x600dd00d` status。 |

`rtl/golden_name_hash_pkg.sv` 和 `rtl/golden_tensor_meta_pkg.sv` 分别保存真实
FNV 常量和 q/s/z/p16 metadata；由 `tools/generate_golden_hash_pkg.py` 生成。

## 构建、lint 与 one-token golden

以下命令在服务器 Docker 容器中执行：

```bash
cd /work/k3demo/standalone_rtl

verilator --lint-only --timing -Wall -Wno-fatal \
  -f rtl/filelist.f tb/tb_nano_golden_decoder_top.sv \
  --top-module tb_nano_golden_decoder_top

python3 tools/build_golden_image.py \
  --output tb/nano_golden_p1.hex --meta /tmp/nano_golden_p1.json \
  --seed 20260706 --prompt 1 --group-size 128
```

完整 testbench 编译较慢（实测约 6 分钟）：

```bash
verilator --binary --timing -Wall -Wno-fatal -j 1 \
  -f rtl/filelist.f tb/tb_nano_golden_decoder_top.sv \
  --top-module tb_nano_golden_decoder_top --Mdir /tmp/nano_golden_obj

/tmp/nano_golden_obj/Vtb_nano_golden_decoder_top \
  +IMAGE=tb/nano_golden_p1.hex +DUMP=/tmp/nano_golden_p1.bin
```

对 dump 使用同一参考模型评分：

```bash
python3 tools/score_against_golden.py \
  --result /tmp/nano_golden_p1.bin --meta /tmp/nano_golden_p1.json \
  --seed 20260706 --prompt 1 --group-size 128
```

判定要求为 status 正确、每 token cosine `>= 0.98`，且两个 prompt 合并后的 near-tie
argmax agreement `>= 0.99`。详细契约见 `golden_contract.md`。

## 当前验证状态（2026-08）

- 标准 FNV image ABI、q/s/z loader、p16 loader 已验证；embedding 的 q/s/z 曾与 Python
  reference 全量逐字节一致。
- 单 token 标准 image 已实际通过 scorer：`cos_min=0.9841526`、
  `argmax_agree=1.0`、status=`0x600dd00d`。
- 完整 prompt 1（16 token）可完成状态流和写回，但尚未通过数值/route 审计：存在从 pos2
  开始的 L0 top-2 分叉，随后影响 MLA 输入与 L1。不得将多 token 或两个 prompt 宣称为
  golden/scorer 通过。
- 多 token 诊断工具：`tools/audit_multitoken_routes.py`、
  `tools/compare_multitoken_snapshots.py`、`tools/compare_one_token_snapshots.py`。

数值问题、已完成修复和被拒绝的试验见 `questionAndsolution.md`。

## Legacy smoke

`tb/smoke_image.hex` 与 `tb/tb_nano_decoder_top.sv` 仍用于旧 smoke 路径。它们仅验证
`run -> status(0x600dd00d) -> done`，不能用于标准 golden 结论。
