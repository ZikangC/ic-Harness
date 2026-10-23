# RTLskill：可迁移的 RTL/EDA 工作流能力库

RTLskill 用来沉淀可跨项目复用的 RTL/EDA（电子设计自动化）流程知识、诊断方法和确定性检查器。它不是某个芯片项目的任务卡，也不保存某次迭代的完整历史。

这个仓库会持续增加新的 Skill。根 README 只承担四件事：

- 说明仓库的总体设计原则；
- 提供当前 Skill 的动态目录；
- 记录各 Skill 在具体 harness（任务控制框架）中的接入状态；
- 约定新增 Skill 时应如何划分职责和组织文件。

每个 Skill 的详细中文介绍放在自己的 `readme_cn.md` 中。面向模型执行的正式入口仍是 `SKILL.md`，确定性逻辑放在脚本中，项目路径和命令则留在具体 harness 的 adapter（适配层）与 controller（控制器）中。

## 仓库设计

一个可迁移能力通常分成四层：

| 层次 | 负责什么 | 不应该负责什么 |
|---|---|---|
| `SKILL.md` | 适用场景、核心判断方法、边界和关键不变量 | 写死某个项目的模块名、服务器路径和命令 |
| 确定性脚本 | 从真实产物提取事实，输出结构化 JSON，执行可重复分类 | 根据自然语言自行猜测结果 |
| 项目 adapter | 把项目日志、diff（差异）、配置和分区转换为通用 schema（数据结构） | 复制一份通用政策并产生分叉版本 |
| controller | 校验动作身份、参数与契约后分发 executor（执行器），维护进程边界、停止信号和退出状态 | 重复实现各 Skill 的阶段语义，独立保存同一事实，或让模型文本覆盖确定性条件 |

大模型适合整理证据、提出假设和处理需要判断力的选择，但不能替代确定性证据和硬门禁。工具 exit 0、文件存在或模型认为“应该成功”，都不是功能、综合、链接或时序通过的充分证明。

## 为什么需要 Skill

这类能力不能只依赖一个长期大模型会话：

- 累计日志和历史决策会污染当前阶段判断，使模型引用旧 run 或已失效的状态；
- 同一种退出码可能被不同轮次解释成 RTL 失败、基础设施失败或测量未完成；
- Yosys 成功不等于后端能够解析网表，局部 ABC 结果也不等于顶层 STA/Fmax；
- 提示词只能表达期望，不能强制“不跑 STA”“必须回退”或“只允许一个模块”；
- 项目规则若散落在 prompt、shell 和日志格式里，下一个 harness 仍要重新解决同类问题。

因此，Skill 应把可迁移的判断方法固定下来，把适合程序判断的部分实现为结构化脚本，再由项目 controller 负责真正执行。

## Skill 目录与接入状态

接入状态只描述当前 `/Users/chenzikang/code/work/doule_RTLharness`，不写入各 Skill 的独立介绍。以后接入其他项目时，可以在本表增加项目列或改用独立接入矩阵，而不修改 Skill 的通用说明。

| Skill | 一句话能力 | 中文介绍 | `doule_RTLharness` 接入状态 |
|---|---|---|---|
| `rtl-implementation-readiness` | 初版 RTL 后独立识别综合成本风险，运行有预算的小实验，比较变体并交付绑定源码的证据和补丁 | [详细说明](rtl-implementation-readiness/readme_cn.md) | v2 已改为独立 CLI，不要求其他 Skill 或共享 runtime；旧 v1 shadow 接线和 q4 v6 `EVIDENCE_INCOMPLETE` 属于历史结果，未自动升级既有 harness，旧 API 需固定归档版本或显式迁移 |
| `stage-evidence-workflow` | 用单次不可变 packet、有界 Fact Catalog 和 validator 统一整理当前阶段证据，并独立校验来源 Skill 的执行契约 | [详细说明](stage-evidence-workflow/readme_cn.md) | 已接入；项目 adapter 只列出证据引用并提供参数化模型命令，Skill 内部完成模型整理、校验、一次复核和确定性降级；controller 只读取最终 gate，随后 adapter 将 `strategic_view` 与确定性 `allowed_actions` 合成唯一 DecisionPacket |
| `netlist-handoff-compatibility` | 验证综合网表能否被目标后端真实读取和链接 | [详细说明](netlist-handoff-compatibility/readme_cn.md) | 尚未接入 |
| `rtl-closure-governance` | 对顶层综合与 STA（静态时序分析）闭合阶段分类，并确定性比较耗尽测量与拟测量的身份关系 | [详细说明](rtl-closure-governance/readme_cn.md) | 已接入；是 Closure 分类的唯一真源，项目仅经 bridge 调用语义门，不保留本地分类器；不授权 RTL 优化或项目动作 |
| `rtl-sta-scope` | 用真实 RTL diff 与当前设计上下文选择最小可辩护 STA 范围 | [详细说明](rtl-sta-scope/readme_cn.md) | 已接入；真实 diff 后的范围硬门，设计上下文缺失或陈旧时保守提升到顶层 |
| `rtl-optimization-triage` | 用硬阈值自动选择已验证模块，并在 Skill 内治理单文件模型编辑、功能/STA 验证、有限重试、回退和主线交还 | [详细说明](rtl-optimization-triage/readme_cn.md) | 已接入；campaign runner 承担内部循环；局部 STA 留在 campaign，顶层 STA 输出可合并的验证义务；项目 adapter 只翻译命令、匹配主线和归一化结果，内部步骤不重复调用 Evidence Model 或消耗 Pro 指导轮 |
| `rtl-module-evidence-registry` | 维护模块当前表示、设计上下文、历史验证、PPA 证据和当前集成主线 | [详细说明](rtl-module-evidence-registry/readme_cn.md) | 已接入；是模块事实与当前黑盒展开主线的唯一 owner；每指导周期前后重建热视图并硬门展开动作 |
| `rtl-transaction-ledger` | 可选地维护通用父子事务身份和生命周期，不承担调度政策 | [详细说明](rtl-transaction-ledger/readme_cn.md) | 能力保留但 `doule_RTLharness` 未接入；当前项目只有一条黑盒主线和串行优化子流程，直接由 Registry + Optimization 管理更轻量 |

“已接入”表示项目 controller 已真实调用其稳定接口，不表示所有可选输入和高级模式都在每一轮启用。“尚未接入”表示 Skill 和测试已经存在，但当前项目流程还没有以它的 JSON 结果作为硬门。

## 文件组织约定

每个 Skill 目录按实际需要选择以下文件：

```text
skill-name/
├── SKILL.md                 模型使用的正式入口
├── readme_cn.md             面向维护者的中文设计说明
├── agents/openai.yaml       可选的界面元数据
├── scripts/                 可重复执行的确定性实现与测试
├── references/              schema、接口和条件化细节
└── *_gate.py                可选的稳定 controller 入口
```

多个 Skill 可以依赖 `runtime/` 下的普通版本化运行库。运行库只能提供无领域语义的原子能力，不得成为另一个状态 owner，也不得读取具体 Skill 的私有状态。

`readme_cn.md` 应优先回答：

1. 这个 Skill 的目的是什么；
2. 为什么仅靠通用大模型做不好，因而有必要固化；
3. 它针对真实项目中出现过的哪些典型问题；
4. 它如何工作，输入、输出、状态和失败语义是什么；
5. Skill、确定性脚本、项目 adapter 和 controller 如何分工；
6. 哪些内容可迁移，接入新项目时需要配置什么；
7. 它不证明什么，以及当前有哪些限制。

详细说明不应复制完整 `SKILL.md`、长日志或任务历史。历史问题只保留能解释设计选择的关键事实，例如“OpenSTA 无法解析 Yosys `write_verilog` 产物”“超时空网表被误判为结构失败”。

## 新增 Skill 的维护流程

新增能力时：

1. 先确认问题具有跨项目复用价值，而不是当前项目的一次性补丁；
2. 明确 Skill 与现有能力的边界，避免把多个独立问题做成一个越来越大的总 Skill；
3. 将可重复判断实现为脚本或 gate，而不是只增加 prompt 规则；
4. 为输入、输出、错误分类和停止条件定义稳定 schema；
5. 添加行为测试，验证状态转换和关键不变量，而不是只检查文案；
6. 编写该目录的 `readme_cn.md`，并在本 README 的目录表增加一行；
7. 项目真正调用 gate 并形成 controller 分支后，再更新根 README 的接入状态。

## 通用边界

- Skill 不自动获得修改 RTL、启动 EDA、访问服务器或删除 run 的权限。
- 原始日志和网表是证据；模型摘要不能取代原始产物。
- SSH、容器、超时和工具不可用必须与 RTL/功能失败分开。
- 局部、分区或带黑盒结果必须保留原 measurement scope（测量范围），不能升级为完整芯片 PPA、STA 或 Fmax 结论。
- 项目特有的模块名、回归长度、工具路径、Docker、服务器和预算策略应留在 adapter/controller 中。
- 同类漏洞应优先抽象成状态、schema 或硬门，不能无限积累一次性提示词。

## 能力增长方向

本仓库不会预设固定数量或固定协作拓扑。未来可继续增加新的独立 Skill，例如统一 STA 健康检查、约束质量审计、形式等价检查、功耗证据治理或物理实现阶段诊断。

当前值得继续抽象的一项能力，是统一输出 `health.json` 的 STA 健康检查：从 RTL/filelist、Yosys 读取与 lowering（降级转换）、网表结构、后端 read/link，一直到时钟、寄存器和 timing path（时序路径）统计。现有 Skill 已覆盖其中若干边界，但不应在统一检查器和 controller 接线完成前宣称已经具备完整能力。

## 微架构能力（新增）

| Skill | 能力 | harness_final 接入 |
|---|---|---|
| microarchitecture-design | 从当前 spec/reference 设计候选、复算预算并形成 RTL 实施契约 | controller 显式加载固定副本；五步设计、独立 scripts/evaluate.py 与受限计算回传已接入；只生成一份 architecture.md，包含比较、预算依据与 RTL 契约；旧多文档格式已移除。是否交付看当前运行证据，工具通过不证明设计正确 |
| microarchitecture-audit | 独立核对需求、资源与周期依据、ABI 及数值契约 | 七步审阅、独立上下文、只读查询和共享复算已接入；审阅同一份 architecture.md 与结构化证据，原文/回执/报告矛盾校验参与放行；合成诊断曾发现状态缺陷。当前状态看运行证据，不是工程正确性或可迁移性充分证明 |

源版本在本库维护；harness 通过显式 vendor_skills 工具更新固定副本与内容指纹，运行不依赖本库绝对路径。微架构阶段不取代 RTL Readiness 或 PPA，估算不能证明完整实现闭合。

## RTL 生成、审阅与综合前实验

| Skill | 入口 | 当前同步状态 |
|---|---|---|
| rtl-implementation | [SKILL.md](rtl-implementation/SKILL.md) | V7 当前初稿，按用户授权从 V6 的补强版本发布；RTL 生成、自检与返稿修复方法，使用配套 harness 接口 |
| rtl-audit | [SKILL.md](rtl-audit/SKILL.md) | V7 当前初稿，与实现 skill 和配套 runtime 一同同步；独立上下文审阅、证据核对与修订请求 |
| rtl-implementation-readiness | [SKILL.md](rtl-implementation-readiness/SKILL.md) | v2 初步定稿，可独立运行风险分析、小实验、变体比较和补丁证据导出 |

这三项与架构两项均维护于本库，并显式同步到 `harness_final/harness/skills/` 及当前指纹清单。V7 按用户授权作为当前初稿同步，原工程试验与独立审阅结果原样保留；源码同步不改写项目验收结论。项目配置、readiness 和架构能力保持现有版本，历史 run 继续使用各自冻结快照。readiness 尚未新增 `harness/run.py` 综合阶段。
