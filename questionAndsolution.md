# RTL Harness 问题与解决记录

本文件记录会影响 RTL harness 正确性、自动调度、远程运行或结果解释的工程问题。它不是逐轮
实验报告；每轮具体的 RTL 修改、diff 与评分结果保存在 `reports/<task>/progress.md`。

更新原则：记录可复用的问题、可验证的根因、已经采取的修复和避免复发的规则；不记录单纯的
命令拼写、当前目录等低价值操作失误。

## 2026-08-20：多 token baseline 的首个离散分叉

### 现象

prompt 1 的 3-token 前缀可以正常生成 image、编译、仿真和评分，但评分失败：

- `cos_min=0.3614145`
- `argmax_agree=0.6666667`
- 首个 route 分叉为 `pos=2`：RTL 的 L0/L1 route 为 `((0,4),(5,4))`，reference 为
  `((0,5),(3,1))`。

### 解释与处理

这证明验证链路正常，也确认 bug 真实存在。pos0/pos1 的连续数值误差在 near-tie router
输入处被放大为 pos2 的离散 expert 选择变化，随后 MLA cache/context 大幅发散。修复时应先
对齐最早的 KDA、归一化、PWL/定点计算和 state/cache 更新，不得为特定 token 或 expert
硬编码。

### 固定策略

先运行 `pos0~pos2` 的 3-token 前缀，保留真实的 state history；前缀通过后自动回归完整
prompt 1。完整 run 的新首错为 position N 时，下一轮自动缩回 N+1 token 前缀。

## 2026-08-20：前缀 image 生成器路径与同步目录不一致

### 现象

执行 `remote_round.sh ... --seq-len 3` 时，reference 成功而 image 生成失败：

```text
build_golden_image.py: error: unrecognized arguments: --seq-len 3
```

### 根因

服务器同时存在旧的 `standalone_rtl/tools/` 和每轮同步的
`tools/standalone_rtl/` 两份工具。`harness.toml` 仍指向旧目录；旧版 image builder 没有
`--seq-len`，而新同步版本有。

### 修复与预防

将 `harness.toml` 的 `image_builder` 和 `scorer` 统一为：

```toml
image_builder = "tools/standalone_rtl/build_golden_image.py"
scorer = "tools/standalone_rtl/score_against_golden.py"
```

之后同步后在容器内核对配置和 `--seq-len` 参数，不再依赖服务器遗留的旧工程副本。

## 2026-08-20：单 token 诊断与 `--seq-len` 参数不兼容

### 现象

诊断调度统一把 prefix 长度传给所有诊断工具，但 legacy
`compare_one_token_snapshots.py` 没有 `--seq-len` 参数。

### 修复与预防

`harness/diagnose.sh` 只给多 token 的 `routes`、`snapshots` 工具传递 `--seq-len`；
`one-token` 保持原有参数。这样 prefix 诊断和单 token 边界诊断都可正常运行。

## 2026-08-20：AI 被人工实时监控命令无限阻塞

### 现象

第 1 轮 OMP/DeepSeek 调用了：

```bash
bash scripts/remote_status.sh --follow
```

该选项内部使用 `tail -f` 跟随一份已完成 run 的 `simulation.log`。日志不会再更新，工具调用
不返回，导致 agent 长时间既不修改 RTL 也不启动下一次验证。

### 根因

`--follow` 面向人工终端：人可以按 `Ctrl+C` 退出；AI 以同步工具调用方式运行命令，会等待
命令自然结束。原来的 agent 约束禁止其自行运行 `remote_round.sh`，但未禁止无限跟随命令。

### 修复与预防

在 `harness/agent_loop.py` 的 agent 提示和任务卡中加入强制规则：只允许会自行结束的有限
命令；禁止 `tail -f`、`remote_status.sh --follow`、交互式 shell、无限循环和持续等待日志
更新的命令。监控由用户在独立终端执行，AI 只读取限长反馈或执行有限的诊断命令。

## 2026-08-20：远程产物回传量控制

### 目标

远程仿真产生的 image、dump、obj_dir 和完整日志不应回传本机；本机仍需有足够证据让 AI
进行下一轮修复。

### 实现

`scripts/remote_round.sh` 将完整 artifacts 保留在服务器的 `runs/<run-id>/`，仅把
`summary.json`、各日志末尾和诊断末尾合成为本地的 `runs/<run-id>/feedback.txt`。`agent_loop.py`
以该紧凑反馈作为下一轮证据，同时在 `reports/<task>/progress.md` 记录修改与结果。

## 检查清单

开始一次新任务或发现异常时，依次确认：

1. `harness.toml` 指向每轮会同步的工具目录，而不是服务器遗留副本。
2. 先手动跑一次已知 baseline，确认 reference、image、仿真、评分、自动诊断全链路可用。
3. 有跨 token state 的首错时，使用从 pos0 开始的最短因果前缀，而不是孤立运行出错 token。
4. AI 的命令必须有限并自行结束；实时查看使用人的独立终端，不作为 AI 工具调用。
5. 检查本地 `feedback.txt` 前先确认该 run 已结束；完整服务器 artifacts 默认不回传。

## 2026-08-21：长序列完整回归在评分前超时

### 现象

prompt 0 的完整 48-token run 返回 `simulation_exit=124`，且 `simulation.log` 写有
`HARNESS_TIMEOUT after 1800s`。最后一个 `TOKEN_AUDIT` 是 pos26；route 诊断同时报告
`MISSING positions=[27,...,47]`，因此 scorer 未执行，不能将该 run 解释为数值失败。

### 根因与处理

`harness.toml` 的 `[verification].timeout_seconds` 原为 1800。该预算包含同一条命令中的
Verilator 编译和仿真，并非只限制仿真。按实际进度外推完整 48 token 约需 3300 秒；为容纳
服务器负载波动并把超时作为“疑似停滞”的保护线，调至 5000 秒（约 83 分钟）。每次
`remote_round.sh` 都会同步该配置到服务器。

### 如何读取自动 route 诊断

本机查看 `runs/<run-id>/feedback.txt` 中的 `=== diagnostics/routes.log ===` 段；它只保留
末尾的紧凑结果。服务器保留完整文件：主机
`/home/txjg7-2/ic-project/k3harness/runs/<run-id>/diagnostics/routes.log`，容器内对应
`/work/k3harness/runs/<run-id>/diagnostics/routes.log`。其中 `MISMATCH pos=N` 表示首个
观察到的离散 route 分叉，`MISSING` 表示 run 在到达这些 token 前已经结束或超时。

## 2026-08-24：run-id 命名变化导致 controller 读取陈旧反馈

### 现象

run-id 改为 `任务--时间--prompt--范围--pid` 后，`agent_loop.py` 仍按目录名字典序选择
“最后一个”反馈。旧格式 `任务-时间-pid` 在排序中会排在新格式之后，导致 controller 持续读取
旧的超时反馈，即使新前缀验证已通过，也不会升级到完整回归。

### 修复与预防

`latest_feedback()` 已改为按 `feedback.txt` 的实际修改时间选择最新文件：

```python
return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None
```

以后 run-id 可以继续使用人类可读格式；“最新结果”必须依据生成时间，而不是字符串排序。
