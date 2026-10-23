# stage-evidence-workflow 中文说明

## 目的

这个 Skill 为单个 pipeline stage（流水线阶段）创建一次全新的证据整理工作流。它把当前 run 的真实产物、当前阶段上下文和相关 Skill 的结构化输出整理为紧凑、可验证的统一证据，再交给独立 Evidence Model（证据模型）做一次性归纳。

它只回答“这一阶段实际发生了什么、证据是否一致”，不负责决定下一步优化哪个 RTL，也不替代项目 controller 的硬门禁。

## 为什么有必要

长期 Pro 同时读取累计 progress、旧决策、任务规则和当前日志时，即使总 token 数没有超限，也容易出现语义污染：旧 run 与新 run 混淆、过期黑盒状态被继续引用、日志尾部代替完整产物、当前证据被历史叙事覆盖。

通用大模型还可能把缺失字段补成合理结论，或者在相同证据下给出不同失败分类。把证据整理拆成一个无历史、单任务、单阶段的模型调用，可以让模型专注于读取和对齐事实；确定性 validator（校验器）则阻止模型把未知、冲突或非法来源变成通过。

## 针对过的真实问题

- Pro 使用旧 session 快照，没有先读取外部刚落盘的 result/feedback。
- 原始 feedback 很长或发生截断，关键 marker（标记）不一定出现在日志尾部。
- result、feedback 和 STA 审计中同名字段不一致，却被自然语言总结掩盖。
- Evidence Model 使用未声明的 `other` 来源，导致结论无法追溯。
- exit 124 表示测量窗口结束，却曾被直接解释成 RTL 失败或永久 blocked（阻塞）。
- 累计 progress 被重复输入，使当前阶段判断受到旧结论影响。

## 工作方式

1. 项目 adapter 明确列出一个 `stage`、一个 `run_id`、一个项目自有的 stage contract（阶段合同）、有限的 artifact role（产物角色）和参数化模型命令。Skill 不内置具体 RTL 项目的路径、模型客户端或必需字段。
2. controller 只调用一次 `scripts/evidence_workflow.py run`；Skill 内部的 `prepare` 读取完整可用产物，记录 hash（哈希），提取 key/value 和 JSON 事实，并标记缺失、冲突和截断。
3. `scripts/fact_catalog.py` 把解析后的当前 result/feedback/progress/controller 小状态和相关 `skill_*` 信封转换为有数量预算的 Fact Catalog（事实目录）。数组保留真实 JSON Pointer 下标，不建立额外 `by_module` 身份系统。
4. `prepare` 只执行一次并计算 `packet_fingerprint`；Skill 自己管理后续至多两次模型尝试，并严格复用同一 packet。
5. 独立 Evidence Model 只接收阶段目标、有限事实目录和按需诊断摘录；模型按主题选择 `fact_refs` 并写摘要，不拼路径、不复制值或 identity。
6. `validate` 校验 packet 指纹、来源完整性和 `fact_refs`，再生成完整 `validated_evidence` 与面向 Pro 的 `strategic_view`。
7. `scripts/mismatch_policy.py` 将源证据真实性与模型摘要质量分开：源证据缺失、冲突、过期或 packet 被修改仍硬阻塞；模型格式错误、未知引用、越权字段或不可用允许一次带具体错误的复核，之后使用确定性 fallback（降级摘要）。
8. 若某个 `skill_*` 信封带有 `execution_contract`（执行契约），validator 直接校验契约结构、动作集合、来源身份和 packet 完整性，并输出 `validated_execution_contracts`。契约字段不进入 Fact Catalog 和 Evidence Model prompt；Evidence Model 不复制、不改写，也不能通过摘要省略这份契约。

默认 `history_mode=none`。只有 controller 有明确趋势问题时，才可以提供少量、结构一致的 `trend_summary`；完整累计 progress、旧 Pro 回复和历史实验表不能伪装成当前阶段上下文。

## 输入、输出和状态

输入通常包括：

- 当前 run 的 `result.json`；
- 当前 run 的 `feedback.txt`；
- 显式提供的 `progress_delta`、`controller_state` 等非 Skill 当前阶段上下文；
- 可选的 `run_evidence`：只包含当前 run 的路径摘要、数量、selector（选择器）和来源，不包含逐单元原始报告；
- 与当前阶段相关的 `skill_*` 结构化信封，例如模块登记簿、闭合治理、优化价值评估或 STA 范围；
- 可选的紧凑 `trend_summary`。

同一语义证据不能同时以 Skill 信封和普通上下文重复输入。例如已有
`skill_closure_governance` 时，不再传第二份 `sta_closure_audit`。
原始 timing path（时序路径）文本继续保存在不可变 run 中；Evidence Model 整理路径摘要后，Pro 只读取 validator 验证过的统一证据，避免相同信息以原始日志和 Skill 输出重复进入上下文。

输出包括不可变 prepare packet（准备证据包）、Evidence Model JSON 和校验后的 `stage_evidence.json`。后者包含完整 `validated_evidence`、有界 `strategic_view`，以及来自确定性 Skill 的 `validated_execution_contracts`。项目 adapter 将 `strategic_view` 与契约的紧凑 `allowed_actions` 投影合并为唯一 DecisionPacket；Pro 和 controller 使用同一 packet，controller 只做身份、动作成员和参数校验，不得把 Pro 动作改写为另一个动作。

状态分成两组：

- `integrity_state=pass|hold|infra_uncertain`：描述 result、feedback、Skill 信封和确定性事实是否可靠；
- `summary_state=pass|retry_required|degraded|unavailable`：只描述 Evidence Model 摘要质量。

兼容状态：

- `NO_INPUT`：没有可用产物；
- `READY_FOR_MODEL`：确定性事实已准备好；
- `RECHECK_REQUIRED`：首次读取有截断或冲突，需要有限复核；
- `MODEL_REVIEW_REQUIRED`：模型输出无效或与事实不一致；
- `CONSISTENT`：模型观察有合法来源且与事实一致；
- `DEGRADED_SUMMARY`：证据完整，但模型摘要复核仍未通过，已使用确定性 fallback；
- `BLOCKED_EVIDENCE`：有限复核后源证据仍缺失或冲突；
- `INFRA_UNCERTAIN`：源证据文件或传输不可可靠完成。

Evidence Model 自身不可用不再等同于源证据基础设施失败；只要 `integrity_state=pass`，controller 可以使用受限的确定性事实包继续交给 Pro。源证据文件不可读仍是 `infra_uncertain`。测量超时可以被保留为 `incomplete_timeout`，但不能冒充完整 PPA 或完整 STA。

## 职责边界

- Skill 定义单阶段证据工作法、来源纪律和一致性状态。
- 脚本负责读取、hash、Fact Catalog、事实选择解析、异常分类和模型输出校验。
- 项目 adapter 决定哪些真实产物对应哪些 role，提供参数化模型 argv，并负责把各项目 Skill 输出转换为标准信封。
- Evidence Skill 调用 adapter 指定的 Evidence Model transport（传输命令），管理一次带具体错误的复核和确定性 fallback；它不固定依赖 OMP。
- controller 只触发一次 Skill 工作流、检查最终 identity 与 `hard_gate`，再把通过校验的结果交给 Pro；它不读取 Fact Catalog、不调用 Evidence Model，也不管理 retry。项目 adapter 从 validator 输出生成不可变 DecisionPacket，controller 只对同一 packet 做通用身份、动作成员和参数校验并调度 executor。
- 总 Pro 只能消费通过校验的紧凑证据，不能覆盖确定性事实。
- 来源 Skill 的语义硬门由其结构化输出定义，controller 负责执行；Evidence Model 只负责面向 Pro 的统一摘要，不能生成动作许可。

## 迁移到新项目

新 harness 需要提供阶段 contract（契约）、artifact role、参数化模型命令和 controller 门禁。contract 保存在项目中，声明该项目当前阶段的必需字段和必需 Skill role；迁移时替换 contract 与 adapter，不修改 Evidence Skill。Evidence Model 可以默认复用执行模型，但应保留独立参数，便于隔离和评估。

这个 Skill 不固定依赖 OMP、SSH、Docker 或 EDA 工具；它只按 argv 形式调用项目 adapter 提供的模型 transport，不使用 shell。迁移时最重要的是避免把项目完整历史重新塞进 `progress_delta` 或 `stage_context`。

## 不证明什么

`CONSISTENT` 只证明模型摘要与给定证据一致；`DEGRADED_SUMMARY` 只证明源证据完整且使用了确定性降级事实。两者都不证明 RTL 正确、综合成功、STA 闭合或 PPA 达标。最终技术结论仍由对应阶段的确定性 gate 和真实工具产物决定。
