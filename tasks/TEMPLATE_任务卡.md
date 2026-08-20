# 任务：<用一句中文描述目标>

## 任务目标

<说明要达到的功能或修复的现象。>

## 必读资料

- `docs/standalone_rtl/golden_contract.md`
- `docs/standalone_rtl/questionAndsolution.md`
- `/Users/chenzikang/code/work/k3demo/reference.py`

## 允许与禁止的修改范围

- 允许修改：`rtl/<相关模块>.sv`
- 禁止修改：`harness/`、`harness.toml`、`tb/`、`tools/`、`docs/`、任务文件。

## 诊断与验收命令

```bash
bash scripts/remote_round.sh \
  --task <任务文件名，不带.md> \
  --seed 20260706 \
  --prompt 1
```

如需覆盖另一个 prompt：

```bash
bash scripts/remote_round.sh \
  --task <任务文件名，不带.md> \
  --seed 20260706 \
  --prompt 0
```

## 通过标准

- 状态字正确；
- 每行 logits cosine >= 0.98；
- near-tie argmax agreement >= 0.99。

## 输出语言

所有计划、修改说明、失败分析、验证结论和最终报告使用中文。
保留代码、信号名、路径、工具命令和原始错误文本。
