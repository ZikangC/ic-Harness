# IC Harness：Evidence-Driven RTL PPA Workflow

这是一个面向 RTL（寄存器传输级）设计的本地与服务器协同 AI harness（任务控制框架）。
当前主线是 Task 02：使用 Yosys/ABC/Nangate45 完成综合与面积分析，并使用 OpenROAD
内嵌 OpenSTA 执行 STA（静态时序分析），在功能不回归的前提下逐步展开顶层黑盒并进行
PPA（功耗、性能、面积）优化。

本仓库保存项目适配、控制器、执行器、任务卡和小型结构化证据。通用的证据整理、闭合治理、
模块登记、优化生命周期和 STA 范围判断位于独立的 `RTLskill` 项目中，通过稳定 JSON 接口接入。

## 核心设计

旧流程容易让 Pro、Flash 和 controller（控制器）重复读取日志、解释 pending 状态并维护多份事实。
当前架构改为：领域 Skill 持有状态和判断语义，Evidence Skill 统一整理当前阶段证据，Pro 只读取
一个不可变 DecisionPacket（决策包），执行端只消费已经校验的请求或 Campaign Contract
（执行活动契约）。

```text
当前 result / feedback / Skill 结构化输出
                    │
                    ▼
       Stage Evidence Workflow
       Fact Catalog → Evidence Model → validator
                    │
                    ▼
       validated evidence + execution contracts
                    │
                    ▼
       Task 02 adapter → immutable DecisionPacket
                    │
                    ▼
              Pro 战略选择
                    │
          ┌─────────┴─────────┐
          ▼                   ▼
  Project executor     Flash Campaign Contract
  黑盒配置事务          有界战术工作包与验证
          └─────────┬─────────┘
                    ▼
       功能回归 / Yosys 预检 / STA-PPA
                    │
                    ▼
 Registry / Closure / Optimization 状态更新
```

### 职责与状态所有权

| 组件 | 负责 | 不负责 |
| --- | --- | --- |
| Module Registry（模块登记簿） | 模块表示、黑盒名单、验证历史、PPA 证据和当前黑盒展开主线 | 从 progress 文本推测真实状态 |
| Closure Skill（闭合治理） | 综合到 STA 的阶段分类、测量是否完整、结构修复与重测语义 | 功能正确性或 RTL 优化授权 |
| Optimization Skill（优化治理） | 候选价值判断、checkpoint、编辑、功能/STA 验证、有限重试、回退和主线交还 | 项目路径、远程命令和顶层战略选择 |
| STA Scope Skill（STA 范围判断） | 根据真实 RTL diff 和设计上下文选择最小可辩护验证范围 | 把局部 STA 冒充顶层验收 |
| Evidence Skill（证据整理） | 构建有界 Fact Catalog（事实目录）、调用 Evidence Model、校验引用和确定性降级 | 生成动作许可或修改 RTL |
| Task 02 adapter（项目适配层） | 映射项目路径、分区、命令、结果字段和项目动作 | 保存第二份 Skill 生命周期状态 |
| Pro | 在 DecisionPacket 的允许动作中进行战略选择 | 直接运行命令或扩展动作权限 |
| Flash | 在不可变 Campaign Contract 内提出并执行有界战术工作包 | 改变修复域、允许路径或验证上限 |
| supervisor/controller | 进程调度、预算、停止、并发保护、身份和参数校验、命令分发 | 重复实现 Closure 或 Optimization 状态机 |

当前项目不接入额外 Transaction Ledger（事务登记簿）。黑盒展开主线由 Module Registry 唯一持有，
Optimization Skill 只捕获不可变主线引用并在优化结束后交还，避免多个 owner（所有者）维护同一 pending 状态。

## 证据与决策边界

- Evidence Model 只读取当前阶段的有界事实，不自动导入累计 progress、旧 Pro 回复或完整历史日志。
- 模型只选择 `fact_refs` 并生成摘要；真实值、来源、identity（身份）和 generation（代次）由 validator 解析。
- `integrity_state` 描述证据真实性，`summary_state` 描述摘要质量；摘要失败不会自动变成 RTL 失败。
- Evidence Model 不可用但确定性证据完整时，工作流使用 deterministic fallback（确定性降级）继续。
- Pro 只能选择 DecisionPacket 中存在的动作和参数，不能提交或修改 `executor_ref`、执行模式和操作身份。
- 自由文本 `flash_directive` 已移除。Flash 只能在物化后的 Campaign Contract 内工作。
- 服务器 `runs/` 是不可变证据库；本地不删除、清理或回传大型运行目录。

## 优化与黑盒展开

Optimization Skill 在一次 campaign（优化活动）内推进：

```text
候选价值判断
→ checkpoint
→ 单文件 RTL 编辑
→ 功能检查与 Yosys 预检
→ STA Scope 判断
→ 局部 STA 或顶层验证义务
→ 接受 / 有限重测 / 回退
→ 恢复原黑盒展开主线
```

内部优化步骤不重复调用 Evidence Model，也不消耗 Pro 的 `--max-cycles` 指导轮数。功能检查失败时
不会启动 STA；测量期间禁止再次编辑同一候选；有限修复或测量重试耗尽后由 Skill 执行回退。
当优化需要共享或顶层 STA 时，Skill 输出标准验证义务，由项目适配层判断能否与当前黑盒展开测量合并。

黑盒展开始终一次只处理一个 Module Registry 确认的 `top_level_sta` 黑盒。部分层次 STA
（`PARTIAL_HIERARCHICAL`）可以证明当前带黑盒设计已链接并存在真实时序路径，但不能写成无黑盒完整芯片
STA 或 Fmax（最高工作频率）结论。

## 快速开始

### 前提

- 本地已安装并配置 OMP；
- 已配置 Pro、Flash 使用的模型 selector（模型选择器）；
- 可访问项目配置的远程执行服务器和 Docker EDA 环境；
- 本项目同级存在 `RTLskill`，或通过相应命令行参数/环境变量提供 Skill 路径；
- 启动前确认没有另一个 Task 02 controller 正在运行。

### 启动 supervisor

```bash
cd /path/to/ic-Harness

python3 harness/pro_supervisor.py \
  --pro-model deepseek-v4-pro-202606 \
  --flash-model deepseek-v4-flash-202605 \
  --max-cycles 12
```

Evidence Model 默认复用 `--flash-model`。需要独立模型时增加：

```bash
--evidence-model <model-selector>
```

`--max-cycles` 只统计 Pro 战略指导调用。Optimization Skill 内部的编辑、功能验证、局部 STA、
有限重测和回退由独立步骤预算约束。

当前 Harness 的 Pro prompt（提示输入）保护上限为 200000 字符；这只是本地决策包上限，
不等于模型服务的 token（词元）上下文窗口。Evidence Model 的输入规模可以通过以下参数调整：

```text
--evidence-prompt-max-chars
--evidence-max-artifact-bytes
--evidence-max-facts-per-skill
--evidence-max-total-facts
```

### 查看状态

```bash
bash scripts/pro_supervisor_status.sh
```

查看当前 session 的详细日志尾部：

```bash
bash scripts/pro_supervisor_status.sh --verbose --tail 160
```

### 安全停止

先从 `status/pro_supervisor.status` 读取 session，然后执行：

```bash
bash scripts/request_pro_supervisor_stop.sh <session>
```

停止请求在当前执行边界生效，不会强杀正在进行的功能检查或 STA/PPA 测量。

## 主要目录

| 路径 | 内容 |
| --- | --- |
| `harness/pro_supervisor.py` | 战略指导轮、Skill campaign 与项目执行的总编排 |
| `harness/pro_guidance_runner.py` | DecisionPacket prompt、Pro 输出解析和不可变决策校验 |
| `harness/decision_packet_adapter.py` | 将已验证证据和执行契约合成为 Task 02 允许动作 |
| `harness/task02_project_adapter.py` | Task 02 路径、命令、分区和结果归一化 |
| `harness/ppa_agent_loop.py` | 消费 Campaign Contract 的 Flash 战术执行循环 |
| `harness/task02_project_executor.py` | 无模型、无循环的项目配置事务执行器 |
| `harness/task02_optimization_executor.py` | 无状态的优化功能/预检/测量执行器 |
| `harness/flash_campaign_contract.py` | Campaign Contract 构建与校验 |
| `harness/rtl_skill_bridge.py` | 外部 RTLskill 稳定入口与返回结构校验 |
| `tasks/02_yosys_ppa.md` | 当前 Task 02 目标和验收边界 |
| `runs/` | 本地单次运行证据；不要将大型生成物提交 Git |
| `reports/02_yosys_ppa/` | 人类可读的进度与 Pro 审计记录 |
| `status/` | 当前控制器和 Skill 持久化状态 |

## 测试

项目侧关键回归可以直接运行：

```bash
python3 tests/test_pro_supervisor.py
python3 tests/test_rtl_skill_bridge.py
python3 tests/test_task02_optimization_adapter.py
python3 tests/test_optimization_skill_step.py
bash tests/test_ppa_scheduler.sh
```

这些测试覆盖 DecisionPacket、Evidence 接入、Skill bridge（桥接层）、Optimization 项目适配、
Campaign Contract 和 PPA 调度政策。真实远程功能回归和 STA/PPA 仍依赖配置好的服务器与 EDA 环境。

## 结果解释原则

- Yosys exit 0 只证明对应命令完成，不自动等于完整芯片 PPA 成功。
- 分区 ABC 结果是 partition proxy（分区代理测量），不能写成完整芯片 Fmax。
- 带允许黑盒的顶层 STA 必须标记为 `PARTIAL_HIERARCHICAL`。
- `exit 124/137` 是原始执行事实；是否属于测量未完成由 Closure Skill 根据同轮证据判断。
- SSH、容器、远程传输和模型服务失败必须与 RTL/功能失败分开记录。
- 功能、预检和 STA 的证据身份不一致时必须保守阻塞，不能拼接成一次成功验证。

## 当前限制

- 统一覆盖 RTL/filelist、Yosys lowering（降级转换）、网表结构、后端 link（链接）、时钟、寄存器和
  timing path（时序路径）的 `health.json` STA 健康检查尚未完整接入。
- `netlist-handoff-compatibility` 已在 RTLskill 中实现，但当前项目尚未把它作为正式 STA 的硬门。
- 单元和政策测试不能替代真实服务器上的功能、综合和 STA/PPA 验证。

## 文档

- [Pro / Flash supervisor 详细设计](docs/pro_supervisor.md)
- [Task 02 任务卡](tasks/02_yosys_ppa.md)
- [问题与解决记录](questionAndsolution.md)

`archive/` 和旧任务文档仅用于历史审计，不代表当前 Task 02 的运行入口。
