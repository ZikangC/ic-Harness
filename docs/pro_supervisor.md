# Pro / Flash 自动迭代 supervisor

`harness/pro_supervisor.py` 只用于隔离副本。它负责指导轮与执行轮的调度、预算、停止、持久化和进程端口，
不再实现 Pro 提示构造、回复解析或 DecisionPacket 校验。一次完整 Pro 指导轮由
`harness/pro_guidance_runner.py` 唯一拥有：接收不可变 DecisionPacket，构造有界 prompt，通过 supervisor
提供的通用进程端口调用模型，解析回复并校验决策。每份原始输出分别保存在
`runs/pro_supervisor--<session>/pro_decision_*.log` 与 `flash_controller_*.log`，不会被 progress 摘要替代。

Pro 的累积可读进度与 Flash 的 `progress.md` 并列，写入
`reports/02_yosys_ppa/pro_progress.md`。每个周期会追加 Pro 原始战略输出、解析后的结构化决策；若 Pro
执行受限修改，也会记录执行输出和文件范围检查；Flash 批次结束时还会写入该决策对应的实际结果。该文件只追加，
不替代 session 原始证据。

为降低 Pro 模型的输入 token 压力，supervisor 不会把完整任务卡、累积 `progress.md` 或完整工具 feedback
重复塞入每次战略调用。当前 run 的 `result.json`、feedback 和相关 Skill 信封先交给外部
`stage-evidence-workflow` 整合；Pro 只接收一个当前阶段证据包。完整证据仍保存在
`runs/`、`progress.md` 和 `pro_progress.md`，供人工复盘。
Pro 的受限实施调用也只接收限长的行动依据。输入上限降低 429 概率，但不能绕过 OMP 服务端的账号、模型或 token
配额；发生 429 时仍安全停止而不猜测结果。

## 外部轮次与 result/feedback 交接

supervisor 不把“目录里出现了一个最新文件”当成 Pro 已经读过结果。Task 02 adapter 会对最新的
`agent-02_yosys_ppa--*/result.json` 和 `02_yosys_ppa--*/feedback.txt` 建立小型 manifest（来源路径、run-id、大小和
SHA-256 哈希）；只读这两个小型交接文件，不复制或清理 `runs/` 中的大型证据。新 session 首次接手已有结果，或 session
运行期间外部 agent/controller 写入或更新摘要时，adapter 会在下一次证据准备时通过清单指纹识别变化，并通知 controller 使旧的细粒度证据缓存失效。

该交接是 controller 硬门，而不是 prompt 建议。adapter 在调用 Pro 之前把 manifest 指向的精确 result/feedback 和当前 Skill 信封物化为
Evidence Skill 的唯一 `run` 请求；Skill 完成 prepare、Evidence Model 整理和 validator 校验。同一份输入可按 `request_fingerprint` 复用已经验证的证据包，不再要求 Pro
额外执行一次“阅读结果”动作，也不把 supervisor 自行截取的字段摘要作为第二份输入。这个交接过程不启动 Flash、功能回归、
PPA、SSH 或远端写入。若 controller 自己刚完成 Flash，则同样在下一个指导周期先完成这次 Evidence 交接；随后外部再次更新仍会
按新 fingerprint 重新触发门禁。

当外部新证据对应的实测 STA run 改变时，旧的 `latest_sta_evidence`/`latest_run_evidence` 缓存会失效，避免 Pro 把旧路径
证据误当成当前 run。这里不再设置独立的外部证据检查动作；外部结果由 Evidence Skill 在交给 Pro 前统一读取、校验和压缩。外部 harness 代码改动仍需使用下面的
`--declare-pending-harness-change` 显式登记并强制 smoke 验证。

Pro 原始日志保存模型回复，但不重复回显完整调用 prompt。prompt 本身含有决策 JSON 模板；若它与模型回复混在
同一日志中，解析器可能把模板误当成模型决定。`ProGuidanceRunner` 从后向前选择第一个合法 action 的 JSON，
跳过这类模板，并保留原始模型回复供排查。supervisor 只消费其成功结果或类型化失败，不再保留第二套解析入口。

controller 的所有子进程都使用有效的空标准输入（`stdin=/dev/null`），避免 supervisor 脱离终端后，最终
48-token 回归中的 Python 环境导出脚本因 `Bad file descriptor` 提前退出。最终回归使用与普通功能门相同的
基础设施重试，并在 `runs/agent-02_yosys_ppa--<timestamp>--final/final_regression.json` 保存独立结果；失败时
状态明确区分 `failed`、`infra_failed` 和 `infra_uncertain`，不会把状态永久留在 `flash_running`。

## 当前阶段证据整合工作流

当前项目通过命令行调用外部 `RTLskill/stage-evidence-workflow`，不复制它的生命周期状态机。每次准备调用总 Pro 时，Task 02 adapter
负责发现当前 `result.json`、`feedback.txt`，并物化与当前阶段有关的标准 `skill_*` 结构化信封。当前接入的信封包括闭合治理、模块登记簿、优化候选评估，并在已有真实 RTL diff 时加入 STA 范围判断；
`progress_delta` 和 `controller_state` 已从 Evidence 输入删除，因为它们分别重复当前结果和 Skill/调度状态；controller 的预算、停止和计数不会作为第二份领域证据发送。
Adapter 不再根据 controller 计数自行判断是否附加 trend；需要趋势语义时必须由 Evidence Skill 的明确输入契约和唯一状态 owner 提供。工作流调用独立 Evidence Model（阶段证据模型）并验证其 JSON。该模型只处理一个聚焦阶段，不接收任务级规则、完整累计 progress、旧实验表或 Pro 历史，
且不能启动 EDA、修改文件或决定优化动作。工作流只执行一次 `prepare`，把解析后的当前产物和 Skill 信封转换成带 `packet_fingerprint` 的不可变 Fact Catalog（事实目录）；Evidence Model 只返回按主题组织的 `fact_refs`，
不再自行拼接嵌套路径、复制值或回显 identity。`integrity_state=hold|infra_uncertain` 才会停在证据门；如果源证据完整，但 Evidence Model 的格式、引用或服务调用在一次带错误反馈的复核后仍失败，
则使用 `summary_source=deterministic_fallback` 的有限事实包释放总 Pro，不会把摘要器变成新的单点故障。通过后，项目 adapter 把 validator 的 `strategic_view` 与确定性 Skill 给出的紧凑 `allowed_actions` 合并为唯一且不可变的 `DecisionPacket`；Pro 只接收这一份“阶段证据 + 允许动作”，不再接收 controller 重建的第二套硬门摘要。Evidence Model 只能压缩和引用 Skill 事实，不能产生动作授权。若 Skill 信封包含 `execution_contract`，validator 会独立校验其来源身份、generation、packet 完整性和动作集合，再以 `validated_execution_contracts` 输出；契约字段不进入 Fact Catalog 或 Evidence Model prompt。

Evidence Model 默认复用 Flash/执行模型；需要隔离模型时显式覆盖。总 Pro 的模型选择和战略职责不变：

```bash
python3 harness/pro_supervisor.py \
  --pro-model <pro-selector> \
  --flash-model <flash-selector> \
  --evidence-model <optional-evidence-selector> \
  --evidence-stage sta_health \
  --evidence-contract harness/evidence_contract.json \
  --evidence-max-facts-per-skill 16 \
  --evidence-max-total-facts 60
```

`--evidence-model` 省略时等价于 `--flash-model`。默认 Skill 路径是当前项目同级的
`/Users/chenzikang/code/work/RTLskill/stage-evidence-workflow`；迁移项目时使用
`--evidence-workflow <skill-directory>` 或环境变量 `RTLSKILL_STAGE_EVIDENCE_WORKFLOW` 指向新的 Skill 副本。
Task 02 的必需字段和必需 Skill role 位于项目自有的 `harness/evidence_contract.json`，不再固化在通用 Skill 中。
`--evidence-contract` 必须与 `--evidence-stage` 匹配；迁移项目时替换合同而不是修改 Skill。
每次 session 只保存小型 `stage_evidence_*`、adapter 物化的 Skill 信封、Evidence Model 原始输出和校验日志；不会扫描历史、复制大型 runs 或修改服务器证据库。
Evidence Model 必须回显当前 `packet_fingerprint`，并只能引用当前 Catalog 的 `fact_id`；identity、generation、真实值和来源路径由 validator 从 packet 解析。
supervisor 只调用一次 Skill 的 `run` 接口。Skill 内部完成一次 `prepare`、Evidence Model 调用、校验、一次带错误上下文的复核和确定性 fallback；controller 不再调用模型或 `render-retry`。第二次仍失败时只降级摘要，
而 Skill 信封缺失、当前源事实冲突、过期 identity/generation 或 prepare 后 packet 被修改仍由 validator 硬阻塞。validator 输出完整 `validated_evidence`、有界 `strategic_view` 和验证后的执行契约；项目 adapter 生成 `DecisionPacket` 后，Pro 与 `ProGuidanceRunner` 使用同一 packet。runner 只检查 transaction、generation、packet fingerprint、动作成员和参数 schema；Closure 重试预算与结构修复动作已经在 packet 生成前确定，不再由 supervisor 在 Pro 决策后进行第二次领域判断。

### 可替换项目 adapter

`harness.toml` 的 `[controller].project_adapter` 指向当前项目实现。adapter API v10 要求 `materialize_stage_evidence_execution`、`normalize_stage_evidence_execution`、`materialize_project_execution`、`normalize_project_execution` 和 `normalize_measurement_outcome`；旧的 `evidence_artifact_refs` / `evidence_model_command` 以及 Adapter 内的 `execute` / `reconcile` 入口已删除。通用加载器只校验稳定方法，不读取模块登记簿、Optimization 或 Closure 的内部 schema。当前 `task02_project_adapter:Task02ProjectAdapter` 组合已有 DecisionPacket 逻辑并读取本项目黑盒配置；实际单次配置修改由无循环、无模型、无战略判断的 `task02_project_executor.py` 完成。新的相似项目可以替换 Adapter 与该执行器，而无需修改 supervisor 调度或 `ProGuidanceRunner` 的指导轮协议。

Task 02 的执行结果优先级也由 Adapter 统一归一化：基础设施不确定、功能/预检确定失败、Closure Skill 的测量不完整、完成。`pro_supervisor.py` 和 `ppa_agent_loop.py` 不再各自根据 `ppa_exit=124/137` 重建 timeout 分类；退出码只是原始项目事实，Closure phase 和语义门仍由 `rtl-closure-governance` 唯一产生。Adapter 不据此决定重试、停止或下一动作。

DecisionPacket 中的 `executor_ref` 被限制为少数稳定能力，例如 `flash.run`、`evidence.inspect`、`project.expand_blackbox`、`project.scoped_update` 和 `controller.stop`；Skill 来源名称与执行模式不再拼成动态执行器。Pro 不能提交 `executor_ref`、`execution_mode` 或 `operation_id`，这些字段只由已验证 packet 确定。项目 executor 会持久化本地配置事务的标准执行结果，相同 `operation_id` 再次出现时先对账而不重复修改；Adapter 只物化命令并归一化执行结果。

Optimization 的候选评分、checkpoint、单文件编辑、阶段迁移、局部验证循环、回退语义和治理状态持久化都由 Skill runner 独占；治理文件是裸 Skill schema，supervisor 的 `session_state.json` 不再保存 phase、候选、triage、义务匹配或 pending observation 的副本。Task 02 adapter 只把 Skill 请求翻译成本项目路径/命令，并把项目结果归一化为 observation；Evidence 只接收 Skill runner 生成的有界 projection（投影），不读取治理文件。supervisor 提供政策无关的子进程执行端口，负责并发、预算、安全停止、退出码和原始结果收集，不解释 Optimization phase、候选或测量语义。本地验证不再进入 Pro 决策路径；只有共享/全局 STA 义务进入 Evidence/Pro 战略边界。当前项目不接入 Transaction Ledger，Registry 持有唯一黑盒展开主线。

共享/全局 STA 完成后，supervisor 暂时只把 Adapter 已归一化的结果原样写入独立的 `status/task02_optimization_pending_observation.json` 信封；未消费信封禁止覆盖。下一次 campaign 由 Skill 读取该信封，先原子持久化治理状态，再删除信封。supervisor 不读取 `status/task02_optimization_state.json`，也不通过普通 session state 判断是否存在挂起候选。该独立信封是后续全局 Registry 状态维护接入前的临时跨调用通道，不是第二套 Optimization 状态。
Evidence 的 `request_fingerprint` 由 contract、精确 artifact 内容哈希、Skill 信封、模型选择和输入上限共同生成，只用于抑制完全相同的重复调用；它不是证据身份。不可变证据身份仍只有 Skill 生成并校验的 `packet_fingerprint`。登记簿 generation 或任一相关输入变化时都会产生新请求，不会沿用上一轮登记簿摘要。
没有已完成证据的首次启动属于 bootstrap（引导）例外，允许总 Pro 规划首个执行批次；一旦存在当前 run 证据，后续批次必须通过上述硬门。
正常路径下 Pro 不再接收 supervisor 自行维护的 result 字段白名单或 feedback 前缀摘录；这两类当前 run 内容只由 Evidence Skill 读取并进入 Fact Catalog。
Evidence Model 不可用时使用 validator 的确定性 fallback，不再退回旧的 Pro 专用 result/feedback 摘要。项目中仍保留的 `latest_result_data()`
只用于退出码、功能门、预算、停滞计数等 controller 执行控制，不是第二套证据展示路径。

### 测量不完整语义由 Closure Skill 唯一产生

`exit 124/137` 只是项目执行事实。supervisor、Flash controller、Registry Adapter 和 Optimization Skill
都不得据此重建 `measurement_incomplete`。Closure Skill 从同轮确定性 feedback 和可选原始预算事实产生
Closure phase、`measurement_incomplete`、重试许可与资源阻塞；Task-02 Adapter 只组合该 audit 与当前功能、预检和进程结果。
只运行 preflight 记为 `measurement_not_run`，要求的 timing 证据缺失记为 `verification_evidence_missing`，两者都不是 Closure timeout。

优化候选收到规范化的测量不完整 observation 后由 Optimization Skill 状态机处理。它会保留候选及被打断主线的 Registry 引用，
生成禁止再次编辑的执行契约，并由 Optimization Skill runner 在同一 campaign 调用内推进有限次无编辑重测；这些内部阶段不调用 Evidence Model 或 Pro，也没有额外 Optimization coordinator。
`--max-cycles` 只统计 Pro 战略指导调用；内部优化步骤不占用该计数。每个 campaign 由
`--max-optimization-steps` 单独限制，supervisor 另设由两者乘积导出的调度步硬上限，避免异常循环。
重复无信息增益、资源预算耗尽、用户 stop，以及功能/预检明确失败仍可正常停止。`exit 75/76` 继续按基础设施失败/不确定处理。

任何单独退出码都不等于测得了完整芯片 PPA、无黑盒 STA 或 Fmax；只有对应阶段真实日志支持时才能报告这些结论。

## 外部 `rtl-closure-governance` 接入

supervisor 通过外部 Skill 的稳定入口 `closure_gate.py` 获取 STA 闭合阶段和硬门结果；Skill 目录不是当前项目代码的一部分，
因此迁移到另一个 harness 时只需替换路径或填写该 harness 的证据路径：

```bash
python3 harness/pro_supervisor.py \
  --pro-model <pro-selector> \
  --flash-model <flash-selector> \
  --closure-governance-skill /path/to/RTLskill/rtl-closure-governance \
  --closure-feedback /path/to/current/feedback.txt \
  --closure-artifact /path/to/current/closure_artifact.json \
  --closure-probe /path/to/current/tool_probe.json \
  --closure-netlist /path/to/current/closure.v \
  --closure-top <top-name>
```

`--closure-governance-skill` 省略时读取环境变量 `RTLSKILL_CLOSURE_GOVERNANCE`，再默认寻找当前项目同级的
`RTLskill/rtl-closure-governance`。`--closure-feedback` 省略时自动使用当前项目最新的 Task-02 `feedback.txt`；
`--closure-artifact`、`--closure-probe`、`--closure-netlist` 和 `--closure-top` 是可选的通用输入，required body
可用 `--closure-required-body <module>` 重复填写。所有调用都使用参数数组和只读输入，不经过 shell，不启动 EDA，也不把网表送入模型。

Skill 的 `phase`、`structural_repair_required`、`diagnostic_required`、`closure_gate_required`、
`measurement_rerun_admissible`、`measurement_resource_blocked`、`strategy_change_required`、`timing_ready` 和
`repair_domain` 是 controller 的唯一闭合语义依据。项目只由 `rtl_skill_bridge.py` 做路径、API 版本和必需字段校验；
不再保留本地 Closure 分类器，不合并旧 `result.json` 中的 audit，也不在 controller 中复制 phase 集合。
Skill 缺失、输出无效或接口不兼容统一视为基础设施不确定并 fail closed（失败时关闭执行许可）。每个 session 会保存 `closure_gate_initial.json`、`closure_gate_before_<cycle>.json` 和
`closure_gate_after_<cycle>.json`，方便迁移和复盘。`closure_export_empty`、`closure_top_missing`、`closure_body_missing`、
`sta_parse_error`、`sta_link_error` 等阶段会进入对应闭合硬门；Skill 不会把它们包装成 RTL 性能结论。
如果空导出同时有同轮 timeout/truncation 证据，则阶段是 `closure_measurement_incomplete`：不要求
`repair_harness`。同一测量只有在预算严格提高且不超过合法上限时，才允许执行一次 `top_level_sta` smoke，且仍禁止完整 PPA 结论。
达到 7200 秒最大合法预算后，Closure Skill 返回 `closure_resource_blocked`：项目 adapter 不再把相同测量放入 DecisionPacket，
只保留改变测量策略、只读诊断或停止调试等合法动作。这不是 RTL 失败结论，Closure Skill 也不授权 `optimize_rtl`；任何 RTL
优化许可由 Optimization Skill 在闭合治理之外组合 adapter 提供的登记簿、干净功能基线、待处理事务和候选证据，并输出插入门与执行契约；supervisor 只保留 fail-closed（失败关闭）的实时安全交叉检查。
因此资源边界仍拒绝 `continue/increase_budget` 的同类重复，但可以放行一个明确候选的受控优化实验；
Closure phase 不会因此被提升，测量不完整也不会被改写成 RTL 失败。

启动时显式传入 OMP 中已配置的两个模型选择器：

```bash
python3 harness/pro_supervisor.py --pro-model <pro-selector> --flash-model <flash-selector>
```

Pro 必须输出结构化决策：`continue` 时选择 1--6 个 Flash 回合，并给出仅作优先级提示的
`flash_directive`；Task 02 controller 仍自行执行所有回归、预算和文件范围限制。Pro 可在边界
选择修订任务卡，或修复 Task 02 harness（controller、supervisor、PPA/功能远端脚本、环境配置和
测试）；动作失败或越界写入都会恢复该动作前快照。RTL、reference、testbench 和服务器 runs 始终
不属于 Pro 的直接修复范围。

## 部分闭合后的单模块黑盒展开

`PARTIAL_HIERARCHICAL` 不等于必须继续修通用测量 harness。只要最近一次真实 STA 审计满足
`sta_link_result=ok`、`sta_timing_measured=yes`，且功能回归通过，Pro 可以输出：

```json
{
  "action": "expand_blackbox",
  "module": "p16_tensor_loader",
  "flash_attempts": 0,
  "reusable_knowledge": "none"
}
```

controller 会校验该模块确实在 `top_level_sta` 的当前黑盒列表中，然后只移除这一个模块。下一批被强制
进入模块展开验证：运行 8/16/48-token 功能门、Yosys 预检和 `top_level_sta` targeted PPA；Flash 在这个
验证批次中不能再修改 RTL、任务卡、黑盒声明或其他分区。验证结束后仍保留
`measurement_scope=PARTIAL_HIERARCHICAL`，因为其他计算模块仍是黑盒。该动作不要求等待
`TOP_LEVEL_COMPLETE`，但也不把单模块结果冒充整片 PPA 或完整 Fmax。

模块展开验证使用 `--forced-blackbox-expansion-module`，跳过 Flash 的二次编辑，避免“移除黑盒”和另一个
RTL/harness 假设混在同一实验中。若验证失败，结果和已展开配置保留在本地 runs 供调试；服务器 runs 仍然
不可变。

`continue` 的语义只有一个：启动下一批 Flash，因此可能运行功能回归、预检和 PPA。若 Pro 只需要复盘
已经完成的 STA 路径，必须输出 `inspect_existing_sta_evidence`（`flash_attempts=0`）。supervisor 将只读
服务器不可变 `runs/<run-id>/sta.log` 的 `STA_TIMING_BEGIN..END` 区段，写入当前 session 的
`sta_evidence_<cycle>.json` 与 `pro_progress.md`；不会启动 Flash/EDA，也不会修改服务器 run。若 directive
明确写了“只读已有 STA 且禁止新 PPA”，但 action 误为 `continue`，controller 会安全地改为此只读动作。
相同 source run 已读过一次则以 `no_new_evidence` 停止，避免把同一日志重复送给 Pro。

若上述摘要不足以判断一条路径，Pro 可选择 `inspect_run_evidence`，并输出严格的 `evidence_request`：

```json
{
  "run_id": "latest_measured_sta",
  "artifact": "sta.log",
  "selector": "worst_slack",
  "max_lines": 180
}
```

这不是 shell 权限。`latest_measured_sta` 是推荐别名，由 supervisor 确定性解析为最新已 link 且实际测到
路径的 PPA run；也可使用 prompt 明示的精确 Task-02 PPA run-id。`agent-...` 或 `...--attemptN` 是 controller
结果标识，不能作为远端 `sta.log` 路径。supervisor 只接受上述来源、固定 `sta.log`、`worst_slack`、`max_arrival`
或 `all` 三种选择器及 1--240 行上限；它只读远端 `STA_TIMING_BEGIN..END`，把带逐级 cell/instance 明细的
原始路径块只保存在 session 的 `run_evidence_<cycle>.json` 作为取证记录；项目 adapter 另生成不含逐单元原文的
`run_evidence` 路径摘要，交给 Evidence Model 纳入统一 `validated_evidence`。下一次 Pro 只读取经过 validator 解析的
摘要，不再同时收到原始路径文本。相同请求不会重复，因此模型可以做路径归因，又不会把同一证据复制两次或借此读写其他服务器文件。

### 黑盒候选错误不会直接终止 session

controller 每次进入单模块展开阶段都会把当前 `top_level_sta` 黑盒候选列表直接放入 Pro 输入。若 Pro 仍从
历史 progress 中选出不在列表中的模块，解析器会记录该模块并在下一周期重新请求决策，同时把它加入排除列表；
这次不会启动 Flash，也不会修改 `ppa.toml`。连续 2 次仍然选择非法模块后才停止为 `blocked`，防止错误恢复本身变成
无限 Pro 调用。

如果一个合法模块已经从黑盒名单移除，但其验证因功能、预检或 PPA 得到确定失败，配置和结果会保留，下一轮只能从
剩余黑盒中选择。若只是 SSH、容器或超时导致 exit 75/76，则没有模块失败证据，controller 保留待验证状态并重试原模块。

每个 Pro 决策还必须声明 `reusable_knowledge: append|none`。它是弱控制：`append` 表示当前结果形成了
值得跨项目复用的根因、约束、验证规则或预防制度，Pro 应优先选择
`record_reusable_knowledge` 追加到 `questionAndsolution.md`，但不会阻止一次已有信息增益的 `continue`。
`record_reusable_knowledge` 仍只能配 `append`，且只可写经验库。没有经验证的迁移价值则明确写 `none`，
不把普通逐轮记录塞进经验库。

要求在当前 Flash 批次结束后停下时，读取 `status/pro_supervisor.status` 的 session，并执行：

```bash
bash scripts/request_pro_supervisor_stop.sh <session>
```

这只写入 `stop.request`，不会中断正在运行的功能验证或 PPA。当前 Flash 批次结束后（若当前只是
Pro 决策/受限动作，则在其返回后），supervisor 会**直接**写入 `stopped` 并退出；不会再消耗一次
Pro 调用来请求模型同意停止。

查看当前或最近 session 的简洁状态：

```bash
bash scripts/pro_supervisor_status.sh
```

也可用 `--session <session>` 查看历史 session。默认只显示所选 session 的当前摘要，不显示累计历史。
需要查看该 session 的原始 Pro 决策、受限动作和 Flash controller 日志时，显式使用：

```bash
bash scripts/pro_supervisor_status.sh --verbose --tail 160
```

`--tail N` 控制 `--verbose` 下各份原始日志的显示行数。需要从全局累积报告中查看**该 session**的历史条目时，
显式使用 `--history --tail N`；它不会混入其他 session。

## 已实施 harness 改动的强制验证

Pro 自己完成 `repair_harness` 后，supervisor 会记录本次变更为待验证状态。下一批不再允许 Pro 再次
`repair_harness`：无论其文字决策如何，controller 都会强制一次仅 `top_level_sta` 单网表 smoke（预算按分区
`timeout_s` 提升到合法档位），待新 result/feedback 落盘后才清除该状态并允许下一次修复。

人工或外部 agent 已完成的 harness 修复也必须显式登记，避免 Pro 只能看到内容哈希、却不知道改动语义：

```bash
python3 harness/pro_supervisor.py --declare-pending-harness-change \
  --pending-change-path scripts/remote_yosys_round.sh \
  --pending-change-summary '改用已在目标 OpenROAD 探测过的 all_inputs -no_clocks；仅待一次 STA smoke 验证' \
  --pending-change-expected-phase 'sta_timing_measured|sta_no_timing_path'
```

登记记录会明确提供给 Pro，并作为 controller 的硬门；它不是让模型自行解释的 prompt 建议。

## 顶层已链接但无时序路径的强制诊断

`sta_status=ok_no_path` 只说明 `report_checks` 没有打印可用路径，不能说明 RTL 慢、快或已经没有问题。
若旧 feedback 没有同一真实链接设计的 `sta_diag_*` 事实，supervisor 会覆盖 Pro 的本轮文字决策，强制一次
零修改、仅 `top_level_sta` 单网表 smoke（预算按分区 `timeout_s` 提升到合法档位）。该 run 会记录：`clk` 端口数、已创建时钟数、寄存器数和非时钟
输入数。新证据落盘后才允许 Pro 选择修 harness/约束、修订任务卡或停止调试；不会连续盲跑完整 PPA。

## 强制治理门禁

默认情况下 supervisor 不接受没有新语义证据的第二次 `continue`：指纹包含最近真实 result/feedback 的
稳定字段，以及 RTL、任务卡、harness 和脚本内容哈希，不包含 run-id 或时间。它还在批次边界自动停止于
以下任一条件：连续相同结果、相同测量配置与结果重复、Flash 总回合预算、PPA 请求预算总和或墙钟预算。
这些阈值均可通过 `--max-*` 参数调整并记录到 `session_state.json`。默认长运行窗口为 24 个 Pro 决策、
72 个 Flash 回合、86,400 秒请求 PPA 预算及 7 天墙钟；用户仍可通过 `stop.request` 在任一批次边界停止。

默认每 3 个 Flash 批次，supervisor 会向 controller 传入 `--force-full-measurement` 与
`--final-regression`：前者禁止 targeted PPA，后者在该批次结束后执行 prompt-0 的 48-token 回归。
可分别以 `--full-ppa-every-batches` 和 `--full-regression-every-batches` 调整。

## 测量计划模式

`harness/ppa_agent_loop.py` 只认 controller 自己的测量计划，不信 AI 的空口说明：full 是安全默认；
`local` 且确实只改一个 RTL 文件时 targeted 才成立；脚本/测量 harness 改动且无 RTL 改动时，controller
把 targeted 保留为 smoke——冒烟分区取 planner 显式声明且通过校验的合法分区（一个分区时跑单网表 smoke，
多个分区时跑 targeted 分区集），而不是写死 `harness/ppa.toml` 里第一个已配置分区，绝不因此重复
完整分区套件。这样脚本修复既能在秒级拿到新的真实面积/时序/scope 证据，又不会触发 29 分区全量超时。
若一个假设最终没有产生任何受管文件改动（既无 RTL 改动也无 harness/脚本改动），controller 会路由到
16-token 功能计数点并跳过 PPA，而不是把无新证据的假设误提升为 full 并重复相同分区测量。唯一例外是
planner 显式声明 targeted 并给出合法分区名：它表示“重验一个已落地但尚未得到 PPA 验证的 harness/测量
改动”（例如先前落地、但 PPA/STA 验证从未完成的 closure 修复），属于具体 PPA 假设而非纯计数点，controller
会按请求分区跑单网表 smoke（一个分区时）或 targeted 分区集，而不是降级为功能计数点。该计数点会用真实
feedback 解析 `summary.json.requested_seq_len` 与 `simulation.log` 的 `PASS cycles=`，回报 C(n) 与边际
`m=(C(n2)-C(n1))/(n2-n1)`；任何一项缺失都写“未测得”，绝不把 AI 估算或旧轮数字回填。supervisor 的
`--force-full-measurement` 里程碑仍会周期性禁用 targeted 与 smoke，强制一次 full。

## 可迁移的 STA 范围与 RTL 优化治理

本项目通过 `harness/rtl_skill_bridge.py` 接入三个外部 RTLskill；项目代码只负责把实际 diff、当前配置、结构化证据、
已有展开结果和分区名转换为结构化输入，通用政策不复制进任务卡：

- `rtl-sta-scope` 在每次真实 RTL diff 后选择最小可辩护 STA 范围。项目侧 `design_context_adapter.py` 按当前
  RTL/filelist/配置指纹构建或刷新层次关系与时序边界图，登记簿只维护和校验该投影；真实 diff 与当前模块上下文
  一起交给 Skill。接口、时钟复位、参数、共享状态、组合边界、filelist、架构或上下文影响不明时一律提升到顶层。
- `rtl-optimization-triage` 只把历史上至少一次完成展开功能门、Yosys 预检并实际得到 timing path 的模块列为候选，
  但把“历史验证过”与“当前为降低 closure 资源而临时黑盒化”分开。临时黑盒模块仍可局部优化，只是带
  `top_level_acceptance_pending=true`。Skill 将原集成目标 E 与本次优化目标 O 分开：O=E 时生成
  `fused_same_unit` 计划，先优化再完成原展开验收；O≠E 时生成 `decoupled` 计划并保留 E。当前 controller 只允许
  已经以 RTL body 进入设计的 decoupled 候选，禁止为优化偷偷移除第二个黑盒。失败时恢复 RTL 与必要的
  `ppa.toml`，超时/基础设施不确定则保留待验证状态。Skill 保存被插入优化打断的黑盒展开主线，分别维护
  优化效果 O 与集成验收 E 两个验证义务，并维护
  `editing → functional_repair → sta_required/measurement_pending → rollback_required/resume_pending` 状态，并由 `step` 输出唯一内部执行请求。

候选价值不是固定默认分：项目适配层从不可变 feedback 中提取模块级 ABC 面积、延迟、路径类别和来源 run，
并从当前 RTL 统计实例数作为 replication proxy（复制程度代理）；Skill 才据此计算 timing/area/replication
收益与风险成本。无可归因量化字段的模块直接不具备自主优化资格，不能靠模型补造分数，也不能把默认负分写成“没有优化必要”。

Stage-1 候选由 Skill 按量化证据评分并在超过硬阈值时自动选择，不再由 Pro 输出 `optimize_rtl`。Skill runner 调用 `--flash-model` 指定的单次模型，只允许修改该模块唯一 RTL 文件；随后通过项目执行端口在同一 campaign 内完成必要的功能门、Yosys 预检和局部/受影响锥 STA。若最小 STA 范围要求共享/全局 STA，runner 输出验证义务并结束本次内部循环，Evidence/Pro 再决定如何与黑盒展开主线合并。功能失败不启动 STA；默认允许两次同候选修复，耗尽后从 Skill checkpoint
自动回退。初次测量不完整后，默认最多允许两次无编辑重测；重测不会再次编辑 RTL，指纹匹配时复用功能证据，指纹缺失或变化时只重新跑功能门。两次仍未完成则进入 `rollback_required`；Skill runner 执行并验证恢复，controller 只提供执行端口。SSH/容器不确定不计入修复或领域测量重试。候选生命周期写入
`status/task02_optimization_state.json`；被打断的主线只保存为指向 Registry 当前主线的不可变引用，不再维护第二份 Transaction Ledger。功能通过后，实际 diff 再交给
`rtl-sta-scope`，而不是相信 planner 自报的 targeted/full。局部或 affected-cone（受影响逻辑锥）STA 通过只记
`local_accepted`；需要顶层验收时写入 `acceptance_debt`，直到匹配的 `top_level_partial/top_level_complete` 证据才清除。

STA 成功不会自动等同于优化成功。项目 adapter 只上报规范化测量字段和产物可用性，Optimization Skill 判定测量完整性与同口径收益；默认要求面积或延迟至少改善 1%，且另一项不得恶化超过 1%。超时、截断和缺失测量产物属于可有限重试的不完整测量；工具已完成但口径键不同则直接回退暂缓，不再当作超时重测。两个比例都可由 supervisor 参数调整。

`--max-optimization-measurement-retries` 可调整优化候选的无编辑测量重试次数，默认值为 2。Pro 继续负责集成与闭合战略，但不参与普通 Stage-1 候选选择、编辑、验证重试或回退。

新候选是否允许插入完全取自 `rtl-optimization-triage` 的 `insertion_gate`；该字段缺失时 fail closed（保守拒绝），不再由 supervisor 根据候选数组重建一套旧政策。活动优化 campaign 存在时按 Skill 状态机完成或回退，然后恢复 Registry 引用的主线。黑盒允许集合在 Task 02 adapter 中取 Registry `expandable_blackboxes` 与当前 `ppa.toml` 名单的交集；运行时配置新鲜度也由 adapter 检查。

闭合资源耗尽后，supervisor 不再根据 `continue` 或“需要 Flash”推断是否重复测量。项目 adapter 使用与执行端相同的输入集合生成语义指纹，并把已耗尽测量和 Registry 当前主线的拟测量交给 `rtl-closure-governance`。Closure Skill 排除预算后比较设计/配置指纹、范围类型和目标集合，输出 `same_measurement`、真实策略变化或 `unknown`。`rtl-optimization-triage` 再根据 Adapter 提供的原始 Registry 事实和该政策生成 `execute_mainline_transaction` profile，经 Evidence validator 校验后交给 supervisor；Adapter 不再预先生成动作、operation kind 或 intent。

`rtl-module-evidence-registry` 维护全部 filelist/分区配置模块的当前表示、项目 extractor 提供的版本化设计上下文、严格展开验证证据、带上下文的面积/时序测量和当前黑盒展开主线；它不解析 RTL 构图，也不承担通用父子事务调度。
仅在配置出现、但当前 filelist 没有 RTL body 的黑盒会保留在登记簿中，却不会出现在可展开候选列表。
controller 在每个 Pro 指导周期前以及 Flash 执行结果后重建
`status/task02_module_registry.json`，并向事件与测量 JSONL 只追加新增记录。紧凑热视图以标准 `skill_module_registry` 信封进入 Evidence Model，
Pro 读取经校验的统一摘要；只有可展开名单、当前待处理模块/阶段、generation 和 hard gate 等最小授权字段由 controller 直接提供。
完整登记历史和原始热视图都不进入 Pro prompt。
`expand_blackbox` 在写 `ppa.toml` 前必须通过登记簿 `can-expand` 硬门，因此旧 progress 或旧主线不能授权展开当前非黑盒模块。
若热视图的 session、cycle、phase、配置/filelist/RTL 指纹或证据水位不一致，controller 按基础设施不确定停止，禁止沿用上一轮快照。

默认 Skill 路径是相邻的 `../RTLskill/rtl-sta-scope`、`../RTLskill/rtl-optimization-triage` 与
`../RTLskill/rtl-module-evidence-registry`。迁移到其他 harness 时可使用 `--sta-scope-skill`、
`--optimization-triage-skill`、`--module-registry-skill`，或对应环境变量
`RTLSKILL_STA_SCOPE`、`RTLSKILL_OPTIMIZATION_TRIAGE`、`RTLSKILL_MODULE_EVIDENCE_REGISTRY`。项目 adapter 只需替换
filelist、分区配置、结构化 result 和测量日志的映射，不需要复制 Skill 政策。
执行模型仍由 `--flash-model` 指定；候选评分和授权是确定性的，没有额外长期对话历史或隐式模型依赖。
