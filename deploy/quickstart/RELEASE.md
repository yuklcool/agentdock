AgentDock 0.3.7：发布上游同步后的完整平台镜像。

- Task、Session、Event 游标分页，Workspace 文件 404/非法路径 400 修复。
- Codex 0.157.1、OpenCode 1.18.32 与更新的模型目录。
- Codex 工具选择和单任务工具覆盖。
- Codex app-server JSON-RPC 执行、推理摘要与执行进度开关、任务时间线进度展示。
- Codex 单实例预热、配置变化检测、闲置过期、取消/超时与退出清理。
- 保留 Nanobot、多租户隔离、Skill/MCP、Base URL、WebSocket 流式事件与会话恢复。
- 数据库迁移包含 0034_codex_default_tools；默认工具迁移保持旧 Codex 实例的搜索行为。

全部 8 个组件统一发布为 0.3.7，平台为 Linux amd64。

升级前备份数据库、实例 Volume 和 .env，保留现有密钥；将 .env 中 AGENTDOCK_VERSION 改为 0.3.7，然后执行：

```bash
docker compose --profile images pull
docker compose up -d --no-build --wait
```

控制面启动时执行数据库迁移。已有智能体实例需通过镜像更新流程应用新运行时，并保留原 Volume；普通平台重启不会自动替换已创建的智能体容器。
