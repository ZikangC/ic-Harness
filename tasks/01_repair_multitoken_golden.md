# 任务：修复导入的 Nano-KPU golden RTL baseline 的多 token 问题

## 任务目标

`rtl/` 中的 RTL baseline 能通过编译，也已经通过单 token scorer，但它**尚未完成**。
修复多 token 的数值累积问题，最终使 prompt 1 与 prompt 0 都通过 golden scorer。

## 测试序列定义（prompt 不是自然语言）

这里的 `prompt 0` / `prompt 1` 是 `reference.py` 为回归测试定义的两组**确定性 token
序列**，不是用户输入的一段文字。token 由 `seed` 与序列编号生成，具体逻辑是
`gen_tokens(..., seed, f"nano/tokens/{prompt}", ...)`；相同 seed 下每次完全相同。

| 参数 | reference 中的长度 | 用途 |
|---|---:|---|
| `--prompt 1` | 16 token | 较短多 token 回归；本任务的已知首错从这里开始定位。 |
| `--prompt 0` | 48 token | 更长的最终回归，检验更久的 KDA state、卷积 history 与 MLA KV cache 累积。 |

长度来自 `reference.py` 的 `ModelConfig.seq_lens = (48, 16)`，数组下标就是 prompt 编号。
因此完成标准是：相同 seed 下，完整 16-token prompt 1 **和**完整 48-token prompt 0 都通过；
不是 32 token，也不需要人为编写 token 内容。

## 必读资料

开始修改前，先阅读：

- `docs/standalone_rtl/README.md`
- `docs/standalone_rtl/golden_contract.md`
- `docs/standalone_rtl/questionAndsolution.md`
- `/Users/chenzikang/code/work/k3demo/reference.py`

## 已知现象

已知首个多 token 分叉发生在 prompt 1 的 position 2：RTL 的 L0 MoE route 是 `(0,4)`，
reference 是 `(0,5)`。现有记录表明 KDA 定点/PWL 近似的逐 token 累积误差传递到 L0
router 输入，并翻转了一个 near-tie 排序。

这只是定位起点，**禁止**为特定 token、expert 或输入硬编码结果。

## 允许与禁止的修改范围

- 只允许修改 `rtl/`。
- 保持标准 FNV image ABI、顶层 memory protocol、reference tensor 名称和既有
  testbench/harness 契约不变。
- 仅当新增可综合 RTL 文件时，才更新 `rtl/filelist.f`。
- 不得修改 `harness/`、`harness.toml`、`tb/`、`tools/`、`docs/` 或任务文件。

## 诊断与验收命令

使用 `tools/standalone_rtl/` 下的诊断脚本，以及 testbench 输出的 snapshot，定位
**最早出现差异的中间结果**，不要只依据最终 cosine 盲目修改。

每次顶层评分失败后，harness 会自动调用 `auto` 诊断：先审计 L0/L1 route，再对比
多 token 的 KDA、L0 router 与 MLA cache snapshot。也可按需直接调用以下“诊断函数”：

```bash
# 读取当前服务器 run-id，审计每个 token 的 L0/L1 top-2 route。
bash scripts/remote_diagnose.sh --action routes

# 对比 KDA core、L0 MoE 输入、MLA cache write/context 等连续向量。
bash scripts/remote_diagnose.sh --action snapshots

# 仅在单 token 日志含 RTL_Q14 marker 时，对比该 token 的详细边界。
bash scripts/remote_diagnose.sh --action one-token
```

若你是在**手动**排查，聚焦修复后可用以下命令验证 prompt 1：

```bash
bash scripts/remote_round.sh \
  --task 01_repair_multitoken_golden \
  --seed 20260706 \
  --prompt 1
```

当前首个问题在 position 2。快速修复阶段应只运行保留真实状态历史的 3-token 前缀：

```bash
bash scripts/remote_round.sh \
  --task 01_repair_multitoken_golden \
  --seed 20260706 \
  --prompt 1 \
  --seq-len 3
```

不能只运行 position 2 本身：它依赖 position 0、1 留下的 KDA state、卷积 history 与
MLA KV cache。待 pos2 的 route 和连续向量恢复后，去掉 `--seq-len 3`，重新运行完整
16-token prompt 1。

使用自动调度模式时，不需要每轮手动决定前缀长度。启动命令为：

```bash
python3 harness/agent_loop.py \
  --task tasks/01_repair_multitoken_golden.md \
  --adaptive-prefix \
  --initial-seq-len 3 \
  --max-attempts 10
```

调度规则固定为：3-token 前缀通过后立即自动回归完整 16-token prompt 1；若完整 run
出现新的首个 route 分叉 position N，下一轮自动改跑 pos0～posN 的 N+1-token 前缀。
只有完整 prompt 1 通过后，才进入 prompt 0 的最终回归。

prompt 1 通过后，再验证 prompt 0：

```bash
bash scripts/remote_round.sh \
  --task 01_repair_multitoken_golden \
  --seed 20260706 \
  --prompt 0
```

自动模式由 controller 在每轮 OMP 修改后完成上传、仿真、评分与诊断；本机只接收限长的
评分摘要和日志末尾，完整 artifacts 保留在服务器。不要让 DeepSeek 在修改期间自行启动
`remote_round.sh`，以免与 controller 产生重复 run。

DeepSeek 只能运行会自行结束的有限命令。禁止使用 `tail -f`、
`remote_status.sh --follow`、交互式 shell、无限循环或持续等待日志更新的命令；这些是给人
工监控准备的，AI 工具调用会一直等待而无法进入下一步。需要证据时读取限长反馈或执行
`remote_diagnose.sh` 的有限诊断即可。

## 通过标准

不能仅因单 token scorer 通过而宣布完成。prompt 1 和 prompt 0 都必须满足：

- 完成状态字正确；
- 每一行 logits cosine >= 0.98；
- near-tie argmax agreement >= 0.99。

## 输出语言

所有面向用户的计划、修改说明、失败分析、验证结论和最终报告必须使用中文。
保留 RTL 信号名、文件名、工具命令、原始 Verilator/Python 错误文本及数值单位的英文写法。
