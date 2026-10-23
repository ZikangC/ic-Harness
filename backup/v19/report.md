候选3综合成本能力：真实验证交接（v19，2026-10-22）

已形成可实际使用的局部优化候选：3+3测量中位数 12.685429 → 6.701814 秒，约 1.8928 倍、时间下降 47.17%；完整二值函数SAT等价通过。独立工程审阅 accept，受控采用只改变隔离副本 rtl/nano_core.sv。原生审阅钩子返回 pass / rtl_stage_release=true，但实际报告限定为本次交付及证据绑定审计；不能把该标志解释为完整RTL系统回归、全芯片功能或物理验收。

**固定输入与身份**

- V5冻结候选3：7dbe6321e1a1163fc98ece57f4c9f37711f54245d941eb4ab390ede7d734a5f3。
- 原工作包：/work/harness_final/output/skill-eval--20261019-real-rtl-regression/evaluation/rtl-skill-isolation-v5/project/output/landing--20261021-isolated-v5/rtl/candidates/candidate-3/work。
- 架构 revision-2/baseline：59bdc873d3c3c5fe31c1d89a7a49c2377d904521b5cf307753a6673837518b7f。
- 原独立审阅：audit/review-3/review3_report.json，SHA 510442a3e84e3953e52c129e7fc7f838294c74dc7a2be6f7439cd47ac1275166。
- 原24文件输入闭包SHA ab8411c6cf97ea784e3ee8d3f4f5a98bdb53336982262100eb5d27ed16af89e3；RTL十文件指纹 f282a84092099addb54505ff5fe56b5a0d50b22e497011f60070c7060a3ce8cc。
- spec SHA a07c1b9762577e850bf1bcd3f180b65e4662ee318c6e957ff3011dfada35a592；reference SHA 4825418cf2905a70e951834234bea7295fac4e413e49b5a561fb0da730f3f9ef。
- 原 nano_core.sv SHA 3763cc95037a83d1d51c42cefe9abc795768e48c3138f372e4d20e56478cc79e；变体 SHA db019c536f5d60c9b9399ead1110c6aff235e1262d76098dd974c9e5170d8423。其余原文件未变。
- 新隔离候选（含本轮普通证据闭包）原生SHA a5e26e78f7fa67465a92b238ff15324fb14d65891b20bea6e61dd860f575406f，与原冻结候选身份不同，不回写原候选。

整设计顶层 msh_chip_top。原综合有序SV源为 nano_leaf.sv、nano_mem.sv、nano_core.sv、msh_chip_top.sv，头文件 nano_ctrl.svh、nano_desc.svh、nano_exec.svh、nano_lut.svh、nano_prog.svh、nano_token.svh，均在 rtl/。TB与四组image/expected只作验证材料。完整逐文件SHA在 seed-provenance.json；没有把仿真顶层充作综合顶层。

本轮测量实际顶层是派生 probe_wb。workspace/request的top字段 nano_core 表示源模块，不能据此称整nano_core测量。冻结的 tests/derive.py 从各自版本原RTL生成局部探测，保留两数据平面、常量窗口及runtime-off控制；其重复规模小于全设计。完整前端曾耗845.7秒，不构成此局部范围的成本下限。

**真实测量与证明**

| repeat | baseline 秒 | candidate 秒 |
|---|---:|---:|
| 1 | 12.684905 | 6.603858 |
| 2 | 12.685429 | 6.703129 |
| 3 | 12.785433 | 6.701814 |

同配置、工具、源身份，三次区间分离；保留3+3及1.2倍比较门。局部 proc/opt_clean 后 $mux 67→1；未给全设计逻辑/寄存器/存储器/宏资源，也未把该数字换算成面积。

Yosys 0.64（git d8dab5b32666564eca8e18f412973853ce006e61），二进制SHA 43f5a1a2448bcc29fb09bd199f9f49dae6a33beaa1ecdc180d8a6eceb67af24f；容器Python3.12.11。配置沿用implementation deepseek-v4-flash-202605、review deepseek-v4-pro-202606。没有更换模型来掩盖失败。

v18 func-1从两个实际原文件提取函数体，检查两函数以外字节一致，构建flatten组合miter；全部128×128个off/wid及全部base/src二值数据自由，1个assert、无DFF、无附加输入约束。实际SAT 81805变量/215663子句、SUCCESS，工具6.25秒（内层receipt墙钟6.405904秒）。X/Z传播与整个设计reset/state不在此证明范围。

未进行标准单元库映射，Nangate45环境配置未作为本轮局部计量输入；未引入虚构SRAM模型或有效STA约束。无映射面积、宏面积、WNS/TNS或频率通过结论。架构数值与物理目标仍按原架构包作为后续义务，20k tokens/s与24万周期不被当作本轮致命门。

**作业、命令与证据**

服务器容器运行根：/work/harness_final/output/readiness-integration--20261022/v19/project/output/candidate3-delivery

- 6次测量原始位置：v17/project/output/candidate3-repair/readiness/campaign-1/work/ws/jobs/{base-1..3,cand-1..3}（版本根均在 /work/harness_final/output/readiness-integration--20261022/）。
- 实际配对证明：v18/project/output/candidate3-completion/readiness/campaign-1/work/ws/jobs/func-1，含 request/result、proof.ys、proof.log、verify.json；校验程序 verify_merge.py 的SHA e8c4bfa346eb9233503d42fe9b47ed95fc929ca280eb6cc28d672db6a8d09c3b。
- 当前7个reference直接固定原request/result及原始文件；6测量来自v17，证明来自v18。全部回源完整复核通过，没有改写旧计时或重新签名历史回执。
- clone native job 1792668595954190882：exit0，1.763302秒。
- finalize native job 1792668602300651425：exit0，1.897529秒。
- 审阅独立 verify-finalization job 1792669472824452548：exit0，1.730237秒。
- 三者request/stdout/stderr/result均在当前运行 jobs/<ID>/；0pending，cleanup全部完成。
- 工程审阅：readiness/campaign-1/audit/review.json（accept）；采用：readiness/campaign-1.adoption.json。
- 审阅钩子报告：audit/review-1/report.json，SHA 8152929cf0f61935e32751544f6b846de0fb904d1a4723267a752dc6f0b27355，实际scope见报告保留限制；无必要返修。
- 最终交付：/work/harness_final/output/readiness-integration--20261022/v19/project/output/candidate3-delivery/readiness/campaign-1/work/ws/final，含 comparison.json、candidate.patch、candidate.json、delivery.json、finalization.json。
- finalization identity bd0cb097c5f82d5dec266a05e3bb512c549826a61c44efc04661dcf119609047；patch SHA 11ec61f4f2e83ae84dc69e9dfcaa51005790ae68e7f58fb3a0bfae34df9bffce。

核心交付命令（实际完整argv见 jobs/1792668602300651425/request.json）：

```text
/opt/eda/bin/python3 -B <冻结skill>/scripts/readiness.py finalize <当前绝对ws> --variant maskmerge --baseline-job base-1 --baseline-job base-2 --baseline-job base-3 --candidate-job cand-1 --candidate-job cand-2 --candidate-job cand-3 --verification-job func-1 --out <当前绝对ws>/final
```

本地controller身份 PID66393/start Thu Oct22 19:29:32，exit0，984.206秒；完整一次性启动argv、freeze和seed均在 launch-intent.json/controller-identity.json/source-frozen.json/seed-provenance.json。run_trial.py有独占intent，不能把重启wrapper当断点续跑；复现应选新隔离run，现有run保留原身份。

**通用版本与实际修补**

可用快照为本目录 project/harness，源码身份 2cfe4870eb63161d5da5adab8b15f0ec32903503533bdbb3f7d29bea79896d0c。readiness skill自带Python标准库工具，可无其他skill独立分析、做小实验、比较、证明与输出候选；controller负责事件、预算、资源与已有作业。没有新增综合阶段入口、监控框架或函数/PROC_MUX/固定等待规则。

v14已实跑只读诊断；本轮用普通报告和已有日志进入优化/返修，未重复为获取已有证据而跑探测。实际失败驱动的窄修补包括：严格但容许单完整JSON围栏的报告解析、原路径可集成检查提前到handoff、预算拒绝显示数量/时间与余量、直接引用完整旧证据、CLI输出路径说明及取消已fresh-verified情况下强制重复机械检查。

reusable-fixes.patch是冻结v14→v19的18文件增量，排除环境/项目预算配置；在独立副本dry-run、实际apply与所有非config字节核验通过。完整快照包含既有诊断/执行/交付能力；patch不能直接假定适用于任意production版本。v19本地/容器12项复用检查通过，v18容器54项相关runtime检查+7项复用检查通过；v14历史完整回归1200项中1164通过/36跳过，不能把它说成v19重新跑过全套。

**预算、失败保留与边界**

v16→v18原20个native槽累计196.465971秒，包含失败和必要命令。v19另列最多3个仅交付metadata槽/40秒，实际2槽3.660832秒；正式审阅复核另1槽1.730237秒。全部工具墙钟合计201.857039秒，模型、SSH、资料准备与审阅耗时不计入这个工具数。没有悄悄重置为另一轮240秒，也没有重跑已完成的测量/证明。

v15诊断解析失败、v16工程revise、v17预算请求55秒超过剩53.0875秒、v18相对out路径失败均保留原样。旧摘要 trial-summary.json误将measurement分类为functional；修正摘要是 trial-summary-corrected.json，原摘要未覆盖。真实回执/审阅/代码未因摘要修正而改变。

原完整综合仍是 synth--20261021-v5-candidate3-1658--synthesis-01，PID16124/start_ticks69930191、同boot与命令，2026-10-22 11:47UTC仍运行02_coarse，约4.13GB RSS。未停止、升级或重复提交；原候选24文件、架构、审阅证据、综合脚本、production与canonical skill库全部保护核验通过。没有可靠完成ETA。

当前能证明：候选3上存在源码绑定、完整二值函数等价且局部降本显著的可交接修改，并已实跑独立工程审阅、受控单文件采用和交付审计。完整RTL系统/数值硬门验收、四态合同、全设计综合收益、资源与面积/STA仍未关闭。下一步最有价值的是单独安排新候选的适用RTL/系统验收及全设计综合比较；本轮不自动提交完整综合，也不要求跨设计最终验收。
