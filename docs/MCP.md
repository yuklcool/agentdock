# MCP 服务配置与连接测试

HTTP 和 HTTPS 使用相同的 MCP 配置、握手、工具发现和调用流程，HTTPS 额外提供
传输加密和证书验证。保存 HTTP 地址不再需要 `AGENTDOCK_ALLOW_HTTP_MCP_SOURCE=1`。
该旧变量可以移除。

当前远程连接使用 Streamable HTTP，填写实际 MCP 端点（例如 `/mcp`）。普通 REST
接口不能作为 MCP 端点。测试和 vanilla runtime 都不自动跟随重定向；如果端点返回
重定向，填写最终 URL。HTTPS 必须提供可信证书，不会为了测试关闭证书验证。

## 页面操作

- 在 MCP 服务新建、编辑页点击“测试连接”，无需先保存名称、说明或连接配置。
- 已保存服务也可在列表中直接测试。“启用”与测试结果独立；禁用服务仍可诊断。
- 测试验证 MCP 初始化和工具发现，显示服务信息、协议版本、耗时、工具摘要和错误原因。
- 测试只读取工具定义，不执行业务工具，不修改配置、密钥、启用状态或任务。
- 编辑已有服务时，密钥留空使用保存的密钥；输入新值只用于本次测试，保存后才更新配置。
- 修改 URL、认证信息或密钥后，编辑页的旧测试结果失效。
- 列表结果仅保留在当前页面，附带测试时间，不表示实时在线状态。

控制端测试固定经过与运行容器相同的过滤出口代理。它验证这一网络路径下的
协议连通性，不保证其他驱动的原生客户端加载、容器本地地址或业务工具执行成功。
`localhost` 指当前进程/容器本身，不代表 Docker 宿主机或其他容器。

## 内网 MCP

内网地址不因使用 HTTP 或 HTTPS 而获得不同的网络权限。部署管理员通过已有变量
为可信内网服务器放行最小 CIDR，例如：

```dotenv
AGENTDOCK_NANOBOT_SSRF_WHITELIST=192.168.1.100/32 192.168.1.101/32
```

标准、Coolify 和 quickstart Compose 会将相同范围传入：

- Nanobot 的部署级 `ssrfWhitelist` 配置（现有实现，无需修改 Nanobot 源码）。
- 出口代理的 `EGRESS_ALLOW_CIDRS`。

放行的内网目标从出口代理直接连接，不交给外部上游代理。域名所有解析结果必须
符合策略；域名黑名单仍然有效，回环和链路本地地址（包括云元数据）始终被代理阻止。
白名单只决定访问权限，目标仍须可从代理容器所在网络到达。

控制端的 `AGENTDOCK_MCP_TEST_PROXY` 默认是 `http://egress-proxy:8888`。
自定义部署可配置同等过滤能力的出口代理。代理不可达时测试返回失败，不会回退直连。

## 接口

```text
POST /v1/mcp-servers/test
POST /v1/mcp-servers/{id}/test
```

草稿接口要求 `url`，可提供 `auth_type`（`none`、`bearer`、`header`）、
`auth_header_name` 和 `secret`。已有服务接口允许空对象或临时覆盖字段。
两个接口都要求租户管理员权限；跨租户 ID 与不存在的 ID 一样返回 404。

已有服务省略 `secret` 使用存储密钥；非空值临时替换，空字符串表示本次不使用密钥。
改变地址来源（协议、主机或端口）、认证类型或自定义认证头时，测试和保存都要求
重新输入密钥，避免旧密钥发往其他来源。

结果包含 `status`（`success`、`partial`、`failed`）、`stage`、`server_info`、
`protocol_version`、`tools`、`tool_count`、`tools_truncated`、`duration_ms`、
`tested_at`、`test_location`、`network_route`、`warnings` 和结构化 `error`。
网络/协议失败以 HTTP 200 返回诊断结果；参数/权限错误使用现有 API 错误格式。

测试总时限 15 秒，同一控制端进程最多同时进行 4 个测试；繁忙时返回 429。
工具预览最多读取 20 页、500 个工具，截断时明确标注数量为已读取数量。
vanilla runtime 同样完整读取分页，超过该上限返回显式连接错误，避免静默遗漏工具。

## 更新部署

本次修改涉及 web-console、control-plane、egress-proxy 和 agent-runtime 镜像。
重新构建并更新这四个镜像；已有运行容器需要更新镜像，才能使用新的 runtime 分页逻辑。
若配置了内网白名单，重建/更新相关运行容器，使部署环境变量同步生效。

HTTP 代理现在转发 POST 请求体（包含 chunked 请求），并实时转发 SSE 响应。
上游失败后不会自动重试 POST，避免 MCP 工具重复执行。

验证覆盖真实 HTTP、经本地测试 CA 验证的 HTTPS、过滤代理、无认证/Bearer/自定义头、
握手、工具发现和工具调用；另覆盖证书失败、协议错误、超时、分页、租户权限和页面交互。
