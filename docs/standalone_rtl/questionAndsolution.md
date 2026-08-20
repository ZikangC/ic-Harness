# 计算逻辑完成后的问题与解决记录

本文只记录在 golden 结构、标准 image ABI 和完整层间数据依赖已经接通之后，为达到
reference/scorer 所做的对齐工作。所有“通过”均指实际执行过相应仿真或 scorer；未完成的
多 token 结果不会被表述为 golden 通过。

## 已确认的计算结构

golden 路径实现并实际执行以下顺序：untied embedding、L0 KDA + MoE、L1 NoPE MLA +
MoE、output residual、final RMSNorm、untied LM head、lowest-index argmax。KDA state 与
三路 conv history 在 token 间保留；MLA 当前 token 先写 K/V cache，再对历史（含当前）
做 attention；MoE 为 1 shared + 8 routed experts，top-2 的 bias 仅用于选择。

## 已解决问题

### 1. 标准 q/s/z 的 `z` 解码格式错误

**现象**：早期矩阵投影与 Python 明显不一致。

**根因**：标准 image 中 `z` 是每 group、每输出通道一个原始 uint8 byte，错误地按两个
nibble 打包的格式处理会造成所有反量化权重偏差。

**解决**：统一 q4 计算与 loader 使用 byte-per-zero ABI，并用 Python reference 生成的
`emb.q/s/z` 做全量逐字节验证。

### 2. 连续 q/s/z load 的 descriptor/buffer 生命周期错误

**现象**：KDA `q_pre/k_pre/v_pre` 出现相同或串扰的权重结果。

**根因**：发起 load 后 FSM 改变动态 descriptor，loader 仍直接观察组合 descriptor；
同一 buffer 的 done/rearm 握手也不完整。

**解决**：`nano_golden_datapath_adapter.sv` 增加 q/s/z 和 p16 的 request latch 与
`QSZ_REARM/P16_REARM` 生命周期。修复后 q/k/v 签名分别为
`7fd1db16/e67012f4/a4f93072`，与 Python 投影 cosine 约为 1。

### 3. KDA delta state 的乘法在右移前被窄化

**现象**：例如 Q14 的 `beta*k` 在 `16384*8192` 情况下更新为零，state/readout 明显错误。

**根因**：`(a*b) >>> FRAC_W` 在部分上下文按 `DATA_W` 求值，乘积在右移前截断。

**解决**：`kda_delta_head.sv` 使用 64-bit temporary product 的 `mul_q14`，并增加独立
delta-head testbench。修复后 state write/readout 与手工 Q14 one-hot 合同一致。

### 4. KDA step 后错误地重新组合 core

**现象**：同一个 token 的 KDA recurrence 被先进的 state/history 再计算，造成像第二次
update 的结果。

**根因**：KDA output projection 没有锁存 recurrence edge 的 `core`，后续组合逻辑读取到
已更新 state。

**解决**：在 `KDA_STEP` 锁存 `kda_core_reg`，KDA output 投影使用该寄存器。

### 5. L1 router 捕获了错误的数据总线

**现象**：L1 router 使用 shared expert load 后的 `matvec_y`，而非 router load 的输出。

**根因**：router q/s/z buffer 已被后续 named load 复用，组合 `matvec_y` 不再代表 router。

**解决**：在 `L1_ROUTER_CAPTURE` 锁存专用 `l1_router_pre_reg`，route contract 只使用该
寄存器。

### 6. one-token L1 top-2 因 router sigmoid 近似翻转

**现象**：reference 选择 `(2,1)`，RTL 选择 `(2,4)`；第二 selected expert cosine 约
`-0.0072`，使 L1 MoE 输出 cosine 降至约 `0.576`。

**根因**：共享 sigmoid 在 `[0,2]` 只用一个线性段，对约 `1.154` 的 expert 1 比对约
`0.126` 的 expert 4 低估更多；在 bias 参与选择后翻转 near-tie 排序。

**解决**：`moe_shared_top2_contract.sv` 使用 router 专用的细粒度 sigmoid PWL（只影响
router score，不影响 KDA gate 或 SwiGLU）。修复后 L1 选择恢复 `(2,1)`；单 token 实测
`cos_min=0.9841526`、`argmax_agree=1.0`、status 正确，scorer 通过。

## 多 token 审计与当前问题

### 已建立的观测方法

`tb/tb_nano_golden_decoder_top.sv` 在每个 token commit 输出：

- L0/L1 top-2；
- KDA q/k/v signature、core、attention；
- MLA cache 当前写入的 `kc/shared_kr/value` 与 attention context；
- L0 MoE norm/router pre。

`audit_multitoken_routes.py` 将每个 token 的 L0/L1 top-2 与 Python reference 对照；
`compare_multitoken_snapshots.py` 对照 L0 router 输入、MLA cache write 与 context。

### Prompt 1（16 token）结果

全序列可以完成，`status=0x600dd00d`，但尚未通过：

- `cos_min=0.3614148`，`cos_mean=0.8181324`，`argmax_agree=0.5`；
- 首个 route 分叉在 pos2：L0 RTL `(0,4)`，reference `(0,5)`；
- 之后专家不同会改变 L0 output，进而改变 MLA key/value/query 与 L1 route。

### 已排除的首因

针对 pos0–2 的单独标准 image：

- pos0–1 MLA `kc/shared_kr/value` cosine 约 `0.994–0.997`；
- pos2 cache write 才出现大幅偏差，因此 cache 写/读时序不是首因；
- pos2 KDA q/k/v projection cosine 约 1，conv+SiLU cosine 约 `0.998–0.999`，说明
  q/s/z reload 与 conv history 没有丢失或重置；
- pos2 的 L0 `moe_norm=0.988471`、router pre=`0.986604`，却已足以使真实 raw
  `score+bias` 排序由 reference 的 `(0,5)` 变为 RTL 的 `(0,4)`。这里不是 router
  sigmoid 本身再一次翻转：对 RTL raw pre 使用精确 sigmoid 后 expert 4 仍高于 expert 5。

### KDA recurrence 分段比较（pos2）

| 边界 | cosine | 备注 |
|---|---:|---|
| q/k L2 norm | ~0.99895 | 小误差。 |
| alpha | 0.99618 | PWL/固定点误差存在。 |
| beta | max abs ~0.0096 | PWL 误差存在。 |
| head raw | 0.99299 | recurrence 累积后的低幅度 readout。 |
| head onorm | 0.98809 | 低幅度 raw 经 RMSNorm 放大。 |
| KDA core | 0.98518 | gate 后进入 L0 downstream。 |

当前判断是量化与近似误差逐 token 累积，随后被 KDA onorm 放大，并在 MoE near-tie 处
转化为离散 route 错误。这是数值微架构校准问题，但因影响 expert 选择，仍属于功能正确性
工作，而非可无限后置的性能优化。

## 被拒绝的尝试

### 将 KDA internal state 从 Q14 提升到 Q18

**动机**：减少 outer-product state update 的重复截断。

**结果**：pos2 L0 仍为错误 `(0,4)`；同时 token0 的 L1 从正确 `(2,1)` 回归为 `(2,4)`。

**处置**：已回退，当前源码与服务器均恢复为通过 one-token scorer 的 Q14 state 实现。
这说明不能只改变 state fractional width；必须按 token 的 head raw/onorm/gate 及 route
margin 逐项校准，并验证不会破坏已经正确的 token0。

## 后续工作

1. 在不改变 token0 `(L0=(0,3), L1=(2,1))` 的前提下，校准 KDA state update/readout、
   onorm、gate 的舍入和近似。
2. 以 pos2 L0 reference `(0,5)` 恢复为局部验收，再跑完整 prompt 1 all-token route audit。
3. prompt 1 scorer 通过后运行 prompt 0；只有两个 prompt 均满足 harness 阈值，才能宣称
   多 token golden/scorer 通过。
