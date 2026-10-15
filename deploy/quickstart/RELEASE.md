AgentDock 0.3.8：发布 MCP 连接测试与自定义 OpenAI 兼容接口适配。

- MCP 服务支持 HTTP、HTTPS，新增连接测试，修复代理请求体及 SSE 流式传输。
- api、barebones/vanilla、OpenCode、Codex、Nanobot 支持自定义 OpenAI Base URL 与 API Key。
- 显式配置自定义地址时优先使用对应 API Key，避免订阅认证覆盖配置或令牌被转发。
- OpenCode 使用 Chat Completions 兼容 provider，Codex 使用 Responses provider；Claude Code 本轮不调整。
- 支持自定义地址更新、清空、会话恢复及热备失效，api/vanilla 客户端按任务隔离。
- 保留 Nanobot 上游源码、指定预构建基础镜像、多租户、Skill/MCP 和 WebSocket 会话能力。
- 新增固定版 Codex/OpenCode 原生 CLI 回归，验证实际请求路径、认证与流式输出。

全部 8 个组件统一发布为 0.3.8，平台为 Linux amd64。

升级前备份数据库、实例 Volume 和 .env，保留现有密钥；将 .env 中 AGENTDOCK_VERSION 改为 0.3.8，然后执行：

```bash
docker compose --profile images pull
docker compose up -d --no-build --wait
```

控制面启动时执行数据库迁移。已有智能体实例需通过镜像更新流程应用新运行时，并保留原 Volume；普通平台重启不会自动替换已创建的智能体容器。

自定义接口请在 Settings → Credentials 选择 OpenAI，填写 API Key 与基础路径。Codex 需要 Responses API，其余四种使用 Chat Completions；服务必须提供所选模型。地址必须满足部署网络可达及出口白名单要求。
