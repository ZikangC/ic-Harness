# netlist-handoff-compatibility 中文说明

## 目的

这个 Skill 在正式 STA（静态时序分析）或物理实现之前，独立验证“指定综合网表能够被指定后端真实读取并链接”。它专门治理前端综合到后端分析之间的 handoff（交接）问题，包括 Verilog 方言、层次、黑盒声明、库单元覆盖和后端命令兼容性。

通过该 Skill 只表示 `handoff_compatible`，不表示功能正确、时序闭合、可布线或满足 PPA。

## 为什么有必要

综合器和 STA 后端支持的 Verilog 子集并不完全相同。大模型看到 Yosys exit 0、网表文件存在或 `write_verilog` 已执行，很容易推断“后端应该能读”。真实流程中却可能在 `read_verilog` 就失败，根本没有执行 `link_design`。

如果没有独立交接门，解析错误会被误写成“没有 timing path（时序路径）”、黑盒问题或时钟问题，并诱发完整 PPA 反复重跑。模型也很难仅凭命令文本判断目标环境中的真实工具是否支持某种语法，因此必须执行短小的真实 read/link probe（读取/链接探针）。

## 针对过的真实问题

- Yosys `write_verilog` 生成的参数化或转义标识符不被 OpenSTA 解析。
- 行为式存储结构被写成 `reg`/`initial`，后端门级解析器拒绝。
- `write_verilog -selected` 的选区错误，生成只有 header（文件头）或缺失 module body（模块体）的网表。
- 后端错误读取 build 目录中的历史残留网表，使本轮正确产物被旧错误污染。
- 黑盒声明或 Nangate45 库单元模型缺失，导致 read 成功但 link 失败。
- 仅检查后端版本或命令存在，就把环境误判为可用。

## 工作方式

1. `scripts/netlist_inventory.py` 流式读取精确候选网表，记录字节数、SHA-256、模块数、`endmodule` 平衡、重复模块和 top 是否存在。
2. `scripts/handoff_probe.py` 按 `availability → read → link` 的顺序执行短探针。
3. 每个探针必须使用 argv（参数数组），执行真实后端操作，并且只有在操作成功后才输出声明过的 success marker（成功标记）。
4. `scripts/handoff_audit.py` 按“第一个阻塞阶段优先”生成唯一 phase。
5. `handoff_gate.py` 将 inventory、probe 和分类合成稳定的 controller JSON。

主要 phase 包括：

- `source_artifact_empty`：空网表或没有模块；
- `source_artifact_malformed`：模块结构不平衡或重复定义；
- `source_top_missing`：指定 top 不存在；
- `backend_unavailable`：后端命令或环境不可用；
- `unsupported_hdl_construct`：后端不支持综合网表中的 HDL 构造；
- `blackbox_contract_missing`：黑盒声明或接口契约缺失；
- `library_coverage_missing`：映射单元没有对应库模型；
- `backend_read_error`、`backend_link_error`；
- `handoff_compatible`。

若需要网表规范化，必须用声明过的工具转换生成独立派生产物，并记录源/目标 hash、命令、后端和 scope（范围）。不能直接文本修补生成网表来消除报错。

## 输入、输出和状态

输入是精确候选网表、top、后端 probe config（探针配置）和声明的 scope。输出包括：

- 网表结构 inventory JSON；
- 分阶段 probe JSON；
- `handoff_gate` JSON，其中包含 phase、repair domain（修复域）、next action 和 artifact hash。

只有 `handoff_compatible` 可以进入正式 STA 闭合检查。即使交接成功，只要范围内仍有允许黑盒，也必须保留 partial（部分层次）scope。

## 职责边界

- Skill 定义交接阶段、证据要求和修复域。
- inventory/probe/audit 脚本提供确定性事实。
- 项目 adapter 提供目标后端的真实 read/link 命令、库和诊断规则。
- controller 在 gate 未通过时阻止正式 STA/P&R，只允许对应修复和有界复测。
- 大模型可以提出规范化或适配假设，但不能手工指定 phase，也不能把交接成功升级成时序成功。

## 迁移到新项目

新项目需要为目标后端配置：

- availability、read、link 三阶段 argv；
- 每阶段超时和真实 success marker；
- 与正式流程一致的技术库、单元模型和 top；
- 后端特有且足够精确的诊断正则；
- 当前 scope 和允许黑盒契约。

探针必须运行在持有候选网表和后端工具的主机或容器中。远程 controller 只需回传小型 JSON，不应为了分类而复制大型网表。

## 不证明什么

该 Skill 不检查功能等价性，不运行完整时序分析，不判断约束质量，也不证明 PPA、可布线性或 tapeout readiness（流片就绪）。它只保证特定 artifact（产物）在特定后端配置下真实 read/link 成功。
