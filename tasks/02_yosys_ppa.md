# 任务：Yosys 综合、面积与静态时序估算

> 此文件是任务 2 的占位卡。收到正式任务卡后，再填写 top module、RTL filelist、时钟、
> Liberty 库、功能回归和 PPA 验收阈值。

## 已确认环境

- 容器：`k3demo-eda`
- Yosys：`/opt/eda/bin/yosys`（0.64）
- ABC：`/opt/eda/bin/yosys-abc`
- Verilator：`/opt/eda/bin/verilator`
- ASAP7 NLDM Liberty 目录：`/opt/OpenROAD-flow-scripts/flow/platforms/asap7/lib/NLDM/`
- ASAP7 CCS Liberty 目录：`/opt/OpenROAD-flow-scripts/flow/platforms/asap7/lib/CCS/`

## 待填写的任务契约

- 顶层模块：
- RTL filelist：
- 时钟端口与目标周期：
- reset / 异步时钟约束：
- 使用的 Liberty 文件与 PVT corner：
- 功能回归命令：
- 面积基线、目标和硬上限：
- 时序指标（critical delay 或 slack）基线、目标和硬下限：
- 是否允许修改 testbench / 约束文件：

## 固定原则

任何 PPA 优化都必须先通过功能回归，再报告综合面积和时序估算。没有明确 Liberty 和时钟
约束时，不把 cell 数量或无约束路径当作最终面积/时序结论。
