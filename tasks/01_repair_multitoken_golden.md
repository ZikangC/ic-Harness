# 任务：修复导入的 Nano-KPU golden RTL baseline 的多 token 问题

## 任务目标

`rtl/` 中的 RTL baseline 能通过编译，也已经通过单 token scorer，但它**尚未完成**。
修复多 token 的数值累积问题，最终使 prompt 1 与 prompt 0 都通过 golden scorer。

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

每次聚焦修复后，先验证 prompt 1：

```bash
bash scripts/remote_round.sh \
  --task 01_repair_multitoken_golden \
  --seed 20260706 \
  --prompt 1
```

prompt 1 通过后，再验证 prompt 0：

```bash
bash scripts/remote_round.sh \
  --task 01_repair_multitoken_golden \
  --seed 20260706 \
  --prompt 0
```

每轮下载的 `runs/.../summary.json`、`simulation.log`、`score.log` 是下一轮分析依据。

## 通过标准

不能仅因单 token scorer 通过而宣布完成。prompt 1 和 prompt 0 都必须满足：

- 完成状态字正确；
- 每一行 logits cosine >= 0.98；
- near-tie argmax agreement >= 0.99。

## 输出语言

所有面向用户的计划、修改说明、失败分析、验证结论和最终报告必须使用中文。
保留 RTL 信号名、文件名、工具命令、原始 Verilator/Python 错误文本及数值单位的英文写法。
