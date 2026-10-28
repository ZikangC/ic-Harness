# ic-Harness：Harness_final

本版本在 `codex/backup-v19-20261023` 的 v19 提交 `d0ead7f` 上继续更新，当前分支为 `Harness_final`。保留原有独立技能库、示例与备份，更新 `harness/`，并加入当前项目的映射完成报告。

## 相对 v19 的主要变化

- Readiness 支持显式区域策略下的跨文件候选：模块新增、修改、删除及有序源文件列表共同绑定，集成时使用发布日志和回滚保护。
- 增加结构健康证据整理与交付，区分既有问题、新增问题和未知范围；局部检查不等同于整设计验收。
- 增加原生 RTL 结构检查及候选放行门槛，绑定编译声明、源码、工具和实际作业证据；执行快速 lint 与 Yosys 结构检查。
- 区分 `change` 与 `candidate` 审阅范围，局部修改审阅通过不授予完整 RTL 阶段放行。
- 修复执行器由候选来源和流程节点选择，次数限制及审阅状态跨中断保留；增强作业源码检查失败的分类与恢复。
- 补充回归测试、当前环境配置、`completion-report.md` 和 `harness/still_problem.md`。

`harness/skills/` 是本次运行使用的冻结技能；独立的 `RTLskill/` 保留上一版本，两者不再保证字节一致。`backup/`、`examples/` 和下面的 v19 说明保留历史上下文，不代表当前版本的验收结论。`UPLOAD.md` 也是 v19 的历史上传说明。

## 当前使用与限制

需要 Python 3.11+；真实运行还需配置使用者自己的 OMP、认证、SSH、Docker 和服务器 EDA 工具。

```bash
python3 -B harness/run.py --help
python3 -B harness/tools/check_environment.py --resolve-only
```

先阅读 `harness/config/README.md`、`harness/docs/rtl_flow.md` 及 `harness/docs/readiness-integration.md`。Readiness 默认不自动启用；区域策略需要明确配置，带 `RTL_STRUCTURE.json` 的原生区域端到端交付仍有非文件列表构建产物接口限制。审阅模型的工程证据解释仍需人工复核，详见 `harness/still_problem.md`。

`completion-report.md` 记录已有独立映射结果及证据位置；本次代码提交不表示重新执行综合、功能验收或 STA。`output/`、缓存和大型 EDA 产物沿用 `.gitignore` 排除。

---

## v19 历史说明

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
