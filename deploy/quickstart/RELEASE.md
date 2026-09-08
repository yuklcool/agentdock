AgentDock 0.3.1：在模型凭据页面配置 Nanobot Base URL。

- 添加 OpenAI / Anthropic API key 时，可选填 HTTP(S) Base URL。
- 已有凭据可通过 Edit Base URL 修改或清空地址，无需重新输入密钥。
- 地址随凭据传到 Nanobot；下一次任务时应用，实例 API 地址覆盖设置仍优先。
- 包含数据库迁移、页面操作测试、真实网关和镜像拉取部署验收。

从 0.3.0 升级：保留原 .env 和数据卷，将 .env 中 AGENTDOCK_VERSION 改为 0.3.1，执行：

```bash
docker compose --profile images pull
docker compose up -d --no-build --wait
```

服务启动自动迁移数据库。已有智能体在实例 Overview 页更新运行镜像至 0.3.1，保留原 Volume；仅更新管理后台无法让旧 Shim 使用凭据 Base URL。刷新浏览器后在 Settings → Credentials 配置地址。

镜像前缀 ghcr.io/yuklcool/agentdock，全部 8 个组件使用 0.3.1 标签。Linux amd64。Nanobot 基础镜像版本保持 0.3.0-node24。
