AgentDock 0.3.9：发布 Nanobot 模板初始化文件与运行时配置优先级（#16）。

- Nanobot 模板支持 config.json、AGENTS.md、SOUL.md、USER.md、HEARTBEAT.md、TOOLS.md 六个初始化文件的在线编辑、上传、替换与恢复默认。
- 模板只保存自定义覆盖；缺失文件由镜像自带的官方 onboard 初始化，平台不保存上游默认副本，不修改 Nanobot 源码。
- 新容器保存创建快照，一次性写入；模板修改及普通重启、恢复、镜像更新不覆盖既有文件。
- 模型、Provider、API Key、Base URL、模型预设与 fallback 由 AgentDock 运行时决定；其他 Nanobot 原生配置保留。
- 新 Nanobot 容器需显式选择模型，不再继承旧模板中的模型、镜像、资源或环境变量；旧容器保持原配置和文件。
- 数据库迁移 0035 仅新增两个默认空映射的 JSONB 字段；模型 Key 不进入模板或持久初始化配置。
- 新增 PostgreSQL 快照回归，以及真实镜像中的原生初始化、网关请求、只读文件系统与 uid 权限、多实例、暂停恢复和 Volume 复用验收。
- 保留 0.3.8 的 MCP HTTP/HTTPS 与自定义 OpenAI 兼容接口适配。

全部 8 个组件统一发布为 0.3.9，平台为 Linux amd64。

升级前备份数据库、实例 Volume 和 .env，保留现有密钥；将 .env 中 AGENTDOCK_VERSION 改为 0.3.9，然后执行：

```bash
docker compose --profile images pull
docker compose up -d --no-build --wait
```

控制面启动时执行数据库迁移。已有智能体实例需通过镜像更新流程应用新运行时，并保留原 Volume；普通平台重启不会自动替换已创建的智能体容器。

本功能针对新建容器/新 Volume。旧容器升级不会自动重新初始化或应用模板；请通过新建 Nanobot 容器测试模板文件。内置模板需先克隆后编辑。

自定义接口请在 Settings → Credentials 选择 OpenAI，填写 API Key 与基础路径。Codex 需要 Responses API，其余四种使用 Chat Completions；服务必须提供所选模型。地址必须满足部署网络可达及出口白名单要求。
