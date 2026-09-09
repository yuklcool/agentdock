# AgentDock 镜像快速部署

这套部署只拉取预构建镜像，不需要 Git、Node.js、pip 或本地编译。需要 Linux amd64、Docker Engine、Docker Compose v2 和 Python 3（仅用于首次生成配置）。镜像统一发布到 `ghcr.io/yuklcool/agentdock`，版本为 `0.3.4`。

## 首次启动

下载发行页的 `agentdock-deploy.tar.gz`，解压到一个空目录。该目录只需保留本包中的文件。

```bash
mkdir agentdock
cd agentdock
curl -fL https://github.com/yuklcool/agentdock/releases/download/v0.3.4/agentdock-deploy.tar.gz -o agentdock-deploy.tar.gz
tar -xzf agentdock-deploy.tar.gz
python3 configure.py --email admin@example.com
docker compose --profile images pull
docker compose up -d --no-build --wait
```

打开 `http://localhost:8080`，使用配置命令显示的临时密码登录，首次登录修改密码，然后选择工作空间、添加模型凭据并创建 Nanobot 实例。

默认仅监听本机，适合 SSH 转发或已有反向代理。局域网服务器需在首次配置时指定监听和访问地址，例如：

```bash
python3 configure.py --email admin@example.com --bind 0.0.0.0 --url http://192.168.1.10:8080
```

公网使用自己的 HTTPS 反向代理，将完整域名转发到本服务的 8080 端口，并支持 WebSocket Upgrade。配置时使用 `--url https://你的域名`，脚本会启用 Secure 登录 Cookie；域名路由不再写死为 localhost。HTTP 体验模式不应传输真实生产凭据到不可信网络。

`.env` 会保存随机数据库密码、加密密钥和初始化账号信息，权限为 0600。脚本拒绝覆盖已有文件，升级时务必保留原密钥。数据库启动后自动执行迁移、内置模板初始化和管理员创建；重复启动不会重置已有密码。首次改密后可清空 `.env` 中的 `SEED_STAFF_PASSWORD`。

## 镜像拉取权限

如果 `docker compose pull` 返回 `denied` 或 `unauthorized`，先执行：

```bash
docker login ghcr.io -u yuklcool
```

在密码提示中输入你自己的 GitHub classic PAT（至少 `read:packages`），不要输入 GitHub 登录密码。账号需要拥有这些包的读取权限。包所有者也可在 GitHub 的 Packages 页面将这 8 个包设为 Public，此后支持免登录拉取。公开源码仓库不会自动使新镜像包公开。

务必使用 `--profile images pull`：它同时预拉取 Nanobot 运行镜像，控制平面可直接使用宿主机缓存启动实例。需要由控制平面从私有仓库自行下载时，可额外在 `.env` 配置 `AGENT_REGISTRY_USERNAME` 和 `AGENT_REGISTRY_PASSWORD`。

## 镜像清单

所有镜像均使用 `ghcr.io/yuklcool/agentdock/<名称>:0.3.4`：

| 名称 | 用途 |
| --- | --- |
| control-plane | 后端、认证、数据库迁移与实例调度 |
| web-console | 管理后台静态页面 |
| agent-runtime | Nanobot 0.3.0 / Node 24、Shim 与其他执行引擎 |
| postgres | 数据库及连接参数初始化 |
| egress-proxy | 智能体受控出网 |
| searxng | 搜索服务，内置 JSON 查询配置 |
| connectors | GitHub / Slack 等连接服务 |
| reverse-proxy | 统一 HTTP / WebSocket 入口，内置路由 |

Nanobot 基础镜像仍是指定的阿里云 `telchina/nanobot:0.3.0-node24`，固定摘要后叠加 AgentDock 管理层。一个镜像可创建多个隔离实例；每个实例独立容器和 Volume。

## 管理与升级

```bash
docker compose ps
docker compose logs --tail 100 control-plane
docker compose stop
```

升级前备份数据库、全部实例 Volume 和 `.env`，修改 `.env` 的 `AGENTDOCK_VERSION` 为已发布的新版本，然后重新执行拉取和启动命令。服务启动自动迁移数据库；已有 Agent 实例需通过镜像更新流程重建，保留原 Volume。不要通过删除 Volume 完成升级。

默认 Compose 项目名为 `agentdock`，运行网络名与开发部署相同，不要在同一 Docker daemon 同时运行旧开发栈和此部署。此包不自动配置告警接收地址或定时备份；生产运维参见仓库 `deploy/OPERATIONS.md`。

## 发布流程

发布工作流先为所有组件构建 `sha-<提交>` 候选镜像，在干净机器执行拉取、启动、登录、真实私有实例创建/删除和 Shim 验收。仅 main 的验收通过后才将相同镜像摘要发布为版本标签，并生成本部署包。版本标签不可覆盖不同内容；后续发行需同时更新 VERSION 和部署默认版本。
