"""Tenant-scoped, allowlisted audit presentation. Never return raw details or task bodies."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.models_db import containers, tasks, tenants
from control_plane.tables import memberships, users

ACTION_LABELS = {
    "task.submitted": "提交任务",
    "container.create": "创建容器",
    "container.recover": "恢复容器",
    "container.reconciled": "校准容器状态",
    "container.force_pause": "强制暂停容器",
    "container.destroy": "归档容器",
    "container.restore": "恢复容器存储",
    "container.delete": "删除容器",
    "container.reclaim": "回收闲置容器",
    "container.update_env": "修改容器环境",
    "container.update_image": "更新容器镜像",
    "container.update_resources": "修改容器资源",
    "user.create": "创建用户",
    "user.rename": "修改用户姓名",
    "user.password_reset": "重置用户密码",
    "membership.add": "添加工作空间成员",
    "membership.role_change": "修改成员角色",
    "membership.disable": "停用工作空间成员",
    "membership.enable": "启用工作空间成员",
    "staff.impersonate": "切换工作空间",
    "staff.impersonate.exit": "退出工作空间切换",
    "staff.status": "修改平台人员状态",
    "policy.update": "修改配额",
    "tenant.create": "创建工作空间",
    "tenant.update_limits": "修改工作空间限额",
    "tenant.disable": "停用工作空间",
    "api_key.create": "创建 API 密钥",
    "api_key.revoke": "撤销 API 密钥",
    "credential.store": "保存模型凭据",
    "credential.remove": "删除模型凭据",
    "credential.endpoint_update": "修改模型服务地址",
    "credential.oauth_start": "开始模型授权",
    "credential.refresh": "刷新模型凭据",
    "credential.oauth_complete": "完成模型授权",
    "console.session.open": "打开终端",
    "console.session.close": "关闭终端",
    "volume.over_limit": "存储超出限额",
}
TARGET_LABELS = {
    "container": "容器",
    "task": "任务",
    "user": "用户",
    "tenant": "工作空间",
    "credential": "模型凭据",
    "api_key": "API 密钥",
}
# These events are appended after the named action succeeds. A submitted task is
# successfully accepted, not necessarily successfully executed. Unknown events
# must not acquire an invented success status.
SUCCESS_ACTIONS = set(ACTION_LABELS) - {
    "staff.impersonate",
    "staff.impersonate.exit",
    "container.reconciled",
    "volume.over_limit",
}


async def enrich_events(session: AsyncSession, tid: str, rows: list[Any]) -> list[dict[str, Any]]:
    if not rows:
        return []
    actor_ids = {r["actor_id"] for r in rows if r["actor_id"]}
    user_ids = actor_ids | {r["target_id"] for r in rows if r["target_type"] == "user"}
    member_ids = sa.select(memberships.c.user_id).where(memberships.c.tenant_id == tid)
    # account is deliberately nullable until the user model adds it. Do not
    # manufacture an immutable account from an email address or technical ID.
    account = users.c.get("account", sa.cast(sa.null(), sa.Text)).label("account")
    people_rows = (
        (
            await session.execute(
                sa.select(users.c.id, users.c.name, users.c.email, account, users.c.is_staff).where(
                    users.c.id.in_(user_ids),
                    sa.or_(
                        users.c.id.in_(member_ids),
                        sa.and_(users.c.is_staff.is_(True), users.c.id.in_(actor_ids)),
                    ),
                )
            )
        )
        .mappings()
        .all()
    )
    people = {r["id"]: {k: r[k] for k in ("id", "name", "email", "account")} for r in people_rows}
    # Staff may appear as an actor without workspace membership, but that is not
    # permission to resolve arbitrary staff accounts as user targets.
    target_people = {r["id"]: people[r["id"]] for r in people_rows if not r["is_staff"]}
    task_ids = {r["target_id"] for r in rows if r["target_type"] == "task"}
    task_rows = (
        (
            await session.execute(
                sa.select(tasks.c.id, tasks.c.container_id).where(
                    tasks.c.tenant_id == tid, tasks.c.id.in_(task_ids)
                )
            )
        )
        .mappings()
        .all()
    )
    task_map = {r["id"]: r["container_id"] for r in task_rows}
    container_ids = set(task_map.values()) | {
        r["target_id"] for r in rows if r["target_type"] == "container"
    }
    for row in rows:
        details = row["details"]
        if isinstance(details, dict) and isinstance(details.get("container_id"), str):
            container_ids.add(details["container_id"])
    container_rows = (
        (
            await session.execute(
                sa.select(containers.c.id, containers.c.name).where(
                    containers.c.tenant_id == tid, containers.c.id.in_(container_ids)
                )
            )
        )
        .mappings()
        .all()
    )
    container_map = {r["id"]: dict(r) for r in container_rows}
    tenant_name = (
        await session.execute(sa.select(tenants.c.name).where(tenants.c.id == tid))
    ).scalar_one_or_none()
    result = []
    for row in rows:
        action, kind, target_id = row["action"], row["target_type"], row["target_id"]
        details = row["details"] if isinstance(row["details"], dict) else {}
        container_id = (
            target_id
            if kind == "container"
            else task_map.get(target_id)
            if kind == "task"
            else details.get("container_id")
        )
        container = container_map.get(container_id) if isinstance(container_id, str) else None
        name = None
        if kind == "container" and container:
            name = container["name"]
        elif kind == "user":
            name = target_people.get(target_id, {}).get("name")
        elif kind == "tenant" and target_id == tid:
            name = tenant_name
        elif kind == "task" and container:
            # tasks have no business title; their body is a sensitive prompt.
            name = f"{container['name']}的任务"
        actor = people.get(row["actor_id"])
        if actor is None:
            actor = {
                "id": row["actor_id"],
                "name": "系统"
                if row["actor_type"] == "system"
                else "工作空间 API"
                if row["actor_type"] == "tenant" and not row["actor_id"]
                else "用户信息不可用",
                "account": None,
                "email": None,
            }
        # Explicit response allowlist: raw details, prompts, tokens, credentials,
        # task results and errors can never be serialized by accident.
        result.append(
            {
                "id": row["id"],
                "ts": row["ts"],
                "action": action,
                "action_label": ACTION_LABELS.get(action, "其他操作"),
                "actor_type": row["actor_type"],
                "actor_id": row["actor_id"],
                "actor": actor,
                "target_type": kind,
                "target_id": target_id,
                "target": {
                    "type": kind,
                    "id": target_id,
                    "type_label": TARGET_LABELS.get(kind, "资源"),
                    "name": name or f"{TARGET_LABELS.get(kind, '资源')}信息不可用",
                },
                "container": container,
                "tenant_id": tid,
                "status": "success" if action in SUCCESS_ACTIONS else None,
                "status_label": "已提交"
                if action == "task.submitted"
                else "成功"
                if action in SUCCESS_ACTIONS
                else "—",
            }
        )
    return result
