# Nano-KPU RTL harness

这个项目把 `~/code/work/k3demo/reference.py` 作为唯一的浮点 golden source，形成以下受控闭环：

```text
任务描述 -> omp/DeepSeek 修改 rtl/ -> 生成 golden/image -> 仿真 -> cosine 评分 -> 失败信息回灌
```

验收条件是：仿真已正常完成、状态字为 `0x600dd00d`、每个 token 的 logits cosine
不低于 `0.98`，并且 near-tie argmax 一致率不低于 `0.99`。这与 k3demo 自带的
`score_against_golden.py` 的语义完全一致。

## 初始化

1. 编辑 `harness.toml` 的 `verification.command`。该命令在服务器的
   `k3demo-eda` 容器中执行，必须使用给出的
   `{image}` 和 `{dump}`，跑完后将完整的 memory dump 写到 `{dump}`。
2. 把需要实现的子模块任务写到 `tasks/`；从 `tasks/example_kda.md` 开始即可。
3. 先运行一次基准验证：

```bash
cd ~/code/work/RTLharnees
bash scripts/remote_round.sh --task example_kda --seed 20260706 --prompt 1
```

若系统尚未安装仿真器或你还未提供 DUT，这一步会失败，但会保留可复用的
`runs/<run-id>/golden.npz`、`image.hex` 与 `meta.json`。

## 让 DeepSeek 自动迭代

```bash
cd ~/code/work/RTLharnees
python3 harness/agent_loop.py --task tasks/example_kda.md --max-attempts 6
```

每轮模型只允许改动 `rtl/` 和 `tb/`。harness 把本轮验证摘要、完整日志位置和上一轮
diff 传回模型；达到阈值会停止，达到次数上限则以非零状态退出并保留全部证据。

> 请不要在 `harness.toml` 中加入 `--auto-approve`。OMP 仍保留自己的工具审批，
> 而 Python harness 仅会执行你明确配置的验证命令。

## 验证命令契约

`verification.command` 中可以使用以下占位符（命令在容器内执行）：

| 占位符 | 含义 |
| --- | --- |
| `{image}` | 给 Verilog `$readmemh` 使用的 byte-per-line image |
| `{dump}` | DUT 跑完后应写出的完整 memory dump |
| `{meta}` | image 的地址和长度元数据 |
| `{rtl_dir}` | 本项目的 `rtl/` |
| `{tb_dir}` | 本项目的 `tb/` |
| `{build_dir}` | 当前 run 的构建目录 |

命令完成后，harness 会调用 k3demo 原有的 `score_against_golden.py`。它读取 `{dump}`
和 `{meta}`，所以不接受仅打印在终端里的 logits。

## 与现有 k3demo RTL 对接

当前 `rtl/`、`tb/` 已导入 `k3demo/standalone_rtl` 的 golden baseline。它已通过单-token
scorer，但多 token 在 prompt 1 的 pos2 有已知 L0 MoE route 分叉；详见
`docs/standalone_rtl/questionAndsolution.md`。请从 `tasks/01_repair_multitoken_golden.md`
开始，不要把这个 baseline 当作最终通过版本。

## 本地控制、服务器执行

本地运行 OMP/DeepSeek 并保持 `rtl/`、`tb/` 作为唯一源码；
`scripts/remote_round.sh` 则将本轮必要文件同步到
`txjg7-2@192.168.0.13:/home/txjg7-2/ic-project/k3harness`。脚本随后在
`k3demo-eda:/work/k3harness` 中运行 `harness/run.py`，最后只取回当前
run 的限长文字反馈（评分摘要与关键日志末尾）。完整 image、dump、编译产物和详细日志
保留在服务器 `runs/<run-id>/`，不会回传到本地。服务器不会成为另一个需要手工维护的代码副本。

## 迭代记录与实时监控

`agent_loop.py` 每轮持续追加到 `reports/<任务名>/progress.md`：任务、时间、OMP 退出码、
代码改动、统一 diff 与紧凑评分结果均被记录。服务器的完整结果始终留在远端。

随时查看远端状态：

```bash
bash scripts/remote_status.sh
```

实时跟随当前 run 的 Verilator 编译/仿真日志（`Ctrl-C` 仅停止查看，不中断服务器任务）：

```bash
bash scripts/remote_status.sh --follow
```
