# RTL/EDA 事务登记簿

> 当前接入说明：这个 Skill 作为可选通用能力保留，但
> `doule_RTLharness` 已停止接入。该项目只有一条黑盒展开主线和一个串行
> Optimization 子流程，主线由 Module Registry 持有，优化活动由
> Optimization Skill 持有；强行增加独立 Ledger 会形成重复 owner。需要
> 多类父子事务、并发工作区或跨流程恢复的新项目仍可独立接入本 Skill。

## 目的

这个 Skill 为长流程维护唯一的主线事务身份，解决同一“待展开模块”同时复制到模块登记簿、Optimization mainline、controller pending、Evidence 和提示语后彼此漂移的问题。第一版只允许一个主线事务和最多一个临时优化子事务。

## 为什么必要

仅靠模型记住主线并不可靠，而把目标模块复制到多个状态文件也不是持久记忆：任一副本晚更新一轮，就可能恢复错误模块、重复执行已完成事务，或者在优化失败后忘记原目标。稳定事务 ID、generation（代次）和指纹能把“记住目标”变成可验证引用。

## 工作方式

主线通常经历：

```text
proposed → ready → active → verification_pending → completed
                    ↓
                 suspended → ready
```

Optimization 开始时创建一个指向主线的 `optimization_child`，父事务进入 `suspended`。子事务成功、延期或失败后结束，原父事务回到 `ready`，不会从历史日志重新推测目标。只有确定性的功能、STA、Registry 或 controller 恢复证据才能把事务标记为完成或失败。

Ledger 不理解 `blackbox_expansion` 的具体命令，也不比较 PPA 价值。`kind` 和 `subject_ref` 是透明引用；相关 Skill 负责政策，项目 adapter 负责命令映射，Pro 只在经过验证的 ready transaction IDs 中选择。

## 分工

- Module Registry 唯一拥有模块表示、验证和测量事实。
- Transaction Ledger 唯一拥有主线及子事务身份和生命周期。
- Optimization Skill 只保存 `interrupted_transaction_ref`，不复制主线目标。
- controller 保存 checkpoint、进程和命令执行所需的短期资源，不保存独立 pending 模块事实。
- Evidence/Pro 读取 Ledger 生成的战略投影，不成为事务真源。

## 迁移

旧项目只能从当前配置、结构化 result 和 Registry 证据迁移。不得从 progress prose（进度叙述）恢复事务。旧副本与当前事实冲突时返回 `hold`，不能自动选择一个看起来更可信的模块。

## 不做什么

本 Skill 不提供事务优先级算法、不维护任意并发队列、不保存完整执行契约，也不把基础设施失败写成 RTL 或事务失败。未来增加事务类型时，应通过通用引用和事件接入，而不是在 Ledger 中增加按 `kind` 分支的调度政策。
