AgentDock 0.3.6：修复 Console Chat 连续会话语义（Issue #20）。

- Form 保留默认 No session，一次提交一个独立任务。
- Chat 首次发送自动创建 Session，首条与后续请求携带同一会话 ID，适用于全部六种 Driver。
- 无 Session 的独立任务不再混入连续聊天；New session 清空 Thread 并提示“发送后创建”。
- URL 保存当前 Session，支持刷新、收藏、分享和浏览器前进/后退。
- 容器切换重置草稿和本地任务；失败重试保留 Session，旧请求不会清空新会话或切换到 Form 后的新草稿。
- 普通 HTTP 环境下兼容生成随机会话 ID。无新增数据库迁移或 Driver 持久化机制变更。

保留 .env 和数据卷，将 AGENTDOCK_VERSION 改为 0.3.6 后执行：

```bash
docker compose --profile images pull
docker compose up -d --no-build --wait
```

8 个组件发布 0.3.6 镜像，Linux amd64。已有智能体容器无需重建。跨越旧版本升级前仍需备份，并遵循运维指南中的既有迁移要求。
