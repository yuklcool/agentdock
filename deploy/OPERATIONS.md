# AgentDock 运维指南

AgentDock 按用户提供独立 Nanobot 实例，使用 PostgreSQL 保存归属、任务和会话映射，使用独立 Docker Volume 保存工作区及 Nanobot 原生历史。运行镜像固定用户提供的 Nanobot 基础镜像摘要，不修改其中的 Nanobot 源码。

## 升级与用户隔离

部署包升级：保留 `.env` 和数据卷，将 `AGENTDOCK_VERSION` 改为 `0.3.6` 后执行 `docker compose --profile images pull`、`docker compose up -d --no-build --wait`。启动流程自动执行数据库迁移。源码部署则先备份，在停止旧控制平面服务后使用新版代码执行 `alembic upgrade head`，再启动新版服务。

`0032_remove_legacy_bindings` 会删除废弃的用户绑定密钥和模板绑定表，再移除 API Key 表上对应的归属字段。工作空间密钥、容器归属、任务和数据卷保持不变。此清理不可恢复旧密钥；若需回滚旧程序，必须恢复升级前数据库备份，不能依靠 downgrade 重建已删除数据。

用户登录后，在 Fleet → New 创建实例，可选择已有 Nanobot 模板；管理员可选择所属用户。未绑定容器可创建多个；同一工作空间内每位用户最多绑定一个非 destroyed 容器，重复创建返回 409 `user_container_already_bound`。已有实例仍从容器列表进入，暂停或归档的实例使用原有生命周期操作恢复。


管理员创建默认“不绑定”，普通成员创建默认绑定自己，且不能指定其他用户或创建共享实例。普通成员只可访问自己的实例及明确共享的实例；租户管理员可以管理本租户全部实例。实例、任务、SSE、WebSocket、文件、Git、终端、工作流目标、定时任务和统计查询使用同一归属策略。

升级前的实例保留为共享实例，避免擅自指定所有者。需要收紧旧实例时，应由管理员迁移数据到新建私有实例。禁止直接删除用户使私有实例自动变成共享资源；回滚到不支持私有资源的版本应恢复升级前备份。

设置中的 API keys 页面仅管理工作空间密钥。密钥表示可信的空间级服务身份，不携带用户身份，也不根据 `created_by` 继承创建者权限。可以调用本空间内公共和用户绑定容器的任务、会话及事件接口。`GET /v1/containers` 默认仅列未绑定容器；加 `owner_user_id` 可查该用户的唯一非 destroyed 绑定容器。其他工作空间始终不可访问；普通用户登录会话的归属权限不变。只有管理员/Owner 的登录会话或 Staff 能管理工作空间密钥，密钥自身不能管理用户、密钥或配额。完整密钥仅在创建时显示一次。


自动化保存创建者或最后编辑者的身份与权限上限。每次定时执行和工作流下一步执行前重新检查用户状态、成员资格和目标归属；提高用户角色不会自动提高原有低权限自动化的权限。

## 配额与审计

管理员打开设置中的“配额与审计”，可查看固定为 1 的每人绑定容器数，并配置工作空间每日任务/Token 预算、每人每日任务/Token 预算。每日项为 0 表示不限额，按 UTC 自然日重置。实例数与运行名额先在数据库事务中预留，再执行 Docker 操作；冷启动不会一直占用数据库连接。

任务准入使用 PostgreSQL 事务锁。正在执行的任务先预留其 `max_tokens`，完成后改用实际用量，避免同时提交多个任务突破每日准入预算。实例忙时返回 429；实例容量不足返回 409/503，可暂停闲置实例后重试。

Token 预算是任务准入约束。Nanobot 的模型调用可能在当前回合结束时才报告实际用量，因此不是服务商账单的严格硬上限；需要同时在模型服务商设置费用限额。CPU、内存、进程数沿用 Docker 限制。磁盘容量应由宿主机存储配额和监控约束。

`GET /v1/operations/audit` 提供本租户最近操作记录，不返回任务提示词或凭据。`PUT /v1/operations/policy` 更新配额并记录审计。生产中应通过日志采集器将审计和宿主机日志归档到持久存储。

## 监控与告警

`GET /healthz` 检查控制平面与数据库。`GET /v1/operations/metrics` 需要平台 staff 身份，提供实例状态数、任务状态数和超过一小时的未完成任务数；不带用户、提示词或密钥标签。

`deploy/monitoring/prometheus.yml` 和 `alerts.yml` 提供 Prometheus 抓取与规则模板。将管理员监控凭据保存到只读 secret 文件；根据实际部署修改地址。告警覆盖 API 不可用、实例错误、长时间未完成任务。将规则接入自己的 Alertmanager 接收配置后才会发送通知。超过一小时的合法长任务应调整告警阈值。

还应采集 Docker/宿主机 CPU、内存、磁盘、OOM 和反向代理连接指标；只向运维网络开放 Prometheus，公共入口使用 HTTPS/WSS。Nanobot Gateway 仅在实例内监听 `127.0.0.1:8765`，不要映射到宿主机公网。

## 加密备份与恢复

宿主机需要 Python 3.12、Docker CLI、`age`。先执行 `docker pull alpine:3.21`。将 age 私钥与平台原始加密密钥分别保存在受控的异地位置；备份文件不包含部署环境变量或加密密钥。

```bash
age-keygen -o agentdock-backup.key
# 将上一步输出的公钥用于备份
python scripts/backup.py backup --postgres agent-runtime-postgres-1 \
  --recipient age1你的备份公钥 --output /secure-backups/agentdock-20260906.age
```

脚本识别 PostgreSQL 所属 Compose 项目的全部运行中控制平面副本，先停止它们，再冻结已登记的运行中实例，随后备份数据库和完整 Volume，最后恢复原来的运行状态。备份会造成短暂维护窗口，应选择低峰期。不得同时运行其他写入数据库或 Volume 的维护工具。

归档包含校验和、数据库 dump、实例清单和完整 Volume（包括 `.agent-state`、会话、记忆、技能和工作区文件），使用 age 加密输出。临时目录权限为 0700，输出不覆盖已有文件。备份失败也会尝试恢复所有被停止/冻结的容器；如果 Docker 本身故障，按错误提示手动恢复。

恢复必须在隔离的干净主机或 Docker 环境中，目标数据库为空、同名 Volume 不存在、控制平面已停止：

```bash
python scripts/backup.py restore --postgres agentdock-restore-postgres \
  --identity /secure/agentdock-backup.key --input /secure-backups/agentdock-20260906.age
```

脚本检查加密归档、文件路径、校验和及目标状态，恢复数据库和文件 UID/GID/权限。恢复失败保留现场供诊断，不覆盖已有数据。完成后安装原平台加密密钥、相同运行镜像和部署配置，再启动 AgentDock，让生命周期管理恢复实例。检查原会话续聊、凭据可解密、私有实例跨用户拒绝访问，再切换业务流量。

CI 使用 `python scripts/test_backup.py` 在一次性 PostgreSQL 与 Volume 上执行加密备份/恢复，验证会话文件内容、UID/GID、权限和数据库记录。它不代替你的生产数据规模和 RTO/RPO 演练。

## 验收命令

```bash
python scripts/test_nanobot_instances.py --image agentdock-agent:test
```

三个真实 Docker 容器同时运行固定版本 Nanobot，各用独立 Volume，验证会话隔离、MCP 鉴权及工具撤销、技能撤销、Docker 暂停/恢复、网关重启和删除运行容器后的同卷恢复。测试使用本地确定性模型端点，无需真实付费 API Key。单实例串行执行符合 Nanobot 会话与工作区写入语义；扩容通过增加实例实现。

PostgreSQL 集成测试使用 `AGENTDOCK_TEST_DATABASE_URL` 指向一次性数据库，先运行完整迁移，再执行 `services/control_plane/tests/test_private_instances.py`。自动 CI 还执行全量后端单元测试、前端测试、类型检查、Lint 和构建。较广的旧服务集成套件保留在 CI 手动运行入口。


## 0.3.5 唯一绑定迁移预检

`0033_single_user_container` 在锁定容器写入后检查重复绑定，再建立部分唯一索引；不自动解绑、共享、删除容器或迁移历史。归档、暂停、错误和创建中的容器仍占用名额；只有 `destroyed` 或永久删除才释放。旧 `max_private_containers_per_user` 配额统一显示为固定 1，无法再放宽。

升级前可在数据库运行只读检查：

```sql
SELECT tenant_id, owner_user_id, array_agg(id ORDER BY id) AS container_ids
FROM containers
WHERE owner_user_id IS NOT NULL AND status <> 'destroyed'
GROUP BY tenant_id, owner_user_id
HAVING count(*) > 1;
```

若有冲突，迁移停止并列出前 20 组工作空间、用户及容器 ID，原数据和归属保持不变。先备份数据库与数据卷，使用旧版本导出需要保留的工作文件和历史，再明确选定每组保留的一个容器，通过旧版永久删除其余容器后重试升级。仅点击归档或暂停不能消除冲突。不要只把状态改成 destroyed 来绕过检查，因为这不会清理实际运行资源。

工作空间密钥现在拥有同租户绑定容器的服务访问能力。请仅放在可信服务端，不应分发给最终用户；面向最终用户的前端继续使用用户登录会话。
