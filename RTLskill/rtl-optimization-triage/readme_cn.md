# rtl-optimization-triage 中文说明

## 目的

这个 Skill 负责串行的 Stage-1 RTL 优化：从已经有真实模块级证据的候选中，用确定性硬门和分数阈值自动选择最高价值模块；调用一次可配置执行模型只修改一个 RTL 文件；然后维护功能验证、STA 范围、有限修复/重测、回退和原集成主线恢复。它的目标是先把明显较差的实现优化到“不错”，不负责后期跨模块联合优化。

## 为什么有必要

长期 Pro 会同时承担主线、证据阅读、候选选择和代码修改，容易被历史日志与失败修复拉走。此前真实出现过：没有模块证据却判定“无优化价值”；功能失败后仍启动 STA；`measurement_pending` 时重复编辑同一 RTL；重复运行相同的 7200 秒测量；优化失败后忘记原本要展开的黑盒模块；supervisor 又用硬编码规则覆盖 Pro 的选择。

仅增加 prompt 不能保证单文件边界、次数预算、崩溃恢复和真实回退。因此候选选择与生命周期由确定性状态机负责，具体 RTL 修改交给一个无历史的单次模型，Pro 不参与普通 Stage-1 候选授权。

## 工作方式

### 1. 候选资格和评分

项目 adapter 提供 Registry 中的当前候选。候选必须至少满足：

- 有一次真实展开验证；
- 有可归因的模块面积与延迟证据；
- 不是非物理路径伪影或已知设计指纹冲突；
- 允许本地优化，且不处于确定性失败或 cooldown（冷却）状态。

确定性分数综合 timing、area、replication（实例复制程度）、证据置信度、修改风险、验证成本和历史失败。最高分达到 `minimum_value_score` 时输出 `automation_intent=start_optimization`；未达到阈值就继续集成主线，不调用 Pro 来“猜一个”。阈值由项目配置。

### 2. Skill 内部分层

```text
Registry/PPA 证据
→ optimization_governance.py：资格、评分和状态机
→ optimization_runner.py：checkpoint + 单次模型编辑 + 单文件 diff 校验
→ modified_pending_functional
→ 项目功能门
→ rtl-sta-scope
→ 项目 STA/PPA 执行
→ 接受、有限重测或 verified rollback
→ 恢复 Registry 记录的原主线
```

`optimization_runner.py` 接收项目根目录、唯一 RTL 路径、状态文件、checkpoint 目录和参数化 `model_command`。它在模型调用前写入 `edit_prepared` journal（操作日志）和完整 pre-image（修改前快照），调用失败或越界修改时恢复原状。合法编辑必须恰好只改变允许的一个 RTL 文件；成功后记录 diff、SHA-256 指纹，并停在 `modified_pending_functional`。编辑 runner 不运行功能、综合或 STA。

`optimization_campaign_runner.py` 是项目面对 Optimization 生命周期的唯一入口，也是持久化治理状态的唯一读写者。它在一次 campaign 调用中连续推进编辑、功能/局部 STA 验证、有限重测和回退，直到遇到共享/全局 STA 战略交接，或用户停止、资源上限、并发保护、基础设施不确定等进程边界。项目只提供状态文件路径、独立外部 observation 信封路径，以及路径绑定、命令执行和结果归一化 callback（回调）；packet 不携带 state，项目也不提供持久化回调。Skill 不携带 Task 02 路径，supervisor 也不再逐 phase 手写同一套循环。

治理文件只保存裸 governance 对象，不接受 controller 包装层或 session state 镜像。共享/全局 STA 的结果需要跨调用时，controller 只能写一份独立、不可覆盖的规范化 observation 信封；Skill 在下一次调用中先推进并持久化状态，再删除信封。这个临时信封不属于治理 schema。

`optimization_governance.py` 的 `step` 是内部事务的唯一推进接口。它根据当前 phase（阶段）和一个标准 observation（执行观察）输出最多一个 `requested_execution`：单文件编辑、功能/预检、通用 STA Scope capability（能力接口）、项目测量或 Skill 回退。功能/预检与测量是两个明确 operation，项目 controller 不得再把它们合并后自行重选 phase。项目 adapter 只负责构造 diff/设计上下文、映射分区/路径/命令，并把原始项目结果归一化为 observation；Skill 间通过 capability 接口间接连接，缺少 STA Scope capability 时由 Optimization Skill 保守地产生顶层 STA 义务，不把它解释成 RTL 失败。测量是否完整、是否可比、是否改善、共享 STA guard 是否通过以及重试/回退均由 Skill 判断。

### 3. 生命周期

```text
idle
  → begin_candidate → editing
  → edit_completed → modified_pending_functional
  → functional_failed → functional_repair → 再次单文件编辑
  → 功能/编辑次数耗尽 → rollback_required
  → functional_passed → sta_required
  → measurement_incomplete → measurement_pending → 最多有限次无编辑重测
  → 同口径 PPA 改进判定 → sta_passed → resume_pending
  → sta_failed / 无收益 → rollback_required
  → rollback_complete → resume_pending
  → Registry 确认原主线完成 → idle
```

功能不通过时禁止 STA。功能与 Yosys 预检证据只有在“明确成功结果 + 完全相同 RTL 指纹 + 完全相同受管输入上下文指纹”同时成立时才能复用；单独的 RTL 指纹绝不代表检查已经执行，也不能覆盖 filelist 或 harness 改动。测量 timeout（超时）、日志截断或必需测量产物缺失属于 `measurement_incomplete`，不等于 RTL 失败，也不能触发再次编辑；默认初次测量后最多两次无编辑重测。若工具已经完成测量但 `comparability_key` 与基线不同，则属于确定性不可比较，直接回退并暂缓，不再浪费重测。模型服务、SSH、容器等基础设施错误不消耗 RTL 修复次数。

STA 工具成功只说明改动可测量，不等于优化成功。runner 在编辑前保存候选的基线测量；验证结束后，确定性 `evaluate-measurement` 门只比较同一 `comparability_key` 的面积与延迟。默认至少一项改善 1%，且另一项不得恶化超过 1%；阈值可由项目参数覆盖。不可比或低于收益门槛时进入 `optimization_not_improved`，回退后暂缓该模块。

每个 campaign 还有独立的 `max_internal_steps`，覆盖编辑、功能/STA 验证、重测和回退。内部步骤不消耗 Pro 的 `--max-cycles` 指导轮数，也不在每一步重复调用 Evidence Model；只有回到战略边界时才重新整理证据并询问 Pro。另有 controller 调度步硬上限，防止异常状态造成无限循环。

### 4. 主线与优化目标

Module Registry 是当前黑盒展开主线的 owner（所有者）。Optimization 只在候选开始时捕获不可变 `interrupted_mainline_ref`，包含事务 ID、模块、分区、Registry generation（代次）和身份指纹；它不更新主线，也不维护第二份可漂移目标。

原集成目标记为 E，优化目标记为 O。O=E 是 `fused_same_unit`；O≠E 是 `decoupled`。完成一个优化 campaign（活动）后先恢复或推进 E 一次，再刷新证据，之后才考虑下一个候选。这是当前的方案 A，避免连续优化把集成主线饿死。

### 5. STA 范围和验收债务

本 Skill 不判断最小 STA 范围。功能通过后，真实 diff 和当前设计上下文交给独立 `rtl-sta-scope`。局部或受影响锥范围由 campaign 继续验证；若要求 `top_level_partial` 或 `top_level_complete`，本 Skill 不重复启动顶层测量，而是进入 `waiting_external_verification` 并输出带 campaign、模块、RTL 指纹和范围的标准 `verification_obligation`（验证义务）。项目侧薄匹配器决定它能否与当前黑盒展开主线共用一次测量；Evidence Model 只能整理这个结论，不能自行判定接受或回退。局部 STA 仍不能冒充顶层集成验收。

共用测量不要求把收益严格归因到某一个模块，但也不能只凭工具退出码接受。当前项目的确定性 combined guard（联合验收门）至少要求：候选 RTL 指纹未漂移、顶层 link 和 timing path 均真实存在、测量范围覆盖义务要求，并且可信路径满足项目时钟目标。部分顶层通过只形成临时接受并留下最终无黑盒完整顶层 STA 债务；失败或证据可疑时回退可选优化，保留原集成主线。

## 输入与输出

主要输入包括：候选模块、历史展开证明、当前表示、面积/延迟、路径类别、证据归因、风险/成本、Registry 主线引用，以及项目 adapter 提供的唯一 RTL 路径和模型命令。

主要输出包括：

- `eligible_candidates` / `ineligible_candidates` 与原因码；
- `value_score` 和 `automation_intent`；
- Skill 独占持久化的 Optimization 状态与 `execution_contract`；
- `step` 输出的唯一 `requested_execution` 或 `resume_mainline` handoff；
- runner 的 operation journal、pre-image、diff 和 RTL 指纹；
- `accepted`、`rejected`、`deferred` 与 `acceptance_debt`。

## 职责边界

- Module Registry：模块事实、测量证据和当前集成主线的唯一 owner。
- Optimization Skill：候选硬门、评分、单次模型编辑 runner、生命周期和回退语义。
- Evidence Skill：把当前阶段证据整理给 Pro；不替 Optimization 选择普通 Stage-1 候选。
- `rtl-sta-scope`：根据真实 diff 判断最小可辩护 STA 范围。
- 项目 adapter：提供模块到 RTL 文件的映射、模型 argv、功能/STA 命令、项目路径和结果归一化；不读取或保存 governance。
- controller：向 Skill runner 提供无领域语义的子进程执行端口，检查并发、预算与安全停止，并记录退出码；不读取治理状态、不保存阶段投影、不选择内部下一步，也不把本地验证伪装成一次 Pro 决策。
- Pro：处理集成、闭合和其他战略选择；不参与正常的 Stage-1 候选选择或单文件编辑。

## 迁移到新项目

新项目只需配置：Registry 到候选 schema 的 adapter、唯一 RTL 文件解析、功能门、STA 执行、checkpoint 根目录和模型命令。默认是串行工作区；未来如需并行，可替换 workspace adapter，而不改变候选评分和状态机。

## 不证明什么

高分只表示更值得做一次受控实验，不是预计收益。功能与 STA 通过只证明修改可验证；是否真的优化还需要和同口径 baseline（基线）比较。局部或带黑盒结果必须保留真实 measurement scope（测量范围），不能写成完整芯片 PPA、STA 或 Fmax。
