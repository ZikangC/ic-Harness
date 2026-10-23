# Harness 运行环境

`environment.toml` 保存 SSH、Docker、工具和 Liberty 的运行配置；`models.toml` 保存 OMP 调用模板及模型角色。微架构阶段使用 controller、独立设计/审计 skill 与确定性检查，唯一入口为 `python3 harness/run.py architecture`，详见 `../ARCHITECTURE.md`。环境检查仍是只读工具。

项目根目录从配置文件位置解析。复制整个 `harness/` 到另一个项目后，默认使用新项目目录名组成远程路径；如需其他名称，在 `remote.project_name` 中明确设置。检查不会创建远程项目或同步源码。

当前环境是 `txjg7-2@100.118.125.71`，容器 `k3demo-eda`。宿主目录 `/home/txjg7-2/ic-project` 挂载到容器 `/work`，因此本项目对应宿主 `/home/txjg7-2/ic-project/harness_final`、容器 `/work/harness_final`，这两个项目目录尚未创建。

从项目根目录执行：

```bash
python3 harness/tools/check_environment.py --resolve-only
python3 harness/tools/check_environment.py
python3 harness/tools/check_environment.py --models
```

无 `--models` 时不会调用付费模型；`--models` 每个不同模型仅发送一次最小探测请求，禁用工具、扩展和项目规则，不保存模型会话。命令只读检查环境，不运行仿真或综合。

OMP 通过用户现有 `~/.omp/agent/models.yml` 解析 provider 和凭据。harness 不复制密钥；迁到其他机器时仍需配置 SSH、Docker、OMP 和认证。`models.toml` 中 provider 元数据是当前环境说明，修改这些字段本身不会重写 OMP 的 provider 配置。

2026 年 10 月 10 日重新检查：SSH、k3demo-eda、Python、Verilator、Yosys、OpenROAD、Liberty、STA 和两个配置模型的最小调用均通过。`connectivity.json` 是前一天的历史记录，不能代表当前权限。

芯片目标频率、面积、吞吐及存储预算属于 `input/spec`。本环境配置没有继承旧项目的硬件预算，也没有设置旧项目的远程执行目录。

architecture.toml 的局部格式修复上限和 reserved_audit_calls/seconds 均在 max_model_calls/total_active_seconds 总上限内工作；不增加总额度，不保证审阅完成，也不是芯片预算。设计调用触及保留额度会停止并保存证据。
