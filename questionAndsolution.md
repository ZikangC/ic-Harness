# RTL Harness 端到端工程经验库

本文件记录通向**端到端芯片设计流程**时出现的工程案例，并从案例中提炼可跨项目复用的 harness
设计原则：功能建模、RTL、验证、综合、时序/面积评估、性能评估和自动化调度如何真正闭环。它不是
逐轮实验报告；每轮具体的 RTL 修改、diff 与评分结果保存在 `reports/<task>/progress.md`。

更新原则：记录可复用的问题、可验证的根因、已经采取的修复和避免复发的规则；每个阶段未能
端到端闭环时，明确归类为“任务卡缺口”“调度策略缺口”“harness/工具链实现缺口”或“设计本身
的限制”。不记录单纯的命令拼写、当前目录等低价值操作失误。

## 文档结构与准入标准

- **本项目案例库**保留必要的日期、日志证据、文件名和实际参数，用于说明问题如何被证实；只有能
  推导出固定预防规则的案例才进入本文件，其余逐轮细节留在 `progress.md`。
- **跨项目原则**只保留机制、适用条件、证据要求和固定制度；未来建立新 harness 时优先阅读这一部分，
  不直接照搬案例中的路径、模块名、token 位置或 timeout 数值。
- 一个案例若尚未形成可验证规则，应标记为“待提炼”，而不是把临时操作方式写成永久制度。

## 端到端闭环定义

一个阶段只有在输入可复现、自动命令真实执行、结果可解析、失败能路由到下一步修复、且通过结果
能作为下阶段可信输入时，才算完成端到端闭环。AI 的文字判断、手工估算或“命令曾启动”都不构成
闭环证据。

## 2026-09-02：Pro / Flash 分层自动迭代必须以批次边界协调

### 现象与风险

把战略 Pro 与执行 Flash 同时常驻会造成两类不可审计竞争：Pro 可能基于尚未落盘的 Flash 结果改写
任务方向，而用户的停止意图若直接杀进程，会截断功能/PPA 证据并留下半成品输入。

### 固定策略

阶段 Evidence Model 先把当前 `result.json`、feedback、progress 增量、闭合审计和 controller 状态整理为一个
无累计历史的阶段证据包，Pro 只读取该证据包和 controller 硬门摘要。Pro 输出保留原文的结构化决策：继续时指定
1--6 个 Flash 回合和一份 advisory directive；修订任务卡或受限 harness 时
由 Pro 在快照/允许范围内执行。Flash 运行期间 Pro 空闲。停止命令只写 `stop.request`，由 supervisor
在当前批次自然结束后读取、调用 Pro 生成停止结论并转入调试，不中断活跃验证。所有 Pro 决策、Flash
父日志和实际 controller 子日志都必须保存在同一 supervisor session 下。

### 隔离原则

本地副本不足以隔离 RTL harness：远程工程、Docker 容器工作目录、`build/`、`runs/` 和 `status/` 都必须
有独立命名空间。副本脚本应以唯一环境配置导出这些路径；启动前拒绝与另一 controller 并发，避免共享
服务器 CPU 使 PPA 结果失真。

### 长运行的强制门禁

“只有新证据才继续”不能只依赖模型自律。supervisor 应在每个批次边界生成去除 run-id/时间的语义证据
指纹：最近真实结果的稳定字段、紧凑 PPA feedback 的指标行，以及 RTL/任务卡/harness/脚本内容哈希。
新的 `continue` 若与上次 continue 的指纹相同必须被拒绝。session 还应累计相同实验+结果次数、连续
相同结果次数、Flash 回合数、请求的 PPA 预算和墙钟；任何上限命中均停在边界并保留 state/log。

局部 Flash 探针会漂移，因此 supervisor 必须按固定批次节奏强制 controller 做全配置 PPA 与 prompt-0
48-token 回归。强制 full 必须是 controller 的命令行硬开关，而非让模型在 directive 中“尽量选择 full”。

## 案例索引与端到端分类

| 历史问题 | 归类 | 阻断的闭环环节 | 固定预防方式 |
|---|---|---|---|
| 多 token 首个离散分叉 | 设计/验证策略 | 因果状态验证与修复定位 | 最短因果前缀 → 完整序列回归 |
| image builder 路径、诊断参数不一致 | harness 接口 | golden/image/诊断可复现性 | 同步的配置、工具和命令作为一个版本化接口 |
| AI 调用 `tail -f` 长时间阻塞 | 调度策略 | AI 决策阶段可终止性和可观测性 | AI 只运行有限命令；实时监控由人独立执行 |
| 远程产物回传量控制 | 执行架构 | 远程验证与本地 AI 反馈闭环 | 完整产物留服务器，仅回传有界文本反馈 |
| 长序列超时 | 验证预算 | 多 token 回归覆盖率 | 快检/中等回归/完整回归分层，超时与功能失败分开解释 |
| run-id 导致陈旧反馈 | 结果管线 | 下一轮 AI 输入正确性 | 按结果修改时间选择最新反馈 |
| Yosys package import 失败 | 综合兼容性 | RTL → 映射/面积/时序测量 | 目标工具最小 baseline；失败进入综合可用性修复模式 |
| `write_verilog` 全设计写跟随选区、`select *` 不可靠 | harness/工具链实现缺口 | 分区网表 → 顶层 STA link | 显式选区 + grep 验证 body + 保留 `(* blackbox *)` |
| 慢 closure 分区被更小 round 预算截断 | 调度策略 | 分区网表导出 → 顶层 STA link | 目标测量预算取所选分区 `timeout_s` 最大值并升到下一档；禁止低于声明窗口 |

## 2026-09-08：STA 约束接口必须由目标二进制探测，不可凭同族工具经验替换

### 现象与根因

顶层综合网表已可被 OpenROAD 内嵌 OpenSTA 读取并完成 `link_design`，但在
`report_checks` 前设置输入延迟时失败。先后假设
`remove_from_collection`、`filter_collection` 可用，实际容器均报“invalid
command name”。两次修复各消耗一次完整的 `top_level_sta` smoke，却没有推进
STA 阶段。

### 固定预防规则

对工具脚本 API 的兼容性修复，先在**同一容器、同一二进制**中运行秒级
`info commands <name>` 和 `help <command>` 探测；不得根据 PrimeTime、Design
Compiler 或其他 OpenSTA 版本的经验直接替换。该容器的已验证写法是
`all_inputs -no_clocks`，它直接表达“所有非时钟输入端口”，无需集合相减。
把探测结论连同正/负字符串断言写入测试；探测通过后才允许一次有界 STA smoke。

## 2026-09-09：report_checks 路径数选项同样随 OpenSTA 版本漂移

### 现象与根因

collection 原语替换生效后（`error_signature` 已从 `invalid command name
remove_from_collection` 迁移），post-link timing setup 停在 `report_checks`：
该容器 OpenSTA 报 `STA-0514`。根因是旧 `report_checks -nworst` 选项已删除，
`-nworst` 在 `find_timing_paths` 参数解析后成为剩余位置参数，命中 OpenSTA
源码 `sta_error 514`（"not a known keyword or flag"，容器渲染为 `STA-0514`）；
`-endpoint_count`/`-group_count` 虽仍被解析但已是 deprecated 别名，现行名为
`-endpoint_path_count`/`-group_path_count`。

### 固定预防规则

与 collection 原语同源：`report_checks` 的路径/分组数选项也随版本重命名，不得
凭旧版 OpenSTA/Synopsys/PrimeTime 经验照搬 `-nworst`。本容器已验证写法为
`report_checks -path_delay max -endpoint_path_count 1 -group_path_count 2
-digits 3`。测试必须同时断言新选项存在、`-nworst` 全族缺席；任何 STA 错误码
都必须能映射到具体命令行（正/负断言），否则先在同一容器探测，不盲改。

## 2026-09-09：分层测量预算不得低于被选分区的声明工具窗口

### 现象与根因

`top_level_sta` 闭合并行分区声明 `timeout_s = 600`，其闭环流实测需约 448 s 才能写出
133 MB 的自包含网表 `build/ppa_closure_top.v`。一次 targeted smoke 用默认 180 s 作为
round 预算：`remote_yosys_round.sh` 把每个分区工具窗口截断到 round 剩余预算
（`if (( timeout_s > remaining )); then timeout_s=$remaining`），闭合并行分区在 TECHMAP
阶段即被 `timeout` 杀掉（`exit=124`），`write_verilog` 从未执行，网表 0 字节，后置
OpenSTA 无网表可读，报 `sta_error=STA_NO_COMBINED_NETLIST`，审计落入
`closure_export_empty`。对照证据：同一配置 600 s smoke（`02_yosys_ppa--20260909-161231--80662`）
在 447.55 s 写出 133,661,072 字节、8 个 module 并完成 `link_design`；180 s smoke
（`02_yosys_ppa--20260909-165749--83587`）停在 TECHMAP，`closure_bytes=0`、
`closure_module_count=0`。

### 固定预防规则

调度器为目标/烟雾测量选择 round 预算时，必须把所选分区声明的 `timeout_s` 当作硬下限：
round 预算取 `max(所选分区 timeout_s)` 并升到允许的预算阶梯档，禁止用 flat 默认值覆盖
更慢分区的声明窗口。分层测量的通用不变量是「round 预算是分区的容器，不是裁剪器」——
分区自身的工具窗口是权威，round 预算只能跳过超预算分区，不能悄悄缩短单个分区的窗口。
用单元测试断言慢分区预算下限等于其声明 `timeout_s`（或下一阶梯档），并保留「小预算
targeted 仅用于快分区」的语义。

# 第一部分：本项目案例库

## 2026-08-20：多 token baseline 的首个离散分叉

### 现象

prompt 1 的 3-token 前缀可以正常生成 image、编译、仿真和评分，但评分失败：

- `cos_min=0.3614145`
- `argmax_agree=0.6666667`
- 首个 route 分叉为 `pos=2`：RTL 的 L0/L1 route 为 `((0,4),(5,4))`，reference 为
  `((0,5),(3,1))`。

### 解释与处理

这证明验证链路正常，也确认 bug 真实存在。pos0/pos1 的连续数值误差在 near-tie router
输入处被放大为 pos2 的离散 expert 选择变化，随后 MLA cache/context 大幅发散。修复时应先
对齐最早的 KDA、归一化、PWL/定点计算和 state/cache 更新，不得为特定 token 或 expert
硬编码。

### 固定策略

先运行 `pos0~pos2` 的 3-token 前缀，保留真实的 state history；前缀通过后自动回归完整
prompt 1。完整 run 的新首错为 position N 时，下一轮自动缩回 N+1 token 前缀。

## 2026-08-20：前缀 image 生成器路径与同步目录不一致

### 现象

执行 `remote_round.sh ... --seq-len 3` 时，reference 成功而 image 生成失败：

```text
build_golden_image.py: error: unrecognized arguments: --seq-len 3
```

### 根因

服务器同时存在旧的 `standalone_rtl/tools/` 和每轮同步的
`tools/standalone_rtl/` 两份工具。`harness.toml` 仍指向旧目录；旧版 image builder 没有
`--seq-len`，而新同步版本有。

### 修复与预防

将 `harness.toml` 的 `image_builder` 和 `scorer` 统一为：

```toml
image_builder = "tools/standalone_rtl/build_golden_image.py"
scorer = "tools/standalone_rtl/score_against_golden.py"
```

之后同步后在容器内核对配置和 `--seq-len` 参数，不再依赖服务器遗留的旧工程副本。

## 2026-08-20：单 token 诊断与 `--seq-len` 参数不兼容

### 现象

诊断调度统一把 prefix 长度传给所有诊断工具，但 legacy
`compare_one_token_snapshots.py` 没有 `--seq-len` 参数。

### 修复与预防

`harness/diagnose.sh` 只给多 token 的 `routes`、`snapshots` 工具传递 `--seq-len`；
`one-token` 保持原有参数。这样 prefix 诊断和单 token 边界诊断都可正常运行。

## 2026-08-20：AI 被人工实时监控命令无限阻塞

### 现象

第 1 轮 OMP/DeepSeek 调用了：

```bash
bash scripts/remote_status.sh --follow
```

该选项内部使用 `tail -f` 跟随一份已完成 run 的 `simulation.log`。日志不会再更新，工具调用
不返回，导致 agent 长时间既不修改 RTL 也不启动下一次验证。

### 根因

`--follow` 面向人工终端：人可以按 `Ctrl+C` 退出；AI 以同步工具调用方式运行命令，会等待
命令自然结束。原来的 agent 约束禁止其自行运行 `remote_round.sh`，但未禁止无限跟随命令。

### 修复与预防

在 `harness/agent_loop.py` 的 agent 提示和任务卡中加入强制规则：只允许会自行结束的有限
命令；禁止 `tail -f`、`remote_status.sh --follow`、交互式 shell、无限循环和持续等待日志
更新的命令。监控由用户在独立终端执行，AI 只读取限长反馈或执行有限的诊断命令。

## 2026-08-20：远程产物回传量控制

### 目标

远程仿真产生的 image、dump、obj_dir 和完整日志不应回传本机；本机仍需有足够证据让 AI
进行下一轮修复。

### 实现

`scripts/remote_round.sh` 将完整 artifacts 保留在服务器的 `runs/<run-id>/`，仅把
`summary.json`、各日志末尾和诊断末尾合成为本地的 `runs/<run-id>/feedback.txt`。`agent_loop.py`
以该紧凑反馈作为下一轮证据，同时在 `reports/<task>/progress.md` 记录修改与结果。

## 检查清单

开始一次新任务或发现异常时，依次确认：

1. `harness.toml` 指向每轮会同步的工具目录，而不是服务器遗留副本。
2. 先手动跑一次已知 baseline，确认 reference、image、仿真、评分、自动诊断全链路可用。
3. 有跨 token state 的首错时，使用从 pos0 开始的最短因果前缀，而不是孤立运行出错 token。
4. AI 的命令必须有限并自行结束；实时查看使用人的独立终端，不作为 AI 工具调用。
5. 检查本地 `feedback.txt` 前先确认该 run 已结束；完整服务器 artifacts 默认不回传。

## 2026-08-21：长序列完整回归在评分前超时

### 现象

prompt 0 的完整 48-token run 返回 `simulation_exit=124`，且 `simulation.log` 写有
`HARNESS_TIMEOUT after 1800s`。最后一个 `TOKEN_AUDIT` 是 pos26；route 诊断同时报告
`MISSING positions=[27,...,47]`，因此 scorer 未执行，不能将该 run 解释为数值失败。

### 根因与处理

`harness.toml` 的 `[verification].timeout_seconds` 原为 1800。该预算包含同一条命令中的
Verilator 编译和仿真，并非只限制仿真。按实际进度外推完整 48 token 约需 3300 秒；为容纳
服务器负载波动并把超时作为“疑似停滞”的保护线，调至 5000 秒（约 83 分钟）。每次
`remote_round.sh` 都会同步该配置到服务器。

### 如何读取自动 route 诊断

本机查看 `runs/<run-id>/feedback.txt` 中的 `=== diagnostics/routes.log ===` 段；它只保留
末尾的紧凑结果。服务器保留完整文件：主机
`/home/txjg7-2/ic-project/k3harness/runs/<run-id>/diagnostics/routes.log`，容器内对应
`/work/k3harness/runs/<run-id>/diagnostics/routes.log`。其中 `MISMATCH pos=N` 表示首个
观察到的离散 route 分叉，`MISSING` 表示 run 在到达这些 token 前已经结束或超时。

## 2026-08-24：run-id 命名变化导致 controller 读取陈旧反馈

### 现象

run-id 改为 `任务--时间--prompt--范围--pid` 后，`agent_loop.py` 仍按目录名字典序选择
“最后一个”反馈。旧格式 `任务-时间-pid` 在排序中会排在新格式之后，导致 controller 持续读取
旧的超时反馈，即使新前缀验证已通过，也不会升级到完整回归。

### 修复与预防

`latest_feedback()` 已改为按 `feedback.txt` 的实际修改时间选择最新文件：

```python
return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None
```

以后 run-id 可以继续使用人类可读格式；“最新结果”必须依据生成时间，而不是字符串排序。

## 2026-08-24：PPA AI 推测与真实综合结果混淆

### 现象

Task 02 的第一轮 AI 根据 RTL 结构给出了面积、关键路径、频率和吞吐的“静态估算”，但随后
Yosys 在读取 RTL 前因 `read_verilog -f` 参数不兼容失败。此时不能把 AI 估算当作 Nangate45
综合实测。

### 固定规则

- 只有 controller 保存的 Verilator、Yosys/ABC、scorer 日志中的数字可以标为“实测”。
- AI 可以提出结构风险，但没有工具输出时必须写“未测得”，不得写成实际面积、时序或 Fmax。
- 基线工具链失败时不启动普通 RTL PPA 优化；功能通过而 PPA 失败时进入“综合可用性修复模式”，
  修复 RTL 综合兼容性或受限的 PPA 脚本后重跑 baseline。

## 2026-08-24：Task 02 的分阶段闭环与慢验证策略

### 问题

将“分析、改 RTL、等待、长篇解释”塞入一次 OMP 调用会造成 `Working...` 长时间无可见状态；
AI 还可能在没有真实 PPA 数据时做无效推测。每次完整多 token 仿真和 Verilator 重编译又会使
迭代成本过高。

### 固定策略

```text
无 AI 基线（8-token 快检 + PPA）
→ AI 只读复盘/规划
→ AI 只实施一个 RTL 改动
→ controller 8-token 快检
→ 通过后 PPA
→ 下一轮 AI 读取真实结果
```

- 8 token 是快速门，不是完整正确性证明；每 3 个通过候选跑 16 token，关键数据流改动和
  任务结束前跑完整 48 token。
- controller 写入 `status/task02_agent.status` 标明 `planning`、`editing`、
  `functional_quick`、`ppa` 等阶段；`agent.log` 只在 OMP 返回后才会有可见回复，不能作为
  实时思考流。
- 规划阶段禁止写 RTL；若违规，恢复该阶段快照并保存 diff。中断中的未验证候选必须先保存 diff/
  副本，再回退到轮次开始前的 RTL。

### 编译加速

Verilator 编译使用 `-j 32`，并对 RTL、TB、配置和编译命令计算内容哈希。相同编译输入复用
服务器端缓存可执行文件；只改变 token 数或 image 时不再重编译。Yosys/ABC 单次映射通常接近
单核，不能期待同样的线性加速。

## 2026-08-24：功能仿真通过不等于 RTL 可被 Yosys 综合

### 现象与根因

某些 SystemVerilog 写法可被 Verilator 接受，却不被当前 Yosys 前端支持。例如
`golden_tensor_meta_pkg.sv` 在 package 内使用：

```systemverilog
import golden_name_hash_pkg::*;
```

功能 8-token 回归可以通过，但 Yosys 在读取 RTL 前报 `unexpected TOK_IMPORT`，因此尚未得到
任何面积或时序结果。这属于“合法或可仿真的 SV 与综合前端支持范围不同”，不应误判为功能 bug。

### 修复权限边界

综合可用性修复模式允许修改 `rtl/`（含 `rtl/filelist.f`）、`harness/ppa_preflight.ys`、`harness/ppa_measure.ys`、
`harness/ppa.toml`、`scripts/remote_yosys_round.sh`，足以处理绝大多数 RTL 语法兼容、filelist、
Yosys 命令和 Liberty 配置问题。若失败文件是由工程外生成器产生，直接改生成物会被覆盖；此时
应记录为“需要扩大到生成器或加入受控预处理”的阻塞点，不允许 AI 修改无关工具目录。

## 2026-08-24：Task 2 首次端到端 PPA baseline 未闭环的复盘

### 断点

首次 Task 2 流程的 8-token 功能回归通过，但 PPA 阶段没有产生 Yosys/ABC 测量；Yosys 在
`golden_tensor_meta_pkg.sv` 的 package import 前端解析处停止。因此该阶段的链条实际是：

```text
RTL 功能回归通过 → Yosys 读取失败 → 无映射/面积/时序 → 无可信 PPA 结论
```

### 归因

- **任务卡缺口**：最初只描述“功能通过后测量 PPA”，没有明确“功能通过而综合失败时，先修复
  综合可用性并重跑 baseline”的收敛路径。
- **调度策略缺口**：旧 controller 将任何 PPA baseline 失败都当作终止条件，会阻止 AI 处理
  真实的综合兼容问题。
- **harness/工具链实现缺口**：早期 Yosys filelist 参数与本机日志解析命令不兼容；修复后才暴露
  RTL 的 package-import 前端兼容问题。这是工具链 preflight 不充分，而非 PPA 数值失败。
- **不是设计性能结论**：在 `abc`、`stat -liberty` 前停止，不能据此判断面积、时序或吞吐是否达标。

### 固定预防规则

任何新芯片设计阶段在允许 AI 优化前，必须先运行“最小端到端 baseline”：功能快检、目标工具读取、
目标约束/库加载、结果解析。baseline 失败时，controller 要把失败分类为功能、RTL 综合兼容、
harness 命令/依赖或预算超标，并路由到对应修复模式；只有得到真实结构化测量后才进入 PPA 优化。

## 2026-08-24：功能编译失败必须先于 PPA 修复

一次综合可用性修复后，`mla_kv_cache.sv` 出现 Verilator 编译错误（对 unpacked array 使用
`<= '0`）。这不是 PPA 超标，也不是 Yosys 错误；若 controller 只将 PPA feedback 交给下一轮
AI，会让它看不到真正的首要失败。

固定规则：每轮都把最新功能 feedback 和 PPA feedback 一起交给规划者。功能快检或 Verilator
编译失败时进入“功能/Verilator 可用性修复模式”，只允许修改 RTL，功能通过前不启动 PPA；只有
功能通过、PPA 失败时才进入“综合可用性修复模式”。

## 2026-08-24：完整 PPA 前需要限时综合预检

Yosys 在 `PROC_MUX` 展开大型 `key_mem/value_mem` 过程式数组时可消耗两小时、且尚未进入 ABC。
在这种状态下连续运行完整 PPA 会以高成本重复暴露同一 RTL lowering 问题。

固定规则：功能快检通过后先运行最长 600 秒、止于 `proc; opt; fsm; opt; memory; opt` 的 Yosys
综合预检。预检仅判断“RTL 能否通过前端和 memory lowering”，不产生正式 PPA；预检通过后才运行
完整 ABC 映射。修复 memory 架构期间使用 `--preflight-only`，避免每轮触发两小时完整测量。

## 2026-08-25：外层 timeout 与 SSH 退出码不能代表容器内任务状态

### 现象与根因

controller 显示 600 秒 Yosys 预检，但本地 controller 退出后，容器内 Yosys 仍持续运行超过两小时。
同时，多次功能回归在远端已经生成 `pass=true` 的 `summary.json`，本地仍因 SSH 断连得到 exit 255，
从而错误进入 RTL 功能修复模式。

根因有两层：主机上的 `timeout docker exec ...` 只能终止 docker 客户端，不保证终止容器内的孙进程；
SSH/rsync 的 255 是传输层状态，不是 Verilator、RTL 或 scorer 的功能结论。

### 固定预防规则

- timeout 必须在容器内部直接包住目标工具，并设置 TERM 后的 KILL 保护；退出码和完整日志先写入服务器
  挂载目录，本机只读取小型状态和日志尾部。
- 每个长任务在 run 目录写入受 run-id 约束的 PID 文件，使取消脚本可以精确终止该任务，而不停止容器。
- SSH/rsync 使用有限次数重试；若 SSH 断开但远端 `summary.json` 已明确通过，应恢复为成功。
- 无法恢复的传输错误统一使用 exit 75。controller 可自动重试，但不得把 75 路由成 RTL 修复，也不得
  让它消耗新的 AI 修改轮次。
- controller 异常退出但远端已有确定证据时，要补写结构化 `result.json`，避免下一次循环读取更旧的
  失败记录并选择错误模式。

# 第二部分：跨项目原则

本部分是未来搭建其他 harness 时可直接复用的制度。项目案例中的具体数字只作为证据，不构成新项目
的默认参数。

## 资源边界应限制成本，不应替 AI 规定搜索路径

### 问题

复杂端到端流程常出现单次工具运行很久才超时。若每轮固定使用最大 timeout，会重复消耗资源却只得到
“超时”；若把所有步骤固定为极短 timeout，又会把正常但较慢的步骤误判为失败。例如本项目成功的
Yosys lowering 预检实测约 90 秒，因此统一 60 秒并不合理。

### 可复用的解决方式

人定义最终端到端判据、可用资源、可接受的预算档位和证据标准；AI 根据真实日志选择具体的诊断和
测量策略。controller 执行而不让 AI 直接运行任意命令。

推荐预算阶梯：短探针（获得阶段/进展信息）→ 中等模块测量（获得可比较 PPA）→ 长预算重点模块或
顶层里程碑（端到端确认）。升级预算的依据必须是“额外时间会带来新的可验证证据”，而不是单纯等待。
分层结果帮助定位瓶颈，但不得被表述为最终顶层 PPA；任务结束前仍须回到完整功能回归和顶层验证。

## 停止 controller 时必须清理其本地子进程树

controller 可能正在等待 OMP、远程脚本或其他长命令。仅终止 controller 本身，会使子进程成为孤儿：
它既不再能把结果交回 controller，又可能继续消耗模型或本机资源，甚至在结束时写入工作区。

固定规则：controller 启动每个子命令时创建独立进程组；收到 `SIGINT`/`SIGTERM` 后先向该进程组
发送同一信号，再退出。服务器任务另由 run-id 精确取消脚本处理，避免为了停止当前任务而停止整台容器。

## 工具 exit 0 不等于端到端测量完整

分层综合为了在有限预算内产出证据，可能黑盒化重模块、只测部分叶模块，或只得到控制面面积。即使
Yosys/ABC 命令成功退出，这些结果也不能被路由为“完整 PPA 已成功”，否则 AI 会失去修改测量脚本的
权限，转而反复优化已测模块，却无法补齐未测模块、SRAM 容量或顶层关键路径。

固定规则：测量脚本必须显式输出结构化覆盖范围，例如 `PARTIAL_HIERARCHICAL` 或
`TOP_LEVEL_COMPLETE`。controller 根据覆盖范围而非仅根据退出码选择下一模式；只有无测量黑盒的真实
顶层闭包完成，才允许将任务转入纯 RTL PPA 优化或作最终预算结论。

## 循环内的远程命令不得意外消费调度输入

当 Bash 用 here-string、管道或重定向把任务列表送入 `while read` 循环时，循环内启动的 `ssh` 会继承
同一个标准输入。若没有使用 `ssh -n`（等价于 stdin 来自 `/dev/null`），第一次远程调用可能读走后续
全部任务行，表现为“第一个分区成功、整个循环 exit 0”，但其余分区从未运行。这类问题不能从工具
成功退出码发现，必须核对计划任务数与实际结果数。

固定规则：循环内不需要输入的 SSH 必须禁用 stdin；结构化 feedback 必须逐项列出完成、超时和因总预算
跳过的任务。周期性回归计数也必须先确认计数大于零，再判断取模条件，避免 `0 % N == 0` 导致尚无
有效候选时反复触发昂贵回归。

## 验证 harness 本身的调度闭环（元验证）

AI 能依据反馈定位设计或测量问题，但不能保证识别 controller 自己漏调度、漏解析或错误路由。一次
工具 `exit 0` 也不能证明任务计划被完整执行：例如配置了多个综合分区而实际只运行首个分区时，若
feedback 只报告首个成功结果，AI 可能合理但错误地把它当作正常的局部测量。

固定制度应分三层：AI 决定下一步策略；controller 执行受控命令并保存真实证据；harness 对 controller
的调度契约做元验证。后者至少要求：

- 从任务配置读取预期工作项；每个工作项在轮次结束时必须有 `completed`、`timeout` 或
  `skipped_budget` 之一的终态。
- 预期工作项数与实际终态数不一致、且不存在明确预算耗尽解释时，报告 `scheduler_error`，不得把
  本轮当作成功测量。
- 连续多轮没有新增覆盖、没有新的失败位置或没有新的真实指标时，报告“停滞”，要求下一轮说明为何
  重复同一策略有价值；它是决策提醒，不替 AI 强制选择具体优化方法。
- 为 controller 写不依赖 EDA 工具的模拟测试，覆盖多工作项、单项超时、总预算耗尽、SSH 断连和
  stdin 意外消费等调度情形。

本项目已修复 SSH 消费分区列表的根因，并加入预期分区数与实际终态数一致性检查：总预算耗尽后仍
逐项记录剩余分区为 `skipped_budget`，数量不一致时输出 `scheduler_error`。有效测量候选计数也跨
controller 启动恢复，防止把一次长实验拆成多批后丢失周期性回归节奏。不依赖 EDA 的完整模拟调度
测试仍应继续扩充。

## 测量日志解析必须区分“零、未观察到、重复层次汇总”

工具日志中的同一资源可能同时出现在叶模块局部统计和顶层“包含子模块”统计中；不同工具版本也可能
分别输出 `Number of memory bits` 或 `245760 memory bits`。直接 grep 固定文案会把真实数据误报为
“未测得”，直接累加所有匹配行又会重复计算。

固定规则：每个测量分区写入开始标记和统计完成标记；只有到达完成标记的分区才拥有有效容量结果。
同一分区内取层次统计的最大容量值一次，再对互不重叠的分区求和。没有 memory 但完成统计记为零；
超时前未到统计检查点记为 `unobserved`，两者不得混淆。解析器测试必须覆盖工具输出格式变化和层次
重复行。

## 远端工具超时之外，还需要 SSH 控制面墙钟和结果恢复

远端 Yosys 即使已按 180 秒预算结束并写出 exit code，已建立的 SSH 会话仍可能不返回；`ConnectTimeout`
只限制建连，不能限制失活会话。因此每个远端分区还必须有独立的本地 SSH 墙钟（工具预算加固定 grace）。
墙钟到期后，终止的只是本地 SSH 客户端；controller 随即用新的有界 SSH 读取远端已落盘的
`ppa_<partition>.exit_code`。读到数字则恢复真实工具结果，读不到则标记 exit 75，且绝不重发相同
分区命令，避免同一 run-id 产生重复综合。远端 shell 应写入 `container.pid`，使精确取消脚本也能匹配
`run=runs/<id>` 这类相对路径的工作进程。

## 模型或配额错误不是空计划，必须 fail-fast

若 planner/executor 报配额耗尽、明确模型 runtime/error 或任意非零 OMP exit，后续功能/PPA 验证没有
一份成功计划和可信实施可以对应。继续验证会浪费长时间工具预算，也会让 progress 伪装成一次 AI 主导的
迭代。固定规则是：planner 失败时跳过 executor 和全部验证；executor 失败时保存 `changes.diff` 证据并
回退受管输入半成品，再跳过全部验证。controller 记录 `agent_unavailable`，待模型恢复后从新回合重新规划。

## 同一不完整测量不能无证据缩短预算

固定预算阶梯不等于每轮都应回到最短探针。若 RTL、PPA 脚本、分区和黑盒配置相同，上一轮在 600 或
1800 秒仍未完成，降到 180 秒只会得到同一日志的更短前缀。固定规则：只有实际改变测量输入、并明确
说明新短探针验证的假设时才可降档；controller 对同一配置的无解释降档保持上一次预算，并把原因写入
`measurement_budget_source`。这仍保留 AI 选择分层、黑盒化或新探针的自主性，而不让它重复低信息实验。

## RTL 数据流改动的长序列回归必须由 controller 强制执行

任务卡即使要求数据流改动做 16/48-token 回归，若 controller 只按“每 N 个 PPA 候选”调度中等回归，
仍会出现 `p16_tensor_loader` 这类一拍时序改动仅通过 8-token 就进入 PPA 的漏洞。固定规则：任意 RTL
改动先强制 16-token；文件名命中 control/data-path 分类（loader、FSM、cache、arbiter、datapath、attention、
matvec 等）时，再强制 prompt-0/48-token。两项通过前不得运行预检/PPA；纯测量 harness 改动不触发长回归。

## 一个因果假设不等于一个文件：局部优化与架构实验应有不同验证节奏

“每轮只解决一个问题”若被机械实现为“只能改一个文件”，会阻止 AI 修复真实跨模块问题；若完全不限制
文件集合，又会把一次 PPA 变化归因给一堆无关修改。可复用的折中是让 AI 只提出**一个可证伪的因果
假设**，再由 controller 区分两种实验：

- `local`：至多一个 RTL 文件，允许只测与假设相关的已配置 PPA 分区，快速判断局部面积/时序方向；
- `architecture`：多个 RTL 文件作为一个原子变更集，必须写清文件、接口约束和功能不变量，随后强制
  完整功能回归与 full 配置分区测量。

局部测量不能无限连续使用。每两次有效 targeted 测量后自动插入一次 full 配置分区测量；缺少精确分区名、
改的是测量 harness、没有 RTL 改动或出现非法请求时安全回退为 full。这样人不替 AI 指定“该改哪个模块”，
而是定义实验可归因、成本受限、能周期性发现系统退化的自主决策制度。无黑盒的真实顶层闭包仍是最终判据。

同一原则也适用于功能门：所有 RTL 改动强制 16-token；影响 descriptor/cache、控制或数据通路的模块还
强制 prompt-0/48-token。文件名分类只是保守的风险门，不是对 AI 搜索策略的限制。

## 清理历史 run 与活动 controller 必须互斥

`runs/` 不只是日志目录：活动 controller 会在其中写 planner/executor 日志、RTL 快照和验证结果。若在
它运行时压缩或删除历史 run，可能删掉其正在使用的快照，或让下一轮读取到缺失的配置/反馈，从而把一次
维护操作伪装成 RTL 或综合故障。

固定规则：清理脚本先检测 `agent_loop.py` / `ppa_agent_loop.py`，发现活动 controller 时拒绝执行；先用
受 run-id 约束的取消流程停止当前回合，再归档。归档只保留 AI 的规划/实施记录、diff、结构化结果和每类
任务最新的紧凑 feedback；controller 必须显式支持从归档读取这些记录，随后才删除可再生的 RTL 快照、
构建目录和长工具日志。这样“节省磁盘”不会破坏可重启性和决策历史。

服务器的 `runs/` 是不可变证据库：**永不清理、永不归档替换、永不由本地清理脚本删除。**服务器保留
完整工具日志、构建产物和原始 run 目录，便于复盘；本机只保存紧凑策略档案以节省空间。清理脚本必须
硬编码为仅允许在本机工程路径执行，即使脚本同步到了服务器也必须拒绝运行。

## “没有 sta 命令”不等于容器没有 STA

有些 EDA 容器不安装独立的 `sta` / `opensta` 可执行文件，却安装了 OpenROAD；OpenROAD 内嵌的时序
引擎就是 OpenSTA。若 harness 只用 `command -v sta` 探测后端，会把可用的 STA 错误标为
`unavailable`，并让后续 AI 围绕错误前提反复修复。

固定诊断顺序是：先查独立 `sta` / `opensta`，再查 OpenROAD；使用 OpenROAD 时执行
`openroad -no_init`。对标准单元映射后的 Verilog，OpenROAD 必须先读取 technology LEF 和 macro LEF，
然后读取 Liberty、网表、`link_design`、时钟约束并运行 `report_checks`。只读 Liberty 后直接
`read_verilog` 会因没有 technology 而失败。后端可用的最小证据是一份已链接 mapped-netlist 的
register-to-register setup 报告；它仍只是 pre-layout 结果，存在黑盒或未链接单元时必须继续标记为
部分闭包，而不能当作最终芯片 STA。
## 2026-09-03：远端验证的 SSH 建连超时不等于执行超时

`ssh -o ConnectTimeout` 只限制建连；TCP 已建立后，`docker exec` 的输出流仍可能静默悬挂。远端功能/PPA
命令必须同时具备：容器内工具墙钟、客户端 SSH 墙钟、按唯一 run-id 落盘的 exit-code、用新连接恢复结果，且在
结果未知时绝不重发同一远端任务。只有确认旧 run 已结束或无记录时，才把错误标为 transport/infra（exit 75）并
让 controller 做有限重试。收到外部终止信号时 controller 应写 `cancelled`，避免残留 `running` 状态误导监控。

## 2026-09-03：重启 controller 应接管精确旧 run，而非盲目拒绝或并发重跑

本地 controller 被断网、终端关闭或杀掉后，服务器的功能验证可能仍在运行。新 controller 不能仅相信本地
进程表，也不能直接启动替代验证；它应在任何 AI 规划前读取服务器 `status/current.status`，只接受受限字符集的
精确 `run_id`，并只终止该 run 的 `container.pid` 或命令行中精确匹配该 id 的进程。即使状态已写为 `cancelled`，
仍要确认 PID 已退出。先 `TERM`、短暂等待、必要时
`KILL`，并重新确认不存在该 run 的 PID，才写 `state=cancelled` 并继续新 controller。若 SSH/Docker/PID 查询
任一环节无法证明旧任务已停止，应返回 `infra_uncertain`（exit 76）并保留现场，绝不能猜测成功或启动并发 run。
服务器 `runs/` 始终保留，接管只处理进程和状态。

此外，远端包装脚本在本机启动、服务器容器执行的架构中，环境解析程序实际由**服务器宿主机** Python 执行。
即使容器是 Python 3.12，宿主机仍可能是 Python 3.6；因此 `environment.py` 必须避免 `from __future__ import
annotations` 等较新语法。兼容性检查应覆盖宿主机的 `python3 harness/environment.py --shell`，不要只在本机或容器
中做语法检查。

## 2026-09-03：上层模型要接收证据包，不要重复整份长进度

Pro 的职责是在 Flash 批次边界选择继续、修卡或修 harness，并不需要每轮重读完整任务卡、数万字符的
`progress.md` 尾部和原始工具日志。把它们直接拼进 prompt 会放大 token-per-minute/上下文配额压力；一旦
模型服务返回 429，监督器既不能继续验证也不能安全修复。可复用的做法是传递有长度上限的证据包：稳定任务
硬边界摘要、最近 `result.json` 的结构化字段、筛选后的 PPA feedback facts、少量最新进展摘录，以及精简的
治理计数。完整原始证据仍写入 runs/progress，供人或下一次有针对性的诊断读取。受限实施 prompt 的 rationale
也应限长。这样减少冗余输入而不删改证据；它只能降低 429 概率，不能替代服务端的账户/模型配额管理。

## 2026-09-03：模型决策 JSON 不应与回显 prompt 共用解析日志

许多 CLI 会把调用 prompt 记录到与模型回复相同的文件，而 prompt 往往包含“请输出如下 JSON”的模板。若父进程
在启动子进程前没有 flush 日志缓冲，模板甚至可能在模型回复之后才落盘；“取最后一个 fenced JSON”的解析器会把
`action: "continue|..."` 这类模板当成真实决定并安全停止。固定规则是：模型日志只保存模型原始回复，不重复
记录完整 prompt；写日志头后立刻 flush；解析时从后向前选择第一个通过 action/字段校验的 JSON，并跳过不合法
模板。这样既保留可观察的模型输出，也不会把日志格式伪影变成控制流输入。

## 2026-09-03：规划→执行之间的 plan-value/预算字段必须端到端机器可读

AI 规划者输出的计划值（`change_mode` / `measurement_mode` / `affected_partitions` /
`measurement_budget_seconds` 等）若以模板自带的反引号包裹（如 `` `measurement_budget_seconds: 3600` ``），
而 controller 的解析正则只匹配裸行（`^measurement_budget_seconds:\s*(\d+)$`），整行会失配并静默落入默认
分支。结果就是规划期授予的 3600s full 计划从未到达执行器，`result.json` 却记成
`measurement_timeout_seconds=180`、`measurement_scope=UNKNOWN`、`measurement_budget_source=default_no_valid_plan_value`、
`ppa_exit=1`，PPA 结构化事实为空——表面像“测量失败”，实为计划值/预算握手断裂，与 RTL 或测量脚本无关。

适用条件：任何由 AI 输出机读指令、再由 controller 用正则或结构字段解析并执行的 harness。只要 prompt 中展示的
指令模板与解析器的格式不一致（反引号、缩进、冒号空格、大小写、尾随空白），规划值就会被静默丢弃并回退默认值。

固定预防规则：
- 计划值/预算字段必须端到端单向机器可读：prompt 里展示的指令模板与 controller 的解析正则必须逐字对齐；解析器
  对模板自带的反引号和前后空白做容错（`^`?\s*field:\s*(...)\s*`?\s*$`），同时接受裸行与包裹行两种合法形式。
- 每个计划值字段都要有显式的“无有效值”哨兵来源（如 `default_no_valid_plan_value` 与
  `controller_full_measurement_floor`），使一次被丢弃的计划在结果记录中可观测、可归因，而不是伪装成测量失败或
  设计缺陷。
- 用真实规划输出（含其格式伪影）回放进解析器做回归：断言授予的档位真实落到执行配置，而不是只断言字段存在。
  这样“AI 声称授予了什么”与“controller 实际执行了什么”之间不允许有默认值抹平。

## 2026-09-03：下游工具读取生成产物必须按本轮写标记，不能 glob 持久目录

证据：Task 02 的 OpenSTA 回退链对 `build/ppa_mapped_*.v` 做全量 glob，而 `build/` 是跨轮次持久的共享目录；
预算跳过或 `exit 124`（未执行到 `write_verilog`）的分区会留下上一轮旧网表。结果是三个互不相关的映射网表
（`ppa_mapped_cp_fsm.v:324`、`ppa_mapped_decay_from_sigmoid_mod.v:142783`、`ppa_mapped_kda_delta_head.v:23`）
被 OpenSTA 以 STA-0171 语法错误拒绝，`link_design`/`report_checks` 无法跑通，`sta_crit_path_ps` 全部未测得；
其中 `kda_delta_head` 记 0 memory bits，反证“单一 ROM-init 构造”这一前提覆盖不了全部失败，属读取输入范围缺陷
而非单一 RTL 构造。功能快检 `exit 0` 且 `rtl_changed_paths` 为空，进一步确认是 harness 缺陷。

适用条件：任何“上游生成产物 → 下游解析/链接工具”的流水线，只要产物落在跨轮持久目录、下游用 glob 或目录枚举
读取、且上游可能因超时/预算/失败跳过部分产物，就会出现“旧产物被当作本轮结果”的读取污染。多分区综合、生成
网表、报告/覆盖率文件、代码生成器产物都属于这一类。

固定预防规则：
- 上游每成功写出一份产物，就在结构化日志中落一条“写标记”（含文件名/分区标签，如 `MAPPED_NETLIST_WRITTEN=<tag>`）；
  下游只读本轮写标记对应的文件，绝不 `glob` 持久目录，也绝不信任上一轮遗留文件。
- 每条解析失败必须记录完整上下文——文件路径、行号、错误文本，且是**全部**失败文件而非 `head -N` 截断的前几个；
  否则第三及之后的失败会被静默丢弃，使“残留旧产物”与“真实生成缺陷”无法区分。
- 写标记用受控字符集（`[A-Za-z0-9_.-]`）并显式剥离日志引号/空格伪影，避免日志回显格式（如双引号包裹）让
  下游解析失配、静默回退到全量读取。

## 2026-09-04：Yosys write_verilog 在 select -clear 后只写黑盒桩，全设计写网表前必须 select *

证据：Task 02 Round-55 全量 PPA 中，`build/ppa_mapped_cp_arbiter.v`、`ppa_mapped_silu_pwl.v`、
`ppa_mapped_l2norm_q.v`、`ppa_mapped_moe_shared_top2_contract.v` 等所有 `blackbox=""` 分区网表都只有一行
Yosys 头注释（0 个真实 module body）；有 `blackbox` 列表的分区（`cp_fsm` / `q4_matvec_128x64`）只写出
`-blackboxes` 生成的空桩，同样没有真实 top body。OpenSTA 日志给出稳定签名：
`[WARNING STA-0198] module mem_arbiter4 not found ... [ERROR ORD-2013] instance arb LEF master mem_arbiter4
not found`，即单文件读入成功（`sta_read_ok_count=1`）但 `link_design` 把实例当 LEF 宏查找。根因是
`ppa_measure.ys` 在 `memory_map` 前执行 `select -clear`（清空选择集），随后直接 `write_verilog`：该 Yosys
构建的 `write_verilog` 即使不带 `-selected` 也按当前选择集写模块，清空后只剩 `-blackboxes` 强制写出的空桩。
分区 area/delay 统计仍正常（`stat -liberty` 不依赖选择集），所以缺陷只在“写网表”这一步，属 harness 装配缺陷，
不是 RTL 或测量范围问题。

适用条件：任何“Yosys 分区映射后写全设计网表，再交给 OpenSTA/OpenROAD 单文件 link”的流水线；尤其是写网表前
为了 `memory_map` 之类 pass 执行过 `select -clear` 或 `select <top>` 的流程，以及后续依赖该网表包含真实
module body 的 link/解析步骤。

固定预防规则：
- 全设计网表写入前必须显式恢复“选中全部”：`select *`，并用 `write_verilog -selected -blackboxes` 明确
  “写选中模块 + 黑盒空桩”两件事；不要把 `write_verilog` 不带 `-selected` 当作“写全部”的保证。
- 验证“写网表”成功的判据不能只是文件存在或 exit 0，必须 grep 到至少一个非空 `module <top> (...) ... endmodule`
  body；只有头注释或只有黑盒桩都算失败。
- 单文件装配（`cat ppa_mapped_*.v ppa_closure_top.v`）时，被 link 的顶层引用的每个非黑盒实例，其 body 必须能在
  装配结果中直接 grep 到；否则 link 阶段会把它降级为 LEF 宏查找（ORD-2013）。

## 2026-09-04：写全设计不能只靠 `select *`，必须显式重选并保留 blackbox 标记

证据：Round-55 已证明 `select -clear` 后该 Yosys 构建的 `write_verilog` 跟随当前选区，空选区只写头注释
和 `-blackboxes` 空桩。Round-56/57 据此补 `select *`，但 Round-57 的
`runs/02_yosys_ppa--20260904-065616--47494/feedback.txt` 在 6/6 分区 `exit=0`、mem_arbiter4 面积已落盘
（1557.7 µm²）时仍报 `[ERROR ORD-2013] instance arb LEF master mem_arbiter4 not found`，说明 `select *`
单独不足以把真实 body 写进装配网表；Round-58 又删掉 `select *` 与 `-selected`，退回到空写。

适用条件：任何“分区映射后写全设计网表 → OpenSTA/OpenROAD 单文件 link”的流水线，尤其写前为
`memory_map` 之类 pass 执行过 `select -clear`，或所用 `write_verilog` 版本跟随选区而非默认写全设计。

固定预防规则：
- 全设计写前不要依赖 `select *`；用与综合 pass 相同、已证可用的显式选区恢复“全分区”：
  `select <top> %n`（所有叶）再 `select -add <top>`（补顶）。
- `write_verilog -blackboxes` 必须保留空桩上的 `(* blackbox *)` 标记；不要叠加 `-noattr` 把它剥掉，
  否则空桩会被 link 阶段当作缺 LEF 的叶子再报 ORD-2013。
- “写网表成功”的判据是 grep 到非空 `module <top>` body；`stat -liberty` 的 area 行、`exit 0`、文件存在
  都不等价。
- 下游装配后加 per-module 存在性检查（如 `STA_BODY_CHECK=<module>=present|missing`），把裸 ORD-2013
  变成可归因的“哪个 body 缺”。
## 2026-09-04：测量终态判据全满足后仍存 link_error 时，归因黑盒 LEF 缺失而非残余配置缺口

证据：Task 02 的 `runs/agent-02_yosys_ppa--20260904-063957--attempt1/result.json` 记录 PPA 测量
`exit 0`、`scope=PARTIAL_HIERARCHICAL`；同轮结构化 feedback 事实为 `observed_partition_terminals=6`、
6 分区 `exit=0`、`logic_area_mm2=1.9739`、`critical_path_ps=4579.64`、`sram_total_bits=245760`，且
`sta_status=link_error`。这是 closure STA 首次在干净（非超时）轮落盘；在 6 分区均已写出真实 module body
的前提下 link_error 仍存，本轮最强解释为 SRAM 黑盒（网表中的 blackbox 实例没有对应 LEF 宏）固有，
而非仍有分区未写 body 或选区未恢复的配置缺口。

适用条件：任何“分区映射/测量 → 装配网表 → STA link”流水线中，出现“测量自身声明的全部终态判据已满足
（分区 terminal 计数、逐区 exit、面积、时序、容量事实均落盘），但 closure link 仍报 link_error”的情形。

固定预防规则：
- 把“测量完整性”（本轮声明的终态判据是否全部满足）与“closure/link 完整性”（装配网表能否 link 全设计）
  记录为两个独立状态字段；不得因 `link_error` 反推“测量尚未完成”，也不得因测量 `exit 0` 就宣布 closure
  完成。
- 当 link_error 在“所有分区均含真实 body、且面积/时序/容量事实齐全”后仍存在时，先核对该 error 命中的
  实例是否被显式声明为 blackbox（如 SRAM）且无对应 LEF 宏；若是，归类为“设计级黑盒/宏缺失”，停止把它
  当作残余 harness 配置缺陷继续改写网表或选区。
- 终态判据必须机器可核对（分区 terminal 计数、逐区 exit、关键指标事实），使“判据全满足”本身可观测；
  结论的归因（黑盒固有 vs 配置缺口）要标注为“最强解释”并列出支持/排除它的证据，而不是当作已证实事实。


## 2026-09-07：远端终态产物优先于瞬时传输分类；已落地未验证的 harness 改动可 targeted 重验

证据：Task 02 `runs/agent-02_yosys_ppa--20260907-090016--attempt1/result.json` 记录
`functional_medium_exit=76`、`functional_counting_cycles=null`，但同轮
`runs/02_yosys_ppa_functional--20260907-090630--p1--prefix16--87363/feedback.txt` 已落盘
`"pass": true`、`"simulation_exit": 0` 与 `PASS cycles=274906`；其 `remote_console.log` 显示 SSH 主连接
`Timeout, server ... not responding`。即远端功能运行实际已完成并成功，但恢复循环在 180 s 窗口内因 SSH
短暂不可达而未读到 `functional.exit_code`/summary.json，先返回 exit 76，随后 feedback 抓取又成功，形成
“真值已落盘却报 76”的假阴性。同轮 `result.json` 还记录 `rtl_changed_paths=[]` 与
`measurement_plan_mode=functional_counting`，而 planner 已声明 `measurement_mode: targeted` +
`affected_partitions: top_level_sta` 重验已落地的 mem_arbiter4 装配修复，controller 却把它路由到计数点、
未运行任何 PPA/STA。

适用条件：
- 任何“本地脚本经 SSH 启动远端长任务，任务可能因墙钟/保活超时断开”的验证管道，只要远端有落盘的
  终态产物（summary/finish 标记/exit_code 文件）。
- 任何“只读规划者请求重验一个已在先前轮落地、但尚未得到 PPA/STA 验证的 harness/测量改动”的闭环
  调度器。

固定预防规则：
- 传输层分类（exit 75/76）只对“没有 durable 终态产物可读”成立；给结论前必须先抓取 durable 产物，
  产物能证明终态成功时覆盖瞬时传输分类，绝不把已完成成功的运行报成 76。
- 终态成功判据用两个独立字段合取（如 summary 的 `pass=true` 与 `simulation_exit=0`），避免把局部
  日志里的 `pass`（score.log 也可能含 `pass=true`）误当成整体成功。
- 调度器把“无本轮文件改动”路由到功能计数点的前提是“本轮确无 PPA 假设”；若规划者显式给出合法的
  targeted 分区重验，属于具体 PPA 假设，必须走单网表 smoke/targeted，而不是静默降级为计数点。

## 2026-09-07：顶层 STA 单网表 smoke 的 closure 网表必须自包含，否则 link 降级为 LEF 宏查找

证据：Task 02 `runs/agent-02_yosys_ppa--20260907-095617--attempt1/result.json` 记录
`ppa_exit=0`、`measurement_scope=PARTIAL_HIERARCHICAL`、`measurement_partitions=["top_level_sta"]`；同轮
`runs/02_yosys_ppa--20260907-101034--91178/feedback.txt` 记录 `partition top_level_sta exit=0` 且
`sta_status=link_error`（`[ERROR ORD-2013] instance arb LEF master mem_arbiter4 not found`），
`sta_body_check` 六项全 `missing`。该轮只跑 top_level_sta 单分区：closure 网表只写出顶层
`nano_golden_decoder_top` 自身 glue，其五个控制平面子模块（mem_arbiter4、nano_golden_datapath_fsm、
image_header_reader、logit_writer、nano_golden_microop_controller）的 mapped body 全不在装配网表中，
link_design 把第一个未定义实例降级为 LEF 宏查找。同时旧的 `grep "module <name> "`（固定尾随空格）漏判
`module <name>(`、转义 `module \<name>`、`_paramod_*_<name>` 三种真实声明形式，把"body 存在"误报 missing。

适用条件：任何"顶层/closure 网表 + 单网表 targeted smoke + link_design"流水线，且顶层直接实例化的子模块
body 依赖其它分区单独产出；只跑 closure 分区时这些 body 天然缺失。

固定预防规则：
- 用于 link 的 closure/顶层网表必须自包含：在 closure 流程里把被顶层直接实例化、且非黑盒的子模块 body
  一并映射并写入同一网表；否则单网表 smoke 永远无法 link，只能得到误导性的 ORD-2013。
- body 存在性检查必须按网表真实标识符形式匹配（`module <name>(` / `module \<name>` / `_paramod_*_<name>`），
  不得用 `grep "module <name> "` 这类带固定尾随空格的模式。
- 单网表 smoke 的 link 失败先归因"closure 网表是否自包含"，再考虑黑盒 LEF；没有 body 证据时不要把残余
  link_error 归为设计级黑盒缺失。

## 2026-09-07：Yosys `write_verilog -selected` 不可作为自包含多模块网表的写出口径

证据：Task 02 `runs/agent-02_yosys_ppa--20260907-191613--attempt1` 的 Round-63 双写修复（pass1 叶选区写
`ppa_closure_leaves.v`、pass2 top 选区写 `ppa_closure_top.v`）后，`runs/02_yosys_ppa--20260907-194051--11815/feedback.txt`
仍记录 `sta_status=link_error`（本轮错误已从 ORD-2013 变为 STA-1390，因整个装配网表为空）、
`sta_body_check` 六项全 `missing`，且 `STA_NORM_SRC_LINES: 1`（两份 closure 文件各只有一行 Yosys 头注释）。
同一 `yosys.log` 尾部显示两次 `write_verilog -selected -blackboxes` 之间没有任何 `Dumping module` 行，而
紧邻其前的 `stat -liberty` 已证明各模块 body 在内存中映射完成。即：`-selected` 空写不是“未重新选区”造成的
（Round-63 已在写前显式 `select <top> %n` / `select <top>`），而是该 Yosys 0.64 构建的
`write_verilog -selected` 只写“整模块全选”的模块，techmap/abc/clean 之后留下的选区不满足这一要求。

适用条件：任何“Yosys 按模块分层 techmap/abc 映射 → 写自包含网表 → OpenSTA/OpenROAD 单文件
`link_design`”的流水线；尤其写前经过 `abc`/`clean`、需要把多模块真实 body 与黑盒桩写进同一文件的场景。

固定预防规则：
- 不要依赖 `write_verilog -selected` 携带 abc/clean 之后的真实 body；它只写“整模块全选”的模块，而
  techmap/abc 之后的选区不是可信的全选状态。
- 自包含网表优先用单次全设计写：先 `hierarchy -check -top <top>` 剪掉 techmap 引入但未被实例化的
  helper/wrapper 模块（ABC 的 BLIF 提取会把被映射模块拍平，这些 helper 已不可达），再 `memory_map`
  把 $mem ROM 转成结构逻辑，最后 `write_verilog -blackboxes`（不带 `-selected`）一次写出全部可达模块
  与黑盒桩；`-blackboxes` 保留 `(* blackbox *)` 标记（不要叠加 `-noattr`），使空桩仍是可 link 的时序黑盒
  而非缺 LEF 的叶子。
- 写网表成功的判据不是文件存在或 exit 0，而是 grep 到非空 `module <top>` body，且每个被 link 的非黑盒
  实例 body 都能在装配结果中直接匹配到（`module <name>(` / `module \<name>` / `_paramod_*_<name>`）。

## 2026-09-07：注册标准单元不足以闭合自包含网表——`hierarchy -top` 剪枝才是 body 丢失根因

证据：Task 02 `runs/02_yosys_ppa--20260907-230157--18267/feedback.txt` 在 Round-67 加入
`read_liberty -lib <lib>` 后，`STA_NORM_SRC_LINES` 由 874 增至 3108（Nangate45 标准单元确被注册并写出），
但 `STA_BODY_CHECK` 仍六项全 `missing`、`sta_closure_top_name=swiglu_expert`（link 回退到最后一个黑盒桩）。
即“注册 cell 模块”只是让 cell 定义落盘，并没有让六个控制面真实 body 落盘；body 丢失发生在写前的
`hierarchy -top <top>`（Round-65 带 `-check` 会因映射后的标准单元中止；Round-66/67 去掉 `-check`
后仍把 body 剪掉），与 cell 是否注册无关。本条修正上一条（Round-63/64）推荐的写出口径。

适用条件：任何“Yosys 分层 techmap/abc 映射后，把多模块自包含网表（真实 body + 黑盒桩 + 标准单元）
写进单一文件供 OpenSTA/OpenROAD `link_design`”的流水线；尤其是写前为了剪 helper 而调用
`hierarchy -top`、并在其后用 `select *` 或裸 `write_verilog`（不带 `-selected`）的流程。

固定预防规则：
- 已通过 `stat -liberty` 证明 body 在内存中映射完成，而写出的网表仍缺 body 时，优先怀疑写前会
  删除模块的 `hierarchy -top`/`flatten`，而不是“标准单元未注册”；`read_liberty -lib` 只能补齐 cell
  模块定义，不能挽回被剪枝的 body。
- 自包含网表写出口径退回 NORMAL 流已证可用的显式重选：`select <top> %n`（所有叶）再
  `select -add <top>`（补顶），最后单次 `write_verilog -selected -blackboxes`；`memory_map` 仍需先
  `select -clear` 覆盖全设计。未实例化的 techmap helper 落入网表是允许的（link 阶段无害）。
- 写成功的判据是 per-module `STA_BODY_CHECK=<module>=present`（按真实标识符形式匹配），不是
  行数增长、文件存在或 exit 0；行数增长可能只反映 cell/桩落盘，与真实 body 无关。

## 2026-09-08：STA link 目标解析不得回退到最后声明的黑盒桩

证据：Task 02 `runs/02_yosys_ppa--20260907-230157--18267/feedback.txt` 在 closure 网表
`STA_BODY_CHECK` 六项全 `missing` 时，记录 `sta_closure_top_name=swiglu_expert`、
`sta_status=link_error`、`sta_link_result=未测得`、`sta_crit_path_ps=未测得`。OpenSTA tcl 的顶层解析
顺序是“精确名 → `_paramod_*_<top>` → 最后声明的模块”；前两者未命中时，最后声明的模块恰好是计算叶
黑盒桩 `swiglu_expert`，于是 `link_design swiglu_expert` 成功，随后 `create_clock [get_ports clk]` 在
无 clk 端口的桩上未捕获失败，整轮被误报为 `link_error`，真正根因（closure 顶层 body 未落盘）被掩盖成
一条 ORD-2056“某 liberty cell 缺 LEF master”的无关警告。

适用条件：任何“归一化/拼接网表 + 从网表推断 link_design 顶层名 + 单文件 STA 闭合”的流水线；尤其
推断逻辑带“找不到就用最后声明模块”兜底、而网表同时包含黑盒桩的场合。

固定预防规则：
- link 目标解析只允许两个合法结果：命中的真实顶层（精确名或已知的 paramod 前缀形式），或显式的
  “未找到”错误。禁止“回退到最后声明的模块”——那很可能是无端口黑盒桩，link 会以看似成功的姿态
  推进，再在 `create_clock`/`get_ports` 上以无关错误崩溃，掩盖顶层 body 缺失。
- `link_design` 之后到 `report_checks` 的整段时序设置（create_clock/set_input_delay/
  set_output_delay/report_checks）必须与 link_design 一样包在 catch 里：任何失败都输出
  `STA_LINK_RESULT=error` 并 exit 3，不得让 Tcl 未捕获中止产生无法定位的 link_error。
- 反馈必须显式区分“已实测”与“未测得”：新增 `sta_timing_measured` / `STA_CLOSURE_TOP_FOUND`
  字段，使下游（Pro/controller）能直接从结构化 feedback 判断链路断在哪一环，而不是从错误堆栈
  或逐轮 progress 反推。

## 2026-09-08：STA 顶层解析应兼容 escaped identifier，但必须先证明 closure 网表非空

证据：Task 02 的本次 `top_level_sta` smoke 中，Yosys 日志确实以 `\\nano_golden_decoder_top` 形式
显示设计标识符，故 Tcl 解析应兼容 Verilog escaped identifier。不过 smoke 实际写出的
`build/ppa_closure_top.v` 和归一化文件均只有 121 bytes、仅含 Yosys 头注释，没有任何 `module`；
因此 `STA_CLOSURE_TOP_FOUND=0` 在该轮是正确诊断，不能把它归因于转义名解析或 OpenROAD。

固定规则：从 `module` 行提取第一个非空白、非 `(` token；若 token 的首字符是 Verilog escape
`\\`，在比较及 `link_design` 前去掉该 escape。只允许精确的当前工程配置顶层，或已知
`_paramod_*_<top>` 形式；仍然禁止回退到最后声明模块。更高优先级的前置判据是 closure 网表必须含有
非空顶层 body：若没有任何 `module`，直接报告 closure export 失败，先修 `write_verilog` 选区/导出，
不得继续把问题归到 STA 后端。

## 2026-09-08：Yosys selection token 与 `write_verilog -blackboxes` 必须以当前版本 help 为准

证据：容器中的 Yosys 0.64 `help select` 明确 `%n` 为 selection inversion，而不是层次叶模块展开；
`help write_verilog` 明确 `-blackboxes` 为“only write blackbox modules”。本次 top-level STA smoke 的
`select -list` 仍可见已经 ABC 映射的控制面单元，但 `write_verilog -selected -blackboxes` 写出的
closure 文件仅 121 bytes、没有 `Dumping module`。这将“综合已有 body、导出却为空”归因为 harness 的
selection/export 语义误用，而非 RTL 或 OpenROAD。

固定规则：要导出可供 STA link 的真实 closure，先从配置顶层建立完整模块选择；用 `%s` 递归展开
“被所选单元实现的模块”，每次与已有模块集并集，直到受控深度收敛，再以 `write_verilog -selected`
导出完整模块。不要带 `-blackboxes`；需要的计算/SRAM timing blackbox 由独立、可审计的桩文件补充。
每次改动后必须检查 `Dumping module`、顶层 `module` body 和 `STA_BODY_CHECK`，三者缺一即不是 STA
闭合成功。

## 2026-09-08：STA 读入 closure 前须预声明参数化计算黑盒

证据：closure 导出恢复后，OpenROAD 正确识别 `nano_golden_decoder_top`，但在第一个
`rmsnorm_q #(...)` 实例处报 STA-0171。计算模块原始黑盒文件是 SystemVerilog，且原流程没有把它并入
STA 读入网表；控制面补桩器也只覆盖控制面类型。用同一容器 Yosys 执行
`read_verilog -sv -lib <bb>; write_verilog -blackboxes <bb-export>` 可得到带正确端口的标准 Verilog
黑盒声明。

固定规则：对“mapped closure + timing blackbox” STA 流程，先由综合前端把原始 SV 黑盒源重导出为工具可读
的 Verilog blackbox-only 文件，再把该文件置于 closure 正文之前交给 STA。不要靠事后追加少数空桩；
参数化实例在解析时就需要模块声明，且桩的端口必须与实例匹配。

## 2026-09-08：黑盒重导出后应剥离 closure 实例的参数覆盖，并延迟补桩决定

证据：Yosys 导出的 blackbox-only Verilog 保留正确端口，却省略 parameter declaration；OpenROAD 因此在
`rmsnorm_q #(...)` 处报 STA-0171，尽管模块声明已置于文件前部。另一个补桩器在扫描到引用时立即记录候选，
即使相同模块 body 在文件后部出现，EOF 仍会生成重复声明。

固定规则：mapped closure 的端口宽度已具体化时，针对已导出的 timing-blackbox 类型删除实例 `#(...)`，保留
端口连接；补桩候选必须在完整文件扫描后再与最终定义集比较，只为真正未定义的类型输出桩。两项检查都应在
`read_verilog` 前完成，并作为 STA 输入的结构化审计项。

## 2026-09-08：端到端 STA 闭合必须由确定性 phase gate 驱动，而不是依赖模型从摘要猜测

证据：Task 02 的 feedback 已具备 `sta_status`、read/link/top/timing 等零散字段，但 Pro 输入为控制
429 而压缩后，无法稳定区分“closure 导出为空”“顶层缺失”“黑盒未声明”“STA 语法错误”和“link 失败”。
单凭 `Yosys exit=0`、`TOP_LEVEL_COMPLETE` 名称或 `PARTIAL_HIERARCHICAL` 都不足以表明真正运行过
`report_checks`。因此在 `harness/sta_closure_audit.py` 中将同一份真实 feedback 映射为小型、可测试的
phase，并由 controller 强制选择最小 `top_level_sta` smoke。

固定规则：
- 审计至少携带 closure bytes/module count、top found、STA read skip、link result、timing measured 和
  一条限长错误签名；原始网表和完整日志只留在服务器 runs，不为审计回传大文件。
- `closure_export_empty`、`closure_top_missing`、`closure_body_missing`、`undeclared_blackbox`、
  `sta_parse_error`、`sta_link_error`、`sta_no_timing_path` 是前置闭合失败。此时 controller 禁止把
  通用 full PPA 里程碑或功能计数当作下一步，固定跑 `top_level_sta`/600 s 以验证 phase 迁移。
- 两次定向 smoke phase 未变时停在可调试状态；只有 `sta_timing_measured` 才解除闭合门。若有 timing
  blackbox，它仍是 PARTIAL_HIERARCHICAL，不得升级为无黑盒全芯片 STA。
- skill 可以教 agent 如何解释这些 phase；真正不可绕过的约束必须在 audit 脚本与 controller 中实现。

## 2026-09-08：断连恢复先读取精确 run 的终态文件，不能把已完成任务当作待取消任务

证据：一次 `top_level_sta` smoke 的本地 SSH 父进程提前结束，但容器继续运行并已写出
`runs/<run_id>/exit_code=0`。旧 `remote_reconcile.sh` 只看 `status/current.status=running`，随后按 PID
路径进入终止分支，会把这个已完成 run 误记为 `cancelled`。修复后 reconcile 先验证 status 中的精确
run-id，再只读取同一容器路径下的 `exit_code`；存在合法数字即写回 `passed`/`failed`，不再查杀 PID。

固定规则：恢复逻辑的终态优先级应为“同一 run-id 的已落盘 exit code”高于“状态文件中的 running”高于
“进程扫描”。不得通过目录最新时间、模糊命令行或另一 run 的状态推断终态；只有 exact run 没有终态且仍有
精确 PID 证据时，才允许发出终止信号。这样网络中断最多留下待 reconcile 的状态，而不会破坏已完成测量的
证据语义。


## 2026-09-08：下游工具错误不能只用“错误码”分类，必须同时读“错误文本”

证据：Task 02 本轮 `top_level_sta` smoke 的 feedback 记录
`sta_error=[ERROR STA-0171] build/sta_norm/ppa_sta_all.v line 6716012, syntax error`、
`sta_status=read_error`、`sta_read_skip_count=1`，而 `sta_closure_audit.py` 的旧分类把
`"sta-0171" in error` 一律判为 `undeclared_blackbox`（未声明黑盒）。实际上 STA-0171 是
OpenSTA/OpenROAD 的通用“读入失败”错误码，同时覆盖“Verilog 语法错误”（`syntax error`）与
“模块未声明/黑盒缺失”（`unknown module` / `blackbox`）两类完全不同的根因。把通用错误码直接映射到
特定修复方向，会误导上层：本轮真实根因是 closure 网表语法错误，却被指向“补黑盒桩”。

适用条件：任何“下游工具输出错误码 → 上层分类器据此选择修复方向”的流水线；只要该错误码是通用码
（一个码覆盖多种根因，如解析器的读错误、链接器的未解析符号），仅凭码值就无法确定根因。

固定预防规则：
- 分类器必须同时检查错误码与错误文本的语义内容：只有文本出现 `unknown module` / `blackbox` 之类
  才算“未声明黑盒”；`syntax error` / `parse error` 归入“网表解析失败”，二者必须分开并给出不同修复方向。
- 不得用 `error_code in text` 这类裸码子串匹配来判 phase；通用码出现并不携带语义，必须由文本决定。
- 为“码相同、文本不同”的两种结果写回归测试，断言它们落入不同 phase，防止将来又把通用码当作特定根因。

## 2026-09-08：按空白分词做标识符归一化必须锚定 token 开头，避免改写带引号的字符串字面量

证据：`sta_paramod_norm.awk` 原以 `index(tok, "$paramod$")` 判断 token 是否含目标模式，再 `gsub`
把非 `[A-Za-z0-9_]` 字符改成下划线。Yosys 对黑盒重派生实例同时输出
`(* reprocess_after = "$paramod$<hash>\\mla_attention_head" *)`（源 `build/ppa_closure_top.v:6715243`）；
该双引号字符串值同样含 `$paramod$`，于是连引号带内部 `$`/`\` 一起被改写成裸标识符
`(* reprocess_after = __paramod_<hash>__mla_attention_head_ *)`（归一化后
`build/sta_norm/ppa_sta_all.v:6716012`），OpenSTA 报 `STA-0171 ... syntax error`。同一文件含
`$paramod$` 的行仅 6 处：4 处是须归一化的转义标识符（模块定义/实例），2 处是须原样保留的字符串属性值。

适用条件：任何按空白分词 + 字符串替换来归一化生成网表/源码的流水线，且目标模式也可能作为内容出现在
双引号字符串字面量中（属性值、字符串参数、宏文本等）。

固定预防规则：标识符改写必须锚定 token 起始语法形态（转义标识符以 `\` 开头、字符串字面量以 `"` 开头），
用前缀正则 `tok ~ /^\\?\$paramod\$/`，而不是 `index(tok, pat) > 0` 或 `tok ~ /pat/` 这类子串匹配；
归一化后再对“应改写标识符数 vs 应保留字符串数”做分类核对，防止字符串被误当成标识符。

## 2026-09-09：已实施 harness 改动必须是 controller 状态，不是只有内容哈希的隐式事实

### 现象与根因

人工已在目标 OpenROAD 容器探测并修复 STA Tcl 接口后，Pro 的紧凑证据包只包含 `scripts` 内容哈希和旧的
`feedback` 错误。哈希能使实验指纹变化，却不能表达“哪个改动已实施、为何正确、下一步只能验证”；Pro 因而
重复选择 `repair_harness`，有把正确改动再次改错的风险。另一个相关缺口是 `stop.request` 只写进 prompt，
还会额外消耗一次 Pro 调用等待模型输出停止决定。

### 固定预防规则

- 把人工或 agent 已实施的测量/harness 改动登记为持久化 `pending_verification`：至少包含受限文件路径、
  简明语义摘要、目标工具探测证据和预期 phase 迁移。
- pending 状态存在时，controller（不是 prompt）强制一次最小验证；对 STA 闭合修复即唯一
  `top_level_sta/600s` smoke。Pro 的任何再次 `repair_harness` 决定都必须被覆盖为该验证。
- 仅在该验证产生新的 result/feedback 后清除 pending 状态；验证失败才允许 Pro 根据新证据继续修复。
- `stop.request` 必须在每个批次边界、下一次模型调用之前由 supervisor 直接终止；不得把停止权限委托给模型。

## 2026-09-09：`ok_no_path` 必须先补齐结构证据，不能让模型猜 RTL

### 现象与根因

顶层网表已可读取和 `link_design`，但 `report_checks` 没有输出 data arrival time。这个状态至少有三种
不同原因：时钟端口/约束没有建立、综合网表中没有可见寄存器（例如顺序逻辑被测量黑盒抽象）、或寄存器
存在但没有受约束的时序路径。仅用 `sta_status=ok_no_path` 无法区分它们；让上层模型直接选择“改 RTL”或
“改任务卡”会把证据缺口变成猜测。

### 固定预防规则

在同一次真实 OpenSTA 链接后、`report_checks` 前记录 `STA_DIAG_STATUS`、`get_ports clk`、`get_clocks *`、
`all_registers` 和 `all_inputs -no_clocks` 的计数。supervisor 发现历史 `sta_no_timing_path` 缺少这些字段时，
只允许一次零修改的 `top_level_sta/600s` smoke，并暂停 full PPA 里程碑。诊断后的 `no_timing_path_cause`
才可作为 harness/约束/RTL 假设的输入；若诊断 run 本身失败，应报告具体后端错误而非循环重试。

## 2026-09-09：`report_checks` 的数值位于行首，不能用最后一列判定“无路径”

### 现象与根因

真实 OpenSTA 日志已有 `STA_TIMING_BEGIN/END` 及寄存器到寄存器路径，例如
`1689.858   data arrival time`、`-1684.275   slack (VIOLATED)`；但 harness 以 AWK 的 `$NF`
取值。该字段实际分别是 `time` 与 `(VIOLATED)`，数值正则必然失败，导致 `sta_timing_measured=no`、
`sta_status=ok_no_path` 的假阴性。随后模型会在“时钟/寄存器存在却无路径”的错误前提上猜测约束问题。

### 固定预防规则

解析器必须限于本轮 `STA_TIMING_BEGIN/END` 标记之间，并从 `data arrival time` 与 `slack (...)` 行的
第一个数值字段提取结果；分别保留最大 arrival、最小 slack 和 `Startpoint:` 计数的路径数。把真实 MET/VIOLATED
混合文本作为回归样例，禁止再以 `$NF` 解析人类可读 STA 报告。只有该解析器得到路径后才能报告
`sta_timing_measured=yes`；这仍只是带黑盒的 `PARTIAL_HIERARCHICAL` STA，不是全芯片签核。
解析器还必须在目标运行机的 AWK 上验证：本服务器的 mawk 1.3.3 对 `[[:space:]]` 的匹配不可靠，故在已有
marker 限定范围内使用普通文字匹配，并把同一份服务器 STA 日志作为跨环境回归输入。

## 2026-09-09：Agent 不应直接吞完整 EDA 日志，而应获得有界的结构化因果证据

### 现象与根因

完整 `sta.log` 保留在服务器不可变 `runs/` 中，长度和噪声都不适合直接放进 Pro prompt；但若只把
`sta_status`、`sta_timing_measured` 等最终标量传给模型，一处上游解析缺陷就会抹掉 `Startpoint`、`Endpoint`、
`Path Group` 与 slack 等决定性上下文。此时 Pro 只能对错误摘要推理，可能把解析缺陷误判为 RTL、时钟或约束问题。

### 固定预防规则

- 原始 EDA 日志留在服务器 `runs/`，不为模型输入而回传大型 run 文件。
- harness 必须确定性提取一个有界 STA 证据包：报告路径数、最差路径的起点/终点、路径组、arrival、required
  与 slack；警告只传计数和少量样例，并带 parser version/status。
- arrival/slack 必须关联同一条最差路径，不能把“最大 arrival”和“最小 slack”拼成貌似同一路径的结论。
- 若 `STA_TIMING_BEGIN/END` 或 `Startpoint:` 已出现而数值提取为空，controller 必须分类为
  `sta_report_parse_error` 并修 harness；禁止回落成 `ok_no_path`。
- Pro 读取上述结构化摘要和有限证据摘录；只有需要人工复盘时才由人只读打开服务器原始日志。

## 2026-09-09：只读证据分析不得复用“继续执行”动作

### 现象与根因

Pro 曾在 directive 中要求“只读已有 `sta.log`、禁止新 PPA”，但决策 action 仍为 `continue`。旧
supervisor 把 `continue` 无条件解释为启动 Flash controller，于是产生一次不需要的功能/STA 探针，并在
180 秒 closure export 超时后留下混淆证据。这里不是 STA 本身失败，而是“分析已有证据”和“开始新实验”
复用了同一个动作词，prompt 约束无法消除这种控制面歧义。

### 固定预防规则

- controller 应给只读、确定性的证据抽取分配独立 action；该 action 只能读取已完成的不可变 run，并不得
  启动功能回归、综合、PPA 或实施模型。
- `continue` 的语义必须唯一：启动下一批执行 controller。若 directive 与该语义显式矛盾（已有 STA +
  禁止新 PPA），controller 应以安全的只读 action 覆盖，而不是相信模型文本会自洽。
- 路径级证据包须按单条路径关联 startpoint、endpoint、arrival、required 与 slack；禁止把不同路径的
  全局最大 arrival 和最小 slack 拼成一条虚构关键路径。
- 同一个 source run 已被抽取过时，应以 `no_new_evidence` 停在边界，避免“读取同一日志”成为无限循环。

## 2026-09-10：把模型的证据需求声明化，而非开放任意 shell 或持续增加专用解析器

### 现象与根因

STA 摘要已能告诉 Pro 起点、终点和 slack，却不足以判断长路径是实际标准单元链、黑盒边界还是约束伪影。若
禁止模型查询原始证据，它会要求不断添加专用 extractor；若直接开放 shell/SSH，又无法可靠阻止凭据探测、
任意远端读取或写入。两端都不适合作为长期 harness 策略。

### 固定预防规则

- 将模型的只读证据需求表示成受 schema 约束的 action，而不是自然语言命令。controller 验证 run-id、
  artifact、selector、行数和超时后，用自身固定命令读取不可变证据。
- 首先提供少量跨项目通用视图，例如 STA 的最差 slack 路径、最大 arrival 路径和全部有界路径块；返回值同时
  含结构化 summary 和原始 cell/instance 链，让模型负责归因。
- 原始片段必须在下一个决策 prompt 中回送并计入语义指纹；同一请求不得重复。只有通用 schema 确实无法
  表达反复出现的证据需求、且可附最小回归测试时，才扩展 broker，而非为某个模块名写专用脚本。
- 该接口不调度功能/PPA，不修改 RTL/harness/runs；实验与写入仍走确定性 controller 门禁。

## 2026-09-10：证据 broker 的 source id 必须与 controller attempt id 分离

### 现象与根因

Pro 的结构化结果中同时出现 controller 的 `...--attemptN` 标识和实际 PPA run。模型将前者填入只读 STA
查询，严格的 run-id 白名单正确拒绝请求，但整个 session 因格式错误在 Flash 前停止。问题不是模型不理解
STA，而是输入接口要求模型从相邻但语义不同的 ID 中手工选择文件路径。

### 固定预防规则

- 任何可读取不可变 artifact 的模型 action，都应由 controller 在 prompt 中显式提供 canonical source id；
  不要求模型从 `result.json`、session 名或日志文字推导路径。
- 为最常用的“最新可用证据”提供受控别名（本项目为 `latest_measured_sta`），由 controller 解析为满足
  link+timing 条件的真实 PPA run-id；别名解析结果也必须记录在 session 证据包中。
- 保留严格 path 白名单：错误的 controller attempt id 不得被当作文件路径接受。人类可读名称相似不能成为
  放松 run-id 校验的理由；正确做法是消除模型手工映射需求。

## 2026-09-10：脱离终端的最终回归必须固定标准输入并独立收尾

### 现象与根因

supervisor 触发的最终 prompt-0/48-token 回归在本次 run 中没有进入 SSH、Docker 或 Verilator，而是在
`environment.py --shell` 初始化 Python 标准流时报 `OSError: [Errno 9] Bad file descriptor`；随后 shell 的
`set -u` 又把未导出的 `HARNESS_REMOTE_HOST` 显示成了功能失败。相同 RTL 由人工直接执行 48-token 已通过，
说明这是 controller 子进程的启动环境与收尾分类问题，不是 RTL 或 golden mismatch。

### 固定预防规则

- supervisor 和 controller 启动的每个子进程都显式接入有效的 `stdin=/dev/null`，不能依赖父进程是否还附着终端。
- 最终回归复用普通功能门的基础设施重试和远程持久化结果读取；它不能是另一个未覆盖的单独启动路径。
- 最终回归必须写独立的结构化结果文件；非零返回后先按功能失败、基础设施失败或传输不确定分类，再更新
  supervisor 状态，避免状态文件停留在 `flash_running`。

## 2026-09-11：部分顶层闭合后应进入单模块黑盒展开，而不是无限修测量 harness

### 现象与根因

当 `link_design=ok`、真实 `report_checks` 已执行且功能回归通过时，整体仍可能因为其他计算模块保留黑盒而
标记为 `PARTIAL_HIERARCHICAL`。如果 controller 只有“`TOP_LEVEL_COMPLETE` 才允许 RTL/PPA 优化”的阶段，
Pro 会把所有后续回合继续解释成 measurement-completion，反复修统计、路径解析或存储字段，无法推进真正的
计算模块展开。`PARTIAL_HIERARCHICAL` 是最终芯片结论的限制，不应成为局部展开实验的阻塞条件。

### 固定预防规则

- 增加显式 `expand_blackbox` action；Pro 只提供一个模块名，controller 校验它当前位于
  `top_level_sta` 黑盒名单中。
- controller 只移除这一个模块，并强制一次 8/16/48-token 功能门、Yosys 预检和 targeted top-level PPA；
  本批禁止第二个模块、额外 harness 修复或 RTL 顺手修改。
- 展开实验继续标记 `PARTIAL_HIERARCHICAL`，单模块结果只能用于前后对比，不能冒充完整顶层面积、时序或 Fmax。
- SRAM/宏面积字段缺失只阻止完整总面积结论；只要目标模块的逻辑面积和时序可测，就可以先做模块级证据。
- 计划和实际动作必须分离记录：模块展开由 controller 确定性修改配置，Flash 只负责受限验证，避免同一轮混入
  不相关的 RTL/harness 假设。

## 2026-09-11：Pro 选择历史模块名时应重新决策，不应直接终止

### 现象与根因

Pro 已经正确判断应进入 `expand_blackbox`，但从历史 progress 中选出了 `mla_kv_cache`。该模块虽然曾参与黑盒/内存
统计讨论，却不在当前 `top_level_sta.blackbox` 列表中。controller 的严格校验正确阻止了错误的配置修改，但把这个
可恢复的模型输入错误当成致命决策格式错误，导致 session 在 Flash 启动前停止。

### 固定预防规则

- Pro prompt 必须直接携带当前 `top_level_sta` 黑盒候选列表，禁止模型从历史日志推导候选。
- 非法模块选择是可恢复的决策错误：记录模块和当前候选，下一周期重新调用 Pro，并明确排除该模块；最多重试 2 次，
  连续错误才停在可调试状态。
- 合法模块展开后，功能/预检/PPA 的确定性失败意味着该模块已获得负证据，下一轮从剩余候选继续；exit 75/76 只表示
  基础设施没有给出结论，必须保留 pending expansion 并重试原模块，不能误换模块。
- 这条恢复机制不放宽 `ppa.toml` 校验，也不自动替模型选择未知模块；安全性和持续运行能力分别由名单校验与有界重试负责。
