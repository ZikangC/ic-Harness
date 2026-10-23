# 候选3源码示例

`baseline/rtl/` 是 V5 冻结候选3的十文件综合闭包；`v19/rtl/` 是已采用局部优化的相同闭包。两者仅 `nano_core.sv` 不同。`sources.f` 中路径分别相对于 baseline 或 v19 目录，顶层为 `msh_chip_top`，没有顶层参数覆盖，include 目录为 `rtl`。

`candidate.patch` 保存实际修改，`verify_merge.py` 是原来的源码绑定二值函数证明程序。执行该程序需要实际 Yosys，并且所有 EDA 仍应放在获准的服务器上；查看其 `--help` 了解参数。

该示例不包含完整原生 work/architecture、仿真数据和大证据树。函数级证明的 X/Z 与全设计功能/时序限制保留在 `../../backup/v19/scoped-review-report.json`。

`../../backup/synthesis-sv51/` 是已经启动过的真实运行脚本和历史小记录，包含原运行路径、身份及单次提交保护。它不是一个可在任意路径直接启动的通用综合入口。复现需新建运行身份并绑定这些源码与实际工具/库，不要沿用旧 job_id 或修改活动脚本。
