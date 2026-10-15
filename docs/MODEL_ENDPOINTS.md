# 自定义 OpenAI 兼容模型接口

在 **Settings → Credentials** 选择 **OpenAI**，填写 API Key 和可选 Base URL。
同一租户的 api、barebones（内部驱动名 vanilla）、OpenCode、Codex、Nanobot 都使用
这份 OpenAI 凭据。实例需要选择 OpenAI 服务商下的模型；模型名称仍需在平台目录中可选，
且目标服务须提供该名称，或在网关上配置对应模型别名。

## 各模板的协议

| 模板 | 自定义地址使用的接口 | 说明 |
|---|---|---|
| api | `BASE_URL/chat/completions` | 按任务创建兼容客户端 |
| barebones / vanilla | `BASE_URL/chat/completions` | 支持工具调用，客户端按任务隔离 |
| opencode | `BASE_URL/chat/completions` | 使用独立的兼容 provider 和环境变量 Key |
| codex | `BASE_URL/responses` | 使用 Codex 原生 Responses provider |
| Nanobot | `BASE_URL/chat/completions` | 使用现有原生 `apiBase` 与 `apiKey` 配置 |

**仅支持 Chat Completions 的服务不能直接用于 Codex。**此适配不在服务端实现
Chat Completions/Responses 协议转换。Codex 的目标服务还须兼容其所需的 Responses
流式事件和工具协议。界面会提示该区别。

Claude Code 本轮不调整，继续使用现有 Anthropic 协议与认证方式。Anthropic 凭据中的
Base URL 仍仅适用于 Nanobot。

## 地址填写

填写基础路径，例如：

```text
https://gateway.example.com/v1
http://192.168.1.100:8000/v1
https://webui.example.com/api
```

保留服务实际要求的路径前缀。平台不会自动补 `/v1`，不会把 `/api` 改成 `/v1`。
不要填写包含 `/chat/completions` 或 `/responses` 的完整请求地址。
URL 不允许携带用户名、密码、查询参数或片段；密钥单独保存在 API Key 字段。
HTTP、HTTPS 的配置传递方式相同。内网地址仍需要部署网络可达，并满足出口白名单。

凭据列表中 **OpenCode** 指 OpenCode Zen / Go 模型服务商，不是 OpenCode 执行引擎的
通用凭据入口。使用第三方 OpenAI 兼容服务时，应选择 **OpenAI** 凭据，而非 Zen / Go。

## 凭据选择与更新

- 显式配置 OpenAI Base URL 后，支持该地址的驱动优先使用同一条 API Key 凭据。
  即使同时连接了 ChatGPT 订阅，也不会把订阅令牌送到自定义地址。
- 清空 Base URL 后恢复原有服务商地址和认证选择规则；已有 ChatGPT 订阅可能再次被优先选择。
- 修改或清空 Base URL 不需要重新输入已保存的 Key。更新在下一次任务开始时生效，
  正在运行的任务继续使用其启动时的配置。
- OpenCode 每次启动替换或清除平台管理的兼容 provider，保留 MCP、指令及其他 provider 配置。
  配置文件只引用 `{env:OPENAI_API_KEY}`，不写入明文模型 Key。
- Codex 通过启动参数指定模型 provider，Key 只放入子进程环境变量。地址、Key 变化会改变
  热备指纹；恢复会话时显式指定 provider，避免沿用旧的自定义接口。
- api、barebones 使用按请求创建的兼容客户端，不修改共享路由器，避免并发租户串用地址。
- 自定义接口请求失败不会自动回退到官方 OpenAI 地址。
- Nanobot 现有实例级 `AGENTDOCK_NANOBOT_API_BASE` 显式覆盖仍有更高优先级。

## 部署与验证

更新 control-plane、web-console、agent-runtime 三个镜像；已有运行容器需更新到新镜像。
不修改 Nanobot、OpenCode 或 Codex 上游源码。

常规回归包括凭据选取、五个驱动的 URL 透传、租户并发隔离、Key 不落盘、配置更新与清理、
失败不回退、Claude Code 保持原行为，以及界面保存与编辑。

可选原生 CLI 测试使用镜像中固定版本的 CLI 和本地模拟 API，不需要真实 API Key：

```bash
npm install --prefix /tmp/agentdock-endpoint-clis --no-audit --no-fund opencode-ai@1.18.32 @openai/codex@0.157.1
AGENTDOCK_TEST_CLI_DIR=/tmp/agentdock-endpoint-clis/node_modules/.bin \
  pytest -q packages/agentcore/tests/drivers/test_custom_endpoint_cli_live.py
```

原生测试只验证配置、协议和流式响应，不执行模型返回的工具操作，并使用受限的只读测试沙箱。
