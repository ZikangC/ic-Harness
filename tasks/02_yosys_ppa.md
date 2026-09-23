# 任务：当前 Nano-Golden RTL 的综合与 PPA 测量

## 工程边界

本任务只使用当前工程：`/Users/chenzikang/code/work/RTLharness`。

- 综合顶层：`nano_golden_decoder_top`
- RTL：当前工程的 `rtl/` 与 `rtl/filelist.f`
- 功能正确性：当前工程的 `reference.py`、image builder、Verilator testbench 与 golden scorer
- 服务器：`/home/txjg7-2/ic-project/k3harness`
- 容器：同一个 `k3demo-eda`，工作目录 `/work/k3harness`

`nano-kpu` 不属于运行依赖：不导入、不读取、不同步、不修改。它只提供下列资源与性能预算的
参考来源；本工程必须独立复现并测量自己的 RTL。

## 任务目的

对已经完成 RTL 编写、编译和 Python golden 校对的当前设计，建立标准化流程：

```text
功能回归 → lint / protocol audit → Yosys 综合 → ABC Nangate45 单元映射
→ 标准单元逻辑面积 + SRAM/ROM 宏统计 → critical-path pre-layout 估算
→ cycles/token → tokens/s → 与预算比较的报告
```

这不是要求 AI 盲目把 PPA 优化到固定数字；初版目标是可信、可复现地测量并判断是否满足预算。
只有功能回归通过时，PPA 数据才有效。

## 借鉴的 Nano 预算

| 指标 | 预算/目标 |
|---|---:|
| 标准单元逻辑面积 | ≤ 4.0 mm² |
| 片上 SRAM/ROM 容量 | ≤ 4 MiB |
| SRAM/ROM 宏面积模型 | 约 1 Mbit/mm² |
| 外部 DRAM 带宽 | 16 B/cycle |
| 目标频率 / 周期 | 100 MHz / 10 ns |
| 吞吐目标 | 20,000 tokens/s |

```text
总面积估计 ≈ 标准单元逻辑面积 + SRAM/ROM 宏面积
```

正式映射使用 Nangate45 Liberty，作为当前工程受控依赖同步；不得用 ASAP7 或容器中的随机示例库
替代正式测量口径。

### STA 后端口径

容器的标准 STA 后端是 **OpenROAD 内嵌的 OpenSTA**：即使没有独立的 `sta` 或 `opensta` 命令，
`/opt/OpenROAD/build/bin/openroad -no_init` 仍可完成 STA。对 Nangate45 映射网表，脚本必须先读取
`NangateOpenCellLibrary.tech.lef` 与 `NangateOpenCellLibrary.macro.lef`，再读取 typical Liberty、映射后
Verilog、`link_design`、建立 10 ns 时钟并执行 `report_checks`。缺少独立 `sta` 二进制不能被报告为
“STA 不可用”。

该流程测得的是 pre-layout STA。若顶层闭包仍依赖测量黑盒或存在未链接单元，结果必须标为
`PARTIAL_HIERARCHICAL` / `sta_status=partial`，不得作为最终全芯片时序结论。

## 报告要求

每轮在 `reports/02_yosys_ppa/progress.md` 记录：

- 当前 RTL 的功能回归结果；
- Yosys / ABC / Nangate45 Liberty 的版本和命令；
- logic area、macro area、总面积估计、SRAM/ROM bits 与 MiB；
- critical path、与 10 ns 的比较、估算频率；
- cycles/token 与 tokens/s、同 20,000 tokens/s 的比较；
- 明确说明测量是否是 pre-layout estimate；
- 若提出优化，给出同一约束下的优化前后对比，且先证明功能不回归。

初版是测量与预算比较，不因超预算自动把 RTL 判为功能错误；不得把“综合命令成功”误写为
“满足全部预算”。

## 固定迭代政策

```text
无 AI 基线（8-token 功能快检 + 短预算 PPA 探针）
→ AI 只读复盘/规划一个因果假设并选择下一次测量范围/预算
→ AI 将该假设作为一个受限原子改动实施
→ 8-token 功能快检
→ 通过后运行 Yosys/ABC 测量（controller 按 AI 的证据选择预算档位）
→ 下一轮 AI 根据真实结果复盘
→ 在阶段里程碑和任务结束时运行顶层 PPA
```

在任何 PPA 测量前，controller 先运行最长 600 秒的 **Yosys 综合预检**：只执行 RTL 读取、
hierarchy、`proc`/`memory` lowering 和基础优化，不进入 ABC。预检失败时进入综合可用性修复模式；
预检通过后才允许 Yosys/ABC 测量。`--preflight-only` 可用于只迭代功能与综合可用性，避免
每轮等待映射。

### 自主资源政策

- 端到端顶层 PPA 仍是最终判据；任何分层、黑盒或单模块结果只能用于定位与阶段性报告，不能替代
  最终顶层面积、跨模块时序和吞吐结论。
- 每次 PPA 测量必须在日志和 feedback 中声明 `measurement_scope`。只有真实顶层闭包、无测量黑盒的
  `TOP_LEVEL_COMPLETE` 结果才可进入纯 RTL 的“PPA 优化模式”；`PARTIAL_HIERARCHICAL`、`UNKNOWN` 或
  任何缺失分区的结果都进入“测量完善/端到端收敛模式”。这不禁止 controller 在独立 Optimization Skill
  与功能/登记簿硬门通过时插入一个有 checkpoint 的模块优化实验；它只禁止把阶段性结果宣传成完整 PPA。
- **部分闭合后的逐模块展开例外**：当真实 STA 审计满足 `sta_link_result=ok`、
  `sta_timing_measured=yes`，且最近功能回归通过时，`PARTIAL_HIERARCHICAL` 不再阻止逐步展开。
  Pro 可以选择 `expand_blackbox` 并提供一个当前 `top_level_sta` 黑盒模块名；controller 只从
  `harness/ppa.toml` 的 `top_level_sta.blackbox` 列表移除这一个模块，随后强制运行 8/16/48-token
  功能门、Yosys 预检和 `top_level_sta` targeted PPA。默认验证期间禁止再次修 harness 或移除第二个模块；
  但若 Optimization Skill 对当前待展开模块生成 `fused_same_unit` 计划，controller 可以先允许该模块的单文件
  RTL 优化，再用同一组功能门和按实际 diff 决定的 STA 验收原展开事务。其余模块仍为黑盒，结果继续标记
  `PARTIAL_HIERARCHICAL`。存储宏面积缺失只阻止完整总面积
  结论，不阻止首次模块展开实验。
- **黑盒候选决策错误恢复**：controller 必须把当前 `top_level_sta.blackbox` 的完整候选列表放进
  Pro prompt。若 Pro 选择了不在当前列表中的模块，不能启动 Flash、修改配置或将该次决策当作 RTL/PPA
  失败；应记录被拒模块，带着排除列表重新请求 Pro 选择其他当前候选。最多重试 2 次；连续仍非法时才
  停在 `blocked`/可调试状态。若模块展开后的验证返回基础设施 exit 75/76，说明没有得到模块失败证据，
  必须保留待验证展开并重试同一模块；只有得到确定的功能/预检/PPA 结果后，下一轮才从剩余候选选择其他模块。
- controller 必须对配置中的全部测量分区做调度审计：每个分区都要有完成、超时或因总预算跳过的
  终态，并在 feedback 中报告预期数、实际终态数和 `scheduler_audit`。数量不一致属于 harness 错误，
  不能当作有效测量成功。
- **STA 闭合审计门**：每次含 `top_level_sta` 的测量必须从真实 feedback 生成结构化
  `sta_closure_audit`，至少记录 closure 文件字节数/模块数、顶层是否存在、STA read skip 数、link
  结果、是否真的得到 `report_checks` 时序。没有 timeout/truncation 证据时，`closure_export_empty`、`closure_top_missing`、
  `closure_body_missing`、`undeclared_blackbox`、`sta_parse_error`、`sta_link_error` 或
  `sta_no_timing_path` 时，不得把 `TOP_LEVEL_COMPLETE` 或 Yosys exit 0 写成闭合成功。controller
  必须暂时禁止通用 full PPA 里程碑与功能计数回退，并固定执行 `top_level_sta` smoke；只有
  phase 迁移到 `sta_timing_measured`，才解除该门。若 closure 导出为空同时明确是 timeout/truncation，phase
  必须为 `closure_measurement_incomplete`，`closure_repair_required=false`。同一测量只允许以严格更高且不超过
  7200 秒上限的预算执行 `increase_budget` smoke，不得伪装成 harness 修复。达到最大合法预算时，转入
  `closure_resource_blocked` 并禁止继续重复同类测量。该 phase 只要求改变测量策略、
  执行只读局部诊断或停在可调试状态，不授权 `optimize_rtl`。是否允许 RTL 优化必须由 controller 在 Closure Skill
  外组合模块登记簿、干净功能基线、待处理事务和 Optimization Skill 插入计划后决定。组合门允许时，可以先优化
  已验证候选，再按实际 diff 选择 STA 范围并恢复原黑盒展开主线；该许可不等于 closure 已完成，也不允许重复原测量。
  `sta_timing_measured` 只证明当前闭合网表的真实 STA 已运行；若它包含测量黑盒，仍为
  `PARTIAL_HIERARCHICAL`，不是最终无黑盒全芯片 STA。
- `sta_no_timing_path` 不能直接归因给 RTL。若 feedback 尚无同一真实链接设计中的 `sta_diag_status`、
  时钟端口数、已创建时钟数、寄存器数和非时钟输入数，supervisor 必须先强制一次零修改的
  `top_level_sta/600s` 诊断 smoke；禁止在这之前修改任务卡、猜测约束或重跑 full PPA。诊断后再按
  `no_timing_path_cause`（时钟缺失、网表无寄存器、或寄存器存在但无路径）形成可证伪假设。
- SRAM/ROM 容量按每个已完成分区的层次汇总最大值计一次，避免同一 memory 同时出现在叶模块局部统计
  与顶层含子模块统计时重复相加；未到达 memory-stat 检查点的分区必须标为 `unobserved`，不能当作零。
- controller 提供固定的测量预算档位：`180 / 600 / 1800 / 3600 / 7200` 秒。规划 AI 在每轮最后用
  `measurement_budget_seconds: N` 选择其中一档；controller 只接受合法档位并实际执行命令。
- 默认是 180 秒探针，目的是得到“走到哪个 pass、是否稳定推进、哪个分区最重”的新证据，而不是
  强制得到最终面积或时序。
- 若上一轮 PPA 未完成，且 RTL、`ppa_measure.ys`、`ppa.toml`、黑盒声明和远端测量脚本都未改变，
  新一轮不得把预算降到低于上一轮；这只会更早截断同一个实验。只有实际改变测量配置，并在规划中
  说明更短探针能验证的新假设时，才允许降档。controller 对同一配置的无解释降档会保持上一轮预算。
- 只有日志表明短预算已稳定推进、且更长预算将产生新的可验证测量时，AI 才可选择升级。7200 秒仅可
  用于重点模块或顶层里程碑；不得因为“再等一会”而重复使用。
- 当一次短预算超时，AI 可以自行选择分层测量、叶模块测量、黑盒化、综合脚本调整或 RTL 结构优化。
  选择必须引用上一轮真实日志并说明为何比重复顶层综合更有信息量。
- PPA 工具返回 `exit 124` 只表示本次测量预算耗尽。若同一结果的功能门和 Yosys 预检均通过，controller 必须记录
  `measurement_status=incomplete_timeout`，保留已有局部证据并进入下一次 Pro 指导周期；单模块展开验证也必须保留待验证模块，
  不能把该状态映射成 `blocked` 或 RTL/功能失败。普通闭合测量在下一战略边界重新经过 Evidence Model；活动优化 campaign
  则由 Optimization Skill 在不调用 Evidence Model/Pro 的内部步骤中有限重测或回退。只有功能/预检确定失败、exit 75/76 基础设施状态或其他明确失败才进入对应阻断策略。
- `--max-cycles` 是 Pro 指导周期上限，不是单次 Flash/PPA 的执行次数；一次指导周期可以包含多个 Flash attempt。超时继续时仍受
  Flash 总次数、PPA 请求总预算、墙钟、用户 stop 和其他治理上限约束；`exit 124` 本身不能被当作完整 PPA、无黑盒 STA 或 Fmax 证据。
- **“每轮一个问题”定义为一个可验证的因果假设，不定义为一个文件。**规划输出必须声明
  `change_mode: local|architecture`：`local` 是局部假设，至多改一个 RTL 文件；`architecture` 是确有
  必要的跨模块联动假设，可修改多个 RTL 文件，但必须把它们作为一个原子变更集，列出全部文件、接口约束
  和功能不变量。controller 在 local 多文件改动时回退；architecture 一律做全量配置分区 PPA。
- 对 local 的真实单 RTL 改动，规划者可以声明 `measurement_mode: targeted` 并列出 1–4 个
  `harness/ppa.toml` 中的精确分区名。controller 只测这些分区来验证当前假设的局部 PPA 影响；名称非法、
  无 RTL 改动、测量 harness 改动或 architecture 改动时，自动回退为 full（全部已配置分区）而不是猜测。
  连续两次有效 targeted 后 controller 自动插入一次 full，避免局部改善掩盖系统级退化。full 配置分区
  覆盖仍是阶段性测量，不是无黑盒顶层闭包；最终结论仍需完整功能回归和真实顶层 PPA。
- SSH/rsync 等传输层错误统一为 exit 75。exit 75 不代表 RTL、预检或 PPA 失败；controller 必须先只
  重试被中断的验证，再回到最近一轮非 75 的真实结果继续规划，且不得消耗 AI 修改回合。
- 每个远端 PPA 分区的 SSH 客户端必须有独立墙钟上限（远端工具预算加固定 grace），不能仅依赖
  `ConnectTimeout`。墙钟触发后 controller 应用新的有界 SSH 读取该分区已落盘的 exit code；读到则恢复
  真实工具结果，读不到则标记 exit 75，禁止重发同一分区命令造成重复综合。
- planner 或 executor 出现配额耗尽、模型 runtime/error 响应或非零 OMP 退出时，controller 必须立即
  fail-fast：planner 失败不启动 executor；executor 失败回退该阶段的受管输入半成品；两种情况都不得运行
  功能快检、预检或 PPA。模型恢复后从新的回合开始，日志和失败原因作为唯一证据保留。

如果功能快检通过、但 Yosys/ABC/PPA 在获得测量前失败，controller 不得直接结束：应进入
**综合可用性修复模式**。该模式先修复 RTL 的综合兼容性或 PPA 流程本身，再重新执行
8-token 功能快检和 PPA，直到得到可信基线或功能回归失败。

如果 8-token 功能快检失败（包括 Verilator 读取/编译 RTL 失败），controller 应进入
**功能/Verilator 可用性修复模式**：下一轮 AI 必须收到最新功能 feedback，只允许修改 `rtl/`，
并先恢复功能快检；在功能通过前不得运行 PPA。

- 8-token 是快速功能门，不等同于完整功能证明。
- **任意 `rtl/` 文件改动**后，controller 必须在预检/PPA 前强制运行 16-token 回归。
- 若改动的 RTL 文件名表明它属于 FSM、cache、arbiter、controller、loader、stream、datapath、adapter、
  attention、matvec、expert、norm、conv、residual、KDA 或 descriptor/cache 类数据路径，controller 还必须在预检/PPA 前强制
  运行 prompt 0 的完整 48-token 回归。两项通过前不得让该改动的 PPA 结果指导下一次 RTL 修改。
- 纯测量 harness、分区顺序或 PPA 配置改动只需 8-token 快检；每 3 个通过快检和得到有效 PPA 测量的候选，
  仍额外运行 16-token 回归。该候选计数属于整个 Task 02，跨多次 controller 启动持久化。
- 任务结束前仍必须运行一次 prompt 0 的完整 48-token 回归。
- 规划者只读；若发生 RTL 写入，controller 会恢复该阶段快照并记录违规 diff。
- 常规 PPA 优化实施者只允许改 `rtl/`。综合可用性修复时，额外允许修改
  `harness/ppa_preflight.ys`、`harness/ppa_measure.ys`、`harness/ppa.toml`、`scripts/remote_yosys_round.sh`；不得修改
  其他 harness 控制逻辑。
- 面积、时序、频率、cycles/token、tokens/s 只能引用 controller 产生的真实日志；没有测得就写“未测得”。
- 每个阶段写入 `status/task02_agent.status`，阶段为 `planning`、`editing`、`functional_quick`、`ppa`、
  `measurement_completion`、`module_expansion` 或 `complete`。`measurement_completion` 表示功能与局部
  测量可用，但尚未得到无测量黑盒的顶层闭包；在满足逐模块展开门后，controller 必须优先允许
  `module_expansion`，不得因为仍有其他黑盒而无限停留在通用 harness 修复。

## 验证性能政策

- Verilator 编译使用 32 个 job；服务器端编译产物按 RTL/TB/配置内容哈希缓存。
- 改变输入 image 或 token 长度时复用同一 RTL 的缓存；改变 RTL/TB/编译参数时自动生成新缓存。
- Yosys/ABC 单次映射多数为单核流程，不把“64 CPU 可见”误解为单次综合一定能线性加速。
