# Nano-KPU RTL Harness

这是一个让本地 OMP/DeepSeek 修改 RTL、让服务器 Docker 执行 reference 对拍、评分和诊断的
自动迭代 harness。

## 从这里开始

日常运行只需：

```bash
cd /Users/chenzikang/code/work/RTLharness

python3 harness/agent_loop.py \
  --task tasks/01_repair_multitoken_golden.md \
  --adaptive-prefix \
  --initial-seq-len 3 \
  --max-attempts 3
```

完整命令、监控、停止与排错请看 [使用说明](docs/使用说明.md)。

## 文档入口

- [使用说明](docs/使用说明.md)：日常该输入什么命令、如何看进度、如何停止。
- [工程架构说明](docs/工程架构说明.md)：本地、服务器、Docker、数据流和目录职责。
- [问题与解决记录](questionAndsolution.md)：已遇到的可复用工程问题及修复方法。
- [当前任务卡](tasks/01_repair_multitoken_golden.md)：DeepSeek 当前的 RTL 修复目标和约束。

## 当前基线

导入的 golden RTL 已通过单 token，但 prompt 1 在 pos2 有已知多 token route 分叉。当前任务
使用 3-token 因果前缀定位和修复，前缀通过后会自动回归完整 prompt 1。
