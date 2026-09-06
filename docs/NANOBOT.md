# AgentDock · Nanobot 集成指南

AgentDock 是可自行部署的智能体容器管理平台。本版本把官方
[HKUDS/nanobot](https://github.com/HKUDS/nanobot) 作为独立执行引擎接入：
一个 AgentDock Agent 实例对应一个 Docker 容器，容器中由 Shim 管理一个 Nanobot Gateway。

## 已接入的功能

- 管理后台选择 `Nanobot` 驱动，创建多个独立实例。
- 复用平台的启动、暂停、恢复、归档、资源限制和持久工作空间。
- 平台 Task API / SSE 与统一 `/v1/ws` WebSocket 入口。
- 按实例、按平台 `session_id` 保存 Nanobot `chat_id` 映射。
- 增量回答、推理输出、多段输出和回合结束事件。
- 单会话取消、超时清理、网关故障检测与下一任务重启。
- 将后台选择的远程 MCP 和 Skill 同步给 Nanobot 执行。
- API key 配置和 MCP 密钥仅写入运行时临时目录；会话状态写入持久 Volume。

Nanobot 固定源码版本：`455533169d5a641300dd63d260b1ff5543c4093c`。
依赖安装在独立的 `/opt/nanobot` Python 环境中，与 Shim 的依赖分离。
升级时同时更新 Dockerfile、Driver 中的版本常量和本指南，并重新跑协议联调测试。

## 部署与首次使用

```bash
git clone https://github.com/yuklcool/agentdock.git
cd agentdock
make dev
```

`make dev` 构建含 Nanobot 的 Agent 镜像并启动管理后台。镜像构建必须能访问
GitHub 和 Python 包仓库。首次构建需要下载依赖；运行时不会临时下载 Nanobot。

1. 打开 `http://localhost:5173`，使用启动命令输出的管理员账号登录并修改密码。
2. 进入工作空间，在 **Settings → Credentials** 添加 OpenAI 或 Anthropic API key。
3. 新建 Agent，选择 **Nanobot** 驱动及可用模型，设置实例名称、CPU 和内存上限。
4. 如需使用额外能力，在实例配置中选择 MCP 和 Skills。
5. 在该实例的对话页面发送任务；第一次任务会启动容器内的 Nanobot Gateway。
6. 重复创建多个实例。每个实例拥有独立容器和 Volume，内部可以使用相同的 8765 端口。

生产部署参见 [部署指南](../deploy/README.md) 和 [Coolify 指南](../deploy/COOLIFY.md)。
本版本镜像标签为 `0.1.0-nanobot`，控制平面的 `AGENT_IMAGE_TAG` 必须指向本版本构建的镜像。
原有实例不会因为更新控制平面而自动更换镜像；使用镜像更新流程重新创建运行容器，保留原 Volume。

## 组件职责与数据路径

| 组件 | 职责 |
| --- | --- |
| AgentDock Control Plane | 登录、工作空间鉴权、实例管理、任务、资源、审计 |
| Shim | 任务调度、事件持久化、SSE、取消信号 |
| NanobotDriver | 进程启动、配置同步、会话映射、协议适配 |
| Nanobot Gateway | LLM、Agent Loop、工具执行、Memory、MCP、Skills |
| `/v1/ws` | 对外统一连接、实例路由、任务订阅与取消 |

| 数据 | 位置与生命周期 |
| --- | --- |
| 用户文件和 Nanobot Memory | `/workspace/nanobot`，保存在实例 Volume |
| 会话映射 | `/workspace/.agent-runtime/nanobot/sessions`，Shim 管理，文件名由 session_id 哈希生成 |
| Nanobot 会话、WebUI 记录、媒体、Cron、日志目录 | `/workspace/.agent-state/nanobot/data`，保存在实例 Volume |
| 原生运行配置和密钥 | `/home/agent/agentdock-nanobot-*/config.json`，权限 0600，容器 tmpfs，停止/配置更新时清理 |
| 平台分配的 Skills | `/workspace/nanobot/skills/<name>/SKILL.md` |

Nanobot 的工作目录为 `/workspace/nanobot`，会话目录在其外部，满足官方存储隔离要求。
Nanobot 根据配置文件目录定位会话存储。本适配器将临时配置旁的 `sessions`、`webui`、
`media`、`cron`、`logs` 链接到持久目录，避免只保存 chat_id、丢失原生会话历史。
通过平台上传给 Nanobot 使用的文件请放在 `nanobot/` 子目录，Context 文件路径使用该目录内路径。
默认关闭原生 Heartbeat 和 Dream 定时任务，避免任务结束后自动调用模型。
平台普通工作空间下载不包含 `.agent-runtime` 和 `.agent-state`；灾备必须备份完整 Docker Volume。
原生进程的 stdout/stderr 不写入平台事件，避免把 SDK 输出中的认证信息带入任务日志。
启动失败会返回明确错误码，管理员可在受控测试环境使用相同版本排查。

## 并发与配置变化

不同实例可并发工作。当前 Nanobot 实例每次执行一个平台任务；同实例的多个会话分别保存历史，
提交并发任务时平台返回并发限制错误。Driver 自身也串行保护运行配置和会话映射。

下一任务开始前比较模型、密钥、MCP、Skill、环境变量和任务限制。配置变化时重启该实例内的
Nanobot 子进程，再执行新任务；不会在其他实例中重启进程。暂停与恢复由原有容器生命周期完成。
取消任务先向当前 chat 发送 `/stop`，等待停止确认；通信失效或无法确认停止时关闭该实例的网关，
确保已放弃的任务不继续调用工具。下一任务重新启动网关。

## REST 与 SSE

现有接口不变。创建实例后，`agent_id` 即平台返回的容器/实例 id：

```http
POST /v1/containers/{agent_id}/tasks
Authorization: Bearer <工作空间 API key>
Content-Type: application/json

{"prompt":"分析昨晚报警","session_id":"lighting-session-001"}
```

返回 `task_id`，使用以下接口读取事件：

```http
GET /v1/containers/{agent_id}/tasks/{task_id}/events
Accept: text/event-stream
Authorization: Bearer <工作空间 API key>
```

后续消息使用相同 `session_id` 继续会话；新会话使用新 id。省略 session_id 表示独立一次性任务。

## 统一 WebSocket 协议

地址：`wss://你的域名/v1/ws`。开发环境经 Vite 同域代理使用 `ws://localhost:5173/v1/ws`。
浏览器使用 AgentDock 登录 cookie；服务端客户端使用 `Authorization: Bearer ...`。
浏览器必须同源，跨站入口应通过你自己的后端认证代理接入。不要把长期工作空间 API key 写进浏览器代码。
本协议是平台任务协议，并非 Nanobot 原生 WebUI 的完整兼容层。

建立连接后服务端返回：

```json
{"event":"ready","protocol":"agentdock.v1"}
```

发送消息：

```json
{"type":"message","agent_id":"con_example","session_id":"lighting-session-001","content":"分析昨晚报警"}
```

省略 session_id 时由平台生成；保存 `attached` 返回的 session_id 以便继续会话。

```json
{"event":"attached","agent_id":"con_example","session_id":"lighting-session-001","task_id":"tsk_example"}
```

增量输出与结束：

```json
{"event":"delta","agent_id":"con_example","session_id":"lighting-session-001","task_id":"tsk_example","seq":2,"stream_id":"s1","text":"正在分析"}
```

```json
{"event":"turn_end","agent_id":"con_example","session_id":"lighting-session-001","task_id":"tsk_example","seq":6,"status":"completed","result":{"success":true,"output":"分析结果"},"error":null}
```

其他事件包括 `reasoning_delta`、`reasoning_end`、`stream_end`、`message`、`log`、`token_update`。
`stream_end` 只结束一段输出；只能在 `turn_end` 后将整轮任务标记为结束。
`stream_end.text` 是该段的最终完整文本，应替换该段已累积的文本，而不是再追加一次。
按 task_id 和 stream_id 分流，按 seq 去重。`turn_end.status` 可能为 completed / failed / cancelled / timed_out。

取消当前任务：

```json
{"type":"cancel","agent_id":"con_example","task_id":"tsk_example"}
```

断线后重新订阅，不重复提交消息：

```json
{"type":"attach","agent_id":"con_example","task_id":"tsk_example","after_seq":2}
```

断开 WebSocket 仅取消订阅，不取消任务。任务事件仍由平台后台归档。
每个连接最多订阅 8 个任务。15 分钟未发消息的连接会关闭，可发送 `{"type":"ping"}` 保活。

## MCP、Skills 与模型配置

MCP 使用平台现有远程服务器定义：无认证、Bearer 或自定义请求头。Driver 转换为
Nanobot `tools.mcpServers`，工具只由 Nanobot 执行一次。私有数据库或内网 MCP 的网络访问
仍需管理员配置受控通路；默认容器出网策略不会因此关闭。

官方 Nanobot 默认拒绝 MCP 访问私有和回环 IP。确需访问内网服务时，管理员可在部署环境设置
`AGENTDOCK_NANOBOT_SSRF_WHITELIST`，使用空格分隔 CIDR，优先精确服务器 IP
（如 `10.0.0.7/32` 或 IPv6 `/128`）。此设置会传入新建/重建实例的 Shim；
在控制台实例环境变量中填写同名项不能放宽策略。它使用 Nanobot 原生白名单，
会作用于这些 IP 的所有原生网络工具，不仅是 MCP；还需配合 Docker 出网代理规则。
默认空值保留官方防护。修改后重建实例以应用部署配置。

Skills 使用平台已经解析的文本或 Git bundle。更新时只替换本适配器此前管理的目录，取消选择后删除
对应目录；不会清空用户自己建立的其他 Skills。同名用户目录和符号链接会产生冲突错误，需要管理员处理。
平台分配不会禁用 Nanobot 的所有内置技能，Skill 列表不构成数据访问权限边界。

当前模型选择支持目录中的 OpenAI 和 Anthropic API key 模型；不支持订阅 OAuth、Codex 专用模型或
平台未登记的任意模型名称。可在实例环境变量中设置 `AGENTDOCK_NANOBOT_API_BASE`，将兼容模型请求
发到指定 API 地址。模型名称仍须与平台模型目录和该 API 实际提供的模型一致。

平台配置的说明文字和 Context 随每次任务作为上下文传递，不覆盖用户的 AGENTS.md；
它不会替换 Nanobot 原生系统提示词。Nanobot 原生 Memory 和工具体系保持独立。

## 私有实例与运行边界

- 支持同一工作空间内的用户私有实例、模板自动创建和个人 API Key。普通成员只能访问自己的实例
  与共享实例；租户管理员可管理本租户实例。升级前实例保留为共享。详见[运维指南](../deploy/OPERATIONS.md)。
- 超时由 Driver 强制执行；迭代上限和单次生成 token 上限传入 Nanobot。完整 token 用量在回合结束时
  回传，尚不能保证在总 token 配额中途耗尽时即时停止；不要将其作为严格费用控制边界。
- 不声明支持 schema 结构化输出，也没有新增 MCP Apps iframe、完整 Nanobot WebUI 或附件下载协议。
- 提供用户/租户配额、审计、Prometheus 指标与告警模板、加密数据库和 Volume 备份恢复脚本。
  真实生产压测、告警接收地址及灾备切换需在部署环境中验收；不提供自动扩缩容。

## 验证与验收

协议测试使用真实本地 WebSocket 服务，覆盖回合终止、多段流、串线过滤、取消、超时、断线、
会话映射恢复、Skill 选择撤销、密钥临时存放及配置变更重启。统一入口测试覆盖未登录、跨站、
跨租户、任务归属、消息路由和事件转发；前端测试覆盖增量文本合并与推理/回答分流。

```bash
python -m pip install -e 'packages/agentcore[dev]' -e 'services/shim[dev]' \
  -e 'services/control_plane[dev]' -e 'services/connectors[dev]'
PYTHONPATH=packages/agentcore pytest packages/agentcore/tests -m unit -q
PYTHONPATH=services/control_plane:services/control_plane/tests pytest services/control_plane/tests -m unit -q
PYTHONPATH=services/shim pytest services/shim/tests -m unit -q
```

上线前在 Docker 环境验收：创建至少 3 个 Nanobot 实例并发对话，验证跨实例和跨 session 历史隔离；
暂停/恢复后继续相同会话；重建运行容器后确认 Volume 历史保留；取消任务和模拟网关退出；
使用另一个租户身份尝试访问实例、任务和文件；检查备份恢复及数据库权限。


真实网关联调（不需要外部模型 API key）：先在独立虚拟环境安装上述固定版本 Nanobot，并将
`nanobot` 加入 PATH，然后执行：

```bash
AGENTDOCK_TEST_NANOBOT=1 pytest -q packages/agentcore/tests/drivers/test_nanobot_live.py
```

测试使用本地确定性模型和 MCP 端点，验证流式回答、MCP 鉴权与工具注册/撤销、
技能撤销及网关重启后的历史恢复。仅测试进程为本地 MCP 配置 `127.0.0.1/32` 白名单。
GitHub 的 `AgentDock Nanobot` 工作流还会构建实际 Agent 镜像，在 Docker 内验证子进程降权。
本地受限用户命名空间若不能切换 uid，可仅在测试时设置 `AGENTDOCK_TEST_LOCAL_UID=1`；
该模式不验证容器权限边界，不应作为 Docker 验收替代。

完整测试与三实例、数据库、备份恢复验收均已接入 push/PR 自动 CI。执行记录以当前提交的
GitHub Actions 结果为准。生产上线仍需使用自己的模型凭据、域名和容量指标验收。

### Docker 验证记录

2026-09-05，代码提交 `fe5071072698bc38e4b252e7122bccf3dd858102` 的
[GitHub Actions 检查](https://github.com/yuklcool/agentdock/actions/runs/33950259301) 全部通过：
后端适配与路由、前端类型/渲染/构建，以及真实 Agent 镜像构建。
镜像内联调在 root Shim → agent uid 的实际权限边界下运行，验证了流式回答、网关重启后的
历史恢复和子进程 uid（1 项联调通过）。该镜像检查使用 `VARIANT=slim`。
此结果不替代上面的三实例并发和生产部署验收。
