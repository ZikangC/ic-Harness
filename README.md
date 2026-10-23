# ic-Harness：v19 备份

2026-10-23 整理，保存区域级结构重构之前的可用版本。controller 和五个配套 skill 来自已验证的 v19 隔离快照；其余独立 skill 来自当前 RTLskill 库。没有把较旧的生产 controller 或 canonical readiness 覆盖回 v19。

## 内容

- `harness/`：controller、运行器、适配器、测试、文档与冻结的五个 skill。入口实际支持 `architecture`、`rtl`、`readiness`，没有新增通用综合入口。
- `RTLskill/`：独立技能库及其必要 runtime。五个配套 skill 与 `harness/skills/` 字节一致；其他模块保持原库版本，未声称它们都完成了本轮集成验收。
- `input/`：当前项目的原始 spec/reference 小型源码输入，作为可选项目示例，不是通用能力的硬编码要求。
- `examples/candidate3/`：V5 候选3基线及 v19 变体的完整十文件综合源码闭包、有序 source list、修改 patch 与二值函数验证程序。
- `backup/`：版本来源、逐文件哈希、v19 小摘要、原试验配置和新服务器综合运行的脚本/小记录。

没有打包服务器 EDA 数据、仿真镜像、模型会话、标准单元库、网表、检查点、大型日志或凭据。原始项目和活动综合没有被移动或修改。

## 使用

需要 Python 3.11+。Python 代码使用标准库及包内模块；controller 的真实运行还需要本地 OMP、使用者自己的认证配置、SSH、Docker 和服务器上的 EDA 工具。相关 JS 扩展由 OMP 运行。

```bash
python3 -B harness/run.py --help
python3 -B harness/tools/check_environment.py --resolve-only
python3 -B RTLskill/rtl-implementation-readiness/scripts/readiness.py --help
```

真实运行前阅读 `harness/config/README.md` 并设置环境、模型和本次预算。备份默认配置取消了旧试验的嵌套 project_name，使用新服务器环境示例；原配置完整留在 `backup/config-v19/`。

## 已验证范围

v19 形成了源码绑定的局部综合成本优化候选、独立工程审阅及受控采用。其完整二值函数证明与局部 3+3 测量支持这个修改；这些证据不等于全芯片 RTL、面积或 STA 验收。详细历史说明见 `backup/v19/report.md`，不能把该历史记录当作当前作业状态。

示例包含十个综合输入，不包含原生候选的完整 work+architecture 证据树。原生候选 SHA 是来源绑定，不能通过这十个文件重新计算。大结果及原始证据仍在服务器。

区域级重构尚未实现：readiness 的常规 overlay 仍限制在原文件集合内。已经讨论确定的下一步见 `backup/NEXT.md`。

上传到 `ZikangC/ic-Harness` 的操作见 `UPLOAD.md`。
