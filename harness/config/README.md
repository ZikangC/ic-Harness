# 备份版本的运行配置

代码来自已验证的 v19 隔离快照。精确的原试验配置保存在 `../../backup/config-v19/`，其中 `rtl.toml` 是交付收尾的 3 作业 / 40 秒配置，不能当作一轮新优化的通用预算。

当前 `environment.toml` 示例指向 `czk@100.85.224.17`、Docker `eda`，宿主工作区 `/home/czk/workspace` 挂载到容器 `/work`。`project_name` 为空时使用本地项目目录名。运行前按实际部署名称设置它；配置检查不会创建或同步服务器项目。

默认 `rtl.toml` 使用原生产阶段默认值，不自动启用 readiness。为新实验配置 `[readiness]` 的预算、模式和审阅选项时，参考 `../docs/readiness-integration.md`、`../docs/readiness-diagnosis.md`。不要复用已完成试验的 run_id、工作目录或作业身份。

`models.toml` 保留验证时的角色与 OMP 模板。OMP 的 provider、认证和实际模型可用性由使用者自己的 `~/.omp/agent/models.yml` 管理；本包没有复制该文件或密钥。`architecture.toml` 是调用预算设置，芯片约束来自 `input/spec.md`。

仅解析配置、不连接服务器或调用模型：

```bash
python3 -B harness/tools/check_environment.py --resolve-only
```

如需真正检查环境，再运行不带 `--resolve-only` 的命令；只有显式添加 `--models` 才会进行模型探测。历史 `connectivity.json` 未打包。
