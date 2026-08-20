# Task: KDA delta head

实现或修复 `rtl/kda_delta_head.sv`，使其实现 Nano-KPU L0 的单 head Delta Attention
递推。接口必须与 `tb/` 中的 testbench 一致。

必须满足：

- KDA state 初值为零，跨 token 保留；
- 先执行 `alpha[:, None] * state` 的逐行衰减；
- 再执行 `state += outer(k_norm * beta, value - k_norm @ state)`；
- 使用更新后的 state 由 `q_norm @ state / sqrt(32)` 读出；
- 不改动 `harness/`、`harness.toml` 或任务文件。

完成后运行：

```bash
python3 harness/run.py --task example_kda --seed 20260706 --prompt 1
```

