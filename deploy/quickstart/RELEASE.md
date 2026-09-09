AgentDock 0.3.5：同一工作空间内每位用户最多绑定一个容器。

- 管理员创建默认“不绑定”，公共容器可创建多个；可选有效用户作为唯一绑定。
- 数据库部分唯一索引与并发创建检查共同防止重复绑定，重复请求返回明确的 409。
- 工作空间 API Key 默认列表仍为未绑定容器，支持 owner_user_id 查找并调用本空间绑定容器。
- 普通用户 Session 的权限不变；服务密钥不会继承创建者身份。
- 暂停和归档占用绑定名额；destroyed 或永久删除释放名额。
- 配额页面显示固定 1 个绑定容器；OpenAPI、测试和接入文档同步。

升级前备份数据库和数据卷，检查是否存在重复绑定（见 deploy/OPERATIONS.md）。迁移 0033 遇到冲突会停止并列出冲突 ID，不会自动删除、解绑或共享数据。先在旧版本保全数据并永久删除多余容器，再重试升级。

保留原 .env 和数据卷，将 AGENTDOCK_VERSION 改为 0.3.5，执行：

```bash
docker compose --profile images pull
docker compose up -d --no-build --wait
```

8 个组件发布 0.3.5 镜像标签，Linux amd64。已有智能体容器无需重建。Workspace API Key 可调用本空间所有容器，请只在可信服务端保管。
