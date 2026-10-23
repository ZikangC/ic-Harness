# rtl-closure-governance 中文说明

## 目的

这个 Skill 根据真实闭合网表和 STA 反馈，判断顶层综合到时序报告的流程当前卡在哪个阶段，并为 controller 提供确定性的 repair（修复）、retry（重测）或继续条件。

它关注 closure export（闭合网表导出）、OpenSTA 读取、链接和 timing report（时序报告）链路，不负责选择 RTL 微架构优化。

## 为什么有必要

“STA 失败”不是一个足够精确的状态：可能没有网表、top 不存在、模块体丢失、解析失败、黑盒未声明、link 失败、没有时钟/寄存器/路径，也可能只是测量被超时截断。

通用大模型每轮从自由文本重新判断时，容易把同一日志分类成不同动作；它也可能把 Yosys exit 0 当成网表有效，把 `ok_no_path` 当成真实无路径，或把 timeout（超时）空产物当成结构缺陷。确定性 phase 状态机能让相同证据始终得到相同政策。

## 针对过的真实问题

- Yosys exit 0，但 closure 网表为 0 字节、0 个模块或缺少 top/body。
- OpenSTA `read_verilog` 报错，却被旧脚本标成 `ok_no_path`。
- `link_design`、时钟、寄存器和 timing path 的失败混成一个“STA 不工作”。
- `closure_export_empty + exit 124` 被当成结构性导出缺陷，实际只是本轮没有完成。
- exit 137（SIGKILL）等资源问题和 RTL/功能错误没有清楚分离。
- 带允许黑盒的真实时序被错误描述为无黑盒完整芯片 Fmax。

## 工作方式

`closure_gate.py` 可以合并三类小型证据：

- 从当前 feedback 提取的 key/value；
- `sta_closure_collect.py` 流式生成的网表结构 artifact；
- `tool_probe.py` 生成的最小工具能力证明。

`sta_closure_audit.py` 按固定优先级输出唯一 phase：

- `tool_probe_failed`
- `closure_export_empty`
- `closure_measurement_incomplete`
- `closure_resource_blocked`
- `closure_top_missing`
- `closure_body_missing`
- `undeclared_blackbox`
- `sta_parse_error`
- `sta_link_error`
- `sta_no_timing_path`
- `sta_timing_measured`

每个 phase 同时给出稳定的语义门：`structural_repair_required`（结构修复）、`diagnostic_required`（需要补诊断）、`closure_gate_required`（闭合门仍生效）、`measurement_rerun_admissible`（当前预算关系是否允许再次测量）、`measurement_resource_blocked`（测量资源边界）、`strategy_change_required`（必须换策略）和 `timing_ready`（真实时序已得到）。项目 controller 不需要也不应该自己维护 phase 集合。

项目准备下一次测量时，adapter 还可以提供“已耗尽测量”和“拟执行测量”的通用描述符。Skill 根据设计/配置指纹、范围类型和目标集合确定性输出 `measurement_relation`；预算、重试次数和 `continue` 这类动作名不属于测量身份。因此可以区分同一测量重试、改变范围、改变设计/配置和身份不完整，而不让 controller 解析项目语义。

最重要的政策分拆是：

```text
空网表 + 无同轮 timeout/truncation 证据
  → closure_export_empty
  → 修复导出、层次或脚本

空网表 + 有同轮 timeout/truncation 证据
  → closure_measurement_incomplete
  → 同一测量只允许严格提高预算；否则必须改变策略

重试次数或最大合法预算已耗尽
  → closure_resource_blocked
  → same_measurement 禁止；真实 changed_scope/changed_design_or_config 可以作为新策略候选
  → 仍不授权 RTL 优化或具体集成动作
```

连续两次相同定向检查没有 phase 迁移时，不再无限增加预算或重复完整 PPA。controller 可停在可调试状态或改变测量策略；是否允许 RTL 优化必须由登记簿、功能门和优化治理的独立证据在 Skill 外组合决定。

## 输入、输出和状态

输入可以是当前 feedback、tool probe、闭合网表或远端收集的小型 artifact。输出是紧凑 closure audit JSON，包含：

- phase；
- 网表字节数、模块数、top/body 情况；
- read skip、link 和 timing measured；
- repair/retry 标志；
- repair domain 和短错误签名。

`sta_timing_measured` 只证明真实 `report_checks` 产出了时序值。是否存在黑盒、未链接逻辑以及 measurement scope（测量范围）仍决定它是不是完整芯片结论。

## 职责边界

- Skill 定义 closure phase、失败分类和最小下一步。
- Skill 确定性比较测量描述符，给出测量关系和拟测量是否越过已耗尽边界。
- collector/audit/probe 脚本生成结构化证据。
- 项目 measurement adapter 负责运行 Yosys/OpenSTA 并输出约定字段。
- 下游生命周期 Skill 把测量政策与 pending transaction 组合成执行契约；controller 只校验契约、预算和运行安全。
- Pro 可以提出一个可证伪修复假设，但不能覆盖 phase 或要求跳过硬门。
- 项目只通过 adapter/bridge 调用 `closure_gate.py`；不得保留第二套本地分类器，也不得把旧 `result.json` 中的历史 audit 与本轮 Skill 输出合并。
- Skill 不可用、API 版本不兼容或缺少必需语义字段时，adapter 必须标记为基础设施不确定并 fail closed（失败时关闭执行许可），不能静默回退。

## 迁移到新项目

新 harness 应让测量 adapter 输出至少以下字段：closure 字节数与模块数、top、required body、read skip、link 结果、timing measured、状态和错误签名。

昂贵或环境敏感操作前，可以配置最小 tool probe。它必须执行真实的最小操作并在成功后输出 marker；只检查版本或 `command -v` 不足以证明能力。

大型网表应留在执行主机。远程场景只回传 collector 生成的小型 JSON 和必要 feedback。

## 不证明什么与当前限制

该 Skill 不证明功能正确、约束完整或 PPA 达标，也不会把 partial closure（部分闭合）升级为完整芯片 STA。它目前依赖项目 measurement adapter 提供时钟数、寄存器数、路径数等字段；它本身还不是覆盖 RTL/filelist/Yosys lowering 到 OpenSTA 全链的统一 `health.json` 检查器。
