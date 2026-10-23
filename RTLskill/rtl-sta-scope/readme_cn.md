# rtl-sta-scope 中文说明

## 目的

这个 Skill 在 RTL、约束、filelist、黑盒政策或测量 adapter 发生变化后，判断哪些时序证据已经失效，并选择最小但足够的 STA（静态时序分析）范围。

它可以输出无需 RTL STA、单模块、受影响锥、部分顶层或完整顶层。它不运行 EDA，也不把局部结果升级为顶层证明。

## 为什么有必要

“任何改动都跑完整顶层 STA”可靠但成本过高；“模块内部计算逻辑正确，所以只测模块”又可能漏掉接口、组合边界、参数、共享状态、时钟复位和 fanout（扇出）影响。

通用大模型仅根据文件名或修改者的文字说明，很难稳定证明局部边界安全。特别是组合逻辑变化是否穿过模块边界、参数是否改变层次实例、黑盒政策是否改变真实集成范围，都需要结构化事实。这个 Skill 把“局部为什么足够”变成必须明确给出的证据；缺失或未知时自动扩大范围。

## 针对过的真实问题

- harness-only（仅测量脚本）改动被自动升级成完整多分区 PPA，长时间超时却没有新增证据。
- 局部计算模块变化与顶层集成变化没有区分，所有修改走同一昂贵路径。
- 分区 ABC proxy（代理估计）被误当成顶层 STA/Fmax。
- planner 自报 targeted/full，但实际 diff 的接口或组合边界风险与声明不一致。
- 黑盒名单变化后仍沿用旧的局部时序结论。

## 工作方式

项目 adapter 从真实 diff、配置变化和当前设计上下文生成 change packet（变更包），而不是让模型直接指定 scope。设计上下文至少带图指纹、源码归属、直接上下游和时序边界；它可以由模块登记簿提供维护后的当前投影，但图的构建仍属于项目 extractor。

只有同时满足以下条件，单模块修改才可以使用 `local_module`：

- 只修改一个 RTL 模块；
- 外部接口没有变化；
- 时钟复位、参数和共享状态没有变化；
- 跨边界组合行为没有变化；
- 边界明确由寄存器隔离；
- 影响范围已知。
- 设计上下文与当前 RTL/filelist/配置指纹一致，且存在该模块记录。

已知多模块依赖但不需要到顶层时可以使用 `affected_cone`。以下任一情况都会提升为顶层 STA：

- top、接口、时钟复位或参数变化；
- filelist、约束或黑盒政策变化；
- 跨边界组合逻辑或架构变化；
- 局部安全事实缺失或未知。

顶层检查仍有允许黑盒时输出 `top_level_partial`；只有没有剩余 timing blackbox（时序黑盒）时才输出 `top_level_complete`。

## 输入和输出

输入 change packet 包含：

- `change_mode`；
- 每个变更模块的路径和边界事实；
- 已知受影响模块；
- top/filelist/constraints/blackbox policy 是否变化；
- 剩余黑盒列表；
- 测量 adapter 是否变化。

`scope_gate.py` 输出：

- `scope`；
- `requires_sta`；
- `reason_codes`；
- `affected_modules`；
- `measurement_plan`。
- `minimum_scope` 和本次使用的 `design_context_fingerprint`。

缺失的安全布尔字段不会按 `false` 处理，而是不能证明局部安全，从而保守升级到顶层。

## 职责边界

- Skill 定义范围判断原则和 fail-closed（不确定即不放行）政策。
- `sta_scope.py` 根据结构化事实做确定性决策。
- 项目 extractor 构建依赖图；项目 adapter 负责解析真实 diff、配置和黑盒变化，并取当前模块上下文。
- controller 把 scope 映射为真实存在的分区与命令；对应局部分区不存在时必须回退顶层。
- 模型可以帮助识别变化事实，但不能用 prose（自然语言说明）覆盖 gate。

## 迁移到新项目

通用 gate 不解析 git diff，也不知道项目层次。新 harness 需要实现保守 adapter：

- 从实际 diff 识别模块、端口、时钟复位、参数、共享状态和组合边界变化；
- 构建或接入带源指纹的当前设计上下文，不能只传文件名或脱离层次的 diff 片段；
- 识别 filelist、约束与黑盒配置变化；
- 提供可靠依赖锥，无法证明时不要猜测；
- 把输出 scope 映射到项目的模块 STA、锥级 STA 或顶层 STA 命令。

项目可以改进 adapter 的精度，但不能通过缺省 `false` 人为放宽局部条件。

## 不证明什么

这个 Skill 只说明“需要重新获得哪个范围的时序证据”，不证明该范围已经测量成功。`local_module` 和 `affected_cone` 不能作为完整芯片 Fmax；`top_level_partial` 也不能作为无黑盒完整顶层 STA。
