# 任务 01 归档：Nano-KPU Golden RTL 多 token 修复

归档日期：2026-08-24。

此目录是任务 01 的入口说明，不是源文件副本。为保证任务可随时重启，原始路径保持不变：

- 任务卡：`tasks/01_repair_multitoken_golden.md`
- 迭代报告：`reports/01_repair_multitoken_golden/progress.md`
- 单次运行与快照：`runs/01_repair_multitoken_golden-*`、`runs/agent-01_repair_multitoken_golden-*`
- RTL 与 testbench：`rtl/`、`tb/`
- 验证控制器：`harness/`、`scripts/`

归档以 Git commit 和 tag 为准；该 tag 固定任务 1 结束时的完整可复现代码状态。重新进入任务 1
时，不要移动或复制这些路径，直接使用原任务卡和 `harness/agent_loop.py` 启动即可。

任务 2 必须使用自己的任务卡、报告目录和 verifier，不能改写任务 1 的报告或验收规则。
