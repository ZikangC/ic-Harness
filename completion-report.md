# 候选 cce936fb 独立映射基线

已完成。北京时间 2026-10-28 09:26:18 启动映射阶段，09:42:58.970764 退出，exit 0；阶段墙钟耗时 1000.847952 秒（16 分 40.85 秒），包括检查和最终网表写出。09:43:07 supervisor 完成事后身份核验并发布 completed。10:29–10:31 只读复核真实结果。

## 结果

| 指标 | 真实结果 |
|---|---:|
| Nangate45 标准单元映射面积 | 5,555,849.165983 µm²，约 5.556 mm² |
| 其中时序单元面积 | 238,169.218 µm²，约 0.238 mm² |
| 其中组合单元面积 | 5,317,679.947983 µm²，约 5.318 mm² |
| 标准单元数量 | 5,408,860 |
| DFF_X1 | 52,669（包含于标准单元总数） |
| SRAM 黑盒 | 9 个，共 1,720,320 bit；实际宏面积未知 |
| 映射后结构检查 | check -mapped -assert，Found and reported 0 problems |
| STA | 未启用；缺宏 timing、完整时钟和 I/O 约束 |

9 个有逻辑的 ABC 区域全部完成并重新导入 Yosys，纯层次顶层不需要实际 ABC 转换。原脚本 hierarchy -check、check -mapped -assert、stat、Verilog/RTLIL 写出均随完整命令 exit 0 完成。映射网表约 594 MB，mapped.il 约 1.35 GB，均已在服务器计算 SHA。

标准单元数从叶级计数核对：Yosys design.num_cells=5,408,869，减去 9 个 SRAM 黑盒得到 5,408,860。JSON 的 nano_core=1 是层次引用，不能再次计入叶级总数。面积采用 design.area/顶层包含子模块的总计，不能使用文本中顶层本地逻辑面积 0，也不能把包含子模块的模块面积重复相加。

## 输入与工具身份

候选：`cce936fb3061e338d156ab1dcd1ef7934a5c5fe7c188efa1a8533016b8532f33`。顶层 `msh_chip_top`，编译源为 nano_leaf.sv、nano_nl24.sv、nano_mem.sv、nano_core.sv、msh_chip_top.sv，include 目录 rtl；无额外 define/参数。完整工作包、架构与 RTL 闭包继续绑定本次 source_manifest/request；不改变原有限范围 RTL 审阅结论。

复用 generic.il SHA `1fceea5c4ed165c2471a5de7ffad771f809367f1fc67b1a7ad9f5901a14e7ec8`。映射脚本 SHA `0a392e98bfe2d7bf190ff35d873428bdffcf5384275c6fa344126d647931c384`，与原脚本相同。Yosys 0.64 commit `d8dab5b32666564eca8e18f412973853ce006e61`，同一 ABC 和 Nangate45 typical Liberty；工具/库 SHA 均固定于 request。

独立运行增加 COLUMNS=4096，真实 Yosys/ABC 继承情况在启动观察中核验。源码、库、算法脚本保持绑定；完整 generic 检查点复用后只运行 04_map。原运行的 request/preparation/source_manifest/04_map 脚本/producer result/generic 检查点再次哈希核对均未变化。原等待作业没有被取消、恢复、重启或注入数据。

## 警告与结论边界

ABC 报告库中两个多输出单元（例如 FA_X1）；映射全过程仍 exit 0。SRAM 的 8 种参数化类型、9 个实例没有物理面积，stat 明确报告 area unknown。ABC 跳过时序库单元；DFF 由前面的 dfflibmap 处理，最终均为 DFF_X1。

这是当前配置下的全设计标准单元映射结果，适合作为后续同配置比较基线。5.556 mm² 是库面积之和，不是 die/core 面积，不包含 SRAM 宏、布局利用率和布线开销。100 MHz 仍只是规格目标，不能宣称时序通过。映射结构检查不替代功能等价或扩大原独立 RTL 审阅范围。本次结果不能单独证明 Readiness 相对其他候选的整设计收益。

## 证据位置

服务器 `czk@100.85.224.17`，Docker `eda`，共同根目录：

`/work/harness_final/output/map--20261028-cce936fb-baseline`

- 作业：`map--20261028-cce936fb-baseline--mapping-01`
- 真实日志：`logs/04_map.yosys.log`
- 退出结果：`reports/04_map.result.json`、`reports/completion.json`
- 面积和资源：`reports/mapped.stat.json`、`reports/mapped.stat.txt`
- 网表：`mapped.v`，SHA `1db6e12e5c2a6facfd549f195b1a776c6dff403379a5d4efe45bc320a22ca891`
- 检查点：`checkpoints/mapped.il`，SHA `0d8d71f01dfb3f25aab7245853c06e3ae25d08a806d2624a1f7a119d926a2eb9`
- 完成复核：`completion-summary.json`，包括原始小 stat、检查行、产物 SHA 和原文件保护复核。

本地仅保存小摘要与执行辅助脚本；网表、RTLIL、大日志、EDA 数据均留服务器。本目录 completion-summary.json 是从真实服务器结果提取的小摘要。未新增周期检查或其他综合作业。
