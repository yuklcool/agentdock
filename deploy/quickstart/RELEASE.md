AgentDock 0.3.2：管理员可为当前工作空间用户创建私有智能体实例。

- 创建页新增 Owner user 选择框，仅管理员/Owner可见，候选人为当前工作空间的有效非 Staff 用户。
- 管理员代用户创建时按目标用户统计私有实例配额，审计同时记录创建者与实际归属人。
- 管理员实例列表显示归属用户；普通用户自动看到并使用属于自己的实例。
- 普通用户不能指定归属用户；拒绝跨工作空间、无效用户和共享实例指定Owner。
- 复用现有权限模型，不迁移已有容器归属，也不引入模板固定绑定。

升级：保留原 .env 和数据卷，将 AGENTDOCK_VERSION 改为 0.3.2 后执行：

```bash
docker compose --profile images pull
docker compose up -d --no-build --wait
```

本功能由控制平面和管理前端提供，已有实例无需因本功能重建运行容器。刷新浏览器，进入 Fleet → New，选择 Owner user 后创建实例。未指定归属用户时保留原有行为。

全部8个组件统一发布0.3.2镜像标签，Linux amd64。基础 Nanobot 仍为0.3.0-node24。
