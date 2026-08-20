# Python Golden 对齐契约

本文件以本地 `reference.py` 和参考 harness 的判定规则为准。它不是对现有 smoke
RTL 的背书；其目的是将后续 RTL 改造成同一个计算与输入契约。

## 输入与输出

- 所有标准矩阵：`<name>.q/.s/.z`，int4 沿 fan-in 打包，`s` 为 int16 Q3.12，`z`
  为 uint8；group 取 `min(header.group_size, fan_in)`。
- embedding：token-major `emb.q[512,32]`、`emb.s[512,groups]`、`emb.z[512,groups]`。
- 小参数：norm Q2.13、conv/residual-proj/router-bias/dtb Q4.11、KDA A 为 unsigned
  Q8.8。
- descriptor `name_hash`：`fnv1a64(name) & 0xffffffff`，并非 smoke image 使用的
  私有 `0x4d.../0x53...` 编码。
- 输出：每 token 一行 512 个 int32 logits、一个 uint32 argmax、最后 `0x600dd00d`。

## 计算差异必须消除

现有 `nano_datapath_adapter` 仅用于 smoke，不能作为 golden RTL：

| 部分 | golden 契约 | 当前 smoke 缺口 |
|---|---|---|
| residual | `RMSNorm(candidate, res*.norm) @ res*.proj` 后，对 snapshots+prefix softmax | 当前只接收两个外部 score |
| KDA | `q/k/v/b/f1/f2/g1/g2/o`，A/dtb/onorm，更新 state 后 gated norm | 当前仅有简化 alpha/beta 与无 output gate/norm |
| MLA | `wc,wq_c,wq_r,wk_c,wk_r,wv,o`，latent 128 与 dk+dr=48 key | 当前直接 Q/K/V 投影 |
| MoE | router + 一个 unweighted shared expert + top-2 routed experts | 当前缺 shared expert |
| 数值 | float32 reference；硬件输出以 cosine/argmax 判定 | 当前 PWL 与 Q9.14 未经 layer dump 校准 |

## 判定标准

来自 harness scoring：

- 每一个 run 的最小 row cosine 必须 `>= 0.98`。
- 两个 prompt 全部 token 合并后，near-tie argmax agreement 必须 `>= 0.99`。
- near-tie：RTL 选择的 reference logit 不低于 `max(reference_row) - 0.01*std(reference_row)`。
- status、timeout、memory protocol、weight stream 都必须通过。

## 对齐顺序

1. `build_golden_image.py` 生成 Python golden 的原始输入 image 与 metadata。
2. RTL front-end 改用 FNV descriptor hash，并按全部 reference tensor 读取。
3. 将 residual、KDA、MLA、MoE 改为上表公式；每层导出 dump 进行定位。
4. testbench 导出 result binary；`score_against_golden.py` 按 harness 同一 cosine/
   near-tie 规则判定。
