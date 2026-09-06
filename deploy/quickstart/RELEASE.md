AgentDock 首个完整预构建镜像发行包，适用于 Linux amd64。

下载 `agentdock-deploy.tar.gz` 后，生成配置即可使用 Docker Compose 拉取并启动全部服务，无需在服务器构建源码。

```bash
tar -xzf agentdock-deploy.tar.gz
python3 configure.py --email admin@example.com
docker compose --profile images pull
docker compose up -d --no-build --wait
```

访问 http://localhost:8080，使用配置命令显示的临时密码登录。远程监听、HTTPS、升级及镜像读取权限见包内 README.md。

镜像前缀：`ghcr.io/yuklcool/agentdock`，标签：`0.3.0`。包含 control-plane、web-console、agent-runtime、postgres、egress-proxy、searxng、connectors、reverse-proxy。若包仍为私有，先 `docker login ghcr.io`，或由所有者将包设为 Public 后免登录拉取。
