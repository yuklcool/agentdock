"""Audit presentation: safe fields, scoped joins and the existing admin gate."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from test_private_instances import database as shared_database

from control_plane import tables as t
from control_plane.audit_view import ACTION_LABELS, enrich_events
from control_plane.auth.principal import Principal, resolve_principal
from control_plane.models_db import audit_log, containers, tasks
from control_plane.routers import operations

database = shared_database


@pytest.mark.unit
@pytest.mark.parametrize(
    "action,label",
    [
        ("task.submitted", "提交任务"),
        ("container.create", "创建容器"),
        ("user.create", "创建用户"),
        ("staff.impersonate", "切换工作空间"),
        ("container.recover", "恢复容器"),
        ("container.reconciled", "校准容器状态"),
        ("policy.update", "修改配额"),
    ],
)
def test_chinese_actions(action, label):
    assert ACTION_LABELS[action] == label


@pytest.mark.unit
@pytest.mark.parametrize(
    "role,method,staff,expected",
    [
        ("member", "session", False, 403),
        ("admin", "api_key", False, 403),
        ("admin", "session", False, 200),
        ("owner", "session", False, 200),
        ("member", "session", True, 200),
    ],
)
async def test_audit_admin_gate(monkeypatch, role, method, staff, expected):
    app = FastAPI()
    from control_plane.errors import APIError, api_error_handler

    app.add_exception_handler(APIError, api_error_handler)
    app.include_router(operations.router, prefix="/v1")
    principal = Principal(
        tenant_id="tenant", user_id="user", role=role, is_staff=staff, auth_method=method
    )
    app.dependency_overrides[resolve_principal] = lambda: principal
    session = AsyncMock()
    from unittest.mock import MagicMock

    result = MagicMock()
    result.mappings.return_value.all.return_value = []
    session.execute.return_value = result
    app.dependency_overrides[operations._session] = lambda: session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/operations/audit")
    assert response.status_code == expected
    if expected == 403:
        session.execute.assert_not_awaited()


@pytest.mark.unit
async def test_empty_history_does_not_query_enrichment():
    session = AsyncMock()
    assert await enrich_events(session, "tenant", []) == []
    session.execute.assert_not_awaited()


async def test_scoped_enrichment_and_sensitive_data_never_leave_api(database):
    factory, tid, alice, bob, cids = database
    other, outsider, foreign_container = tid + "other", alice + "other", cids[0] + "other"
    async with factory() as db:
        await db.execute(t.users.update().where(t.users.c.id == alice).values(name="张三"))
        await db.execute(
            containers.update().where(containers.c.id == cids[0]).values(name="照明智能体")
        )
        await db.execute(
            t.tenants.insert().values(id=other, name="FOREIGN TENANT", limits={}, status="active")
        )
        await db.execute(
            t.users.insert().values(
                id=outsider,
                name="FOREIGN USER",
                email="foreign@test.invalid",
                password_hash="PASSWORD_SECRET",
                status="active",
                is_staff=False,
                must_change_password=False,
            )
        )
        await db.execute(
            t.memberships.insert().values(
                id=outsider, user_id=outsider, tenant_id=other, role="member", status="active"
            )
        )
        await db.execute(
            containers.insert().values(
                id=foreign_container,
                tenant_id=other,
                name="FOREIGN CONTAINER",
                docker_name=foreign_container,
                volume_name=foreign_container,
                shim_token="SHIM_SECRET",
                image_tag="test",
                config={},
                status="running",
            )
        )
        task_id = tid + "task"
        await db.execute(
            tasks.insert().values(
                id=task_id,
                tenant_id=tid,
                container_id=cids[0],
                driver="nanobot",
                status="failed",
                body={"prompt": "PROMPT_SECRET"},
                config_snapshot={"api_key": "KEY_SECRET"},
                result={"text": "RESULT_SECRET"},
                error_message="ERROR_SECRET",
            )
        )
        entries = [
            ("task.submitted", "task", task_id, alice, {"tenant_id": tid, "token": "TOKEN_SECRET"}),
            ("container.create", "container", cids[0], alice, {}),  # legacy tenant fallback
            ("user.create", "user", bob, alice, {"tenant_id": tid, "password": "PASSWORD_SECRET"}),
            (
                "policy.update",
                "tenant",
                tid,
                alice,
                {"tenant_id": tid, "credential": "CREDENTIAL_SECRET"},
            ),
            ("future.action", "container", "deleted", "deleted", {"tenant_id": tid}),
            # Never resolve another workspace's resources even in a malformed scoped event.
            ("container.create", "container", foreign_container, outsider, {"tenant_id": tid}),
            ("user.create", "user", outsider, outsider, {"tenant_id": tid}),
            # Explicit tenant ownership takes precedence over the legacy container fallback.
            ("foreign.only", "container", cids[0], outsider, {"tenant_id": other}),
            ("foreign.only", "container", foreign_container, outsider, {}),
        ]
        for action, kind, target, actor, details in entries:
            await db.execute(
                audit_log.insert().values(
                    action=action,
                    target_type=kind,
                    target_id=target,
                    actor_type="admin",
                    actor_id=actor,
                    details=details,
                )
            )
        p = Principal(tenant_id=tid, user_id=alice, role="admin", is_staff=False)
        result = await operations.audit_history(p, db, 100)
        events = result["events"]
        assert len(events) == 7
        event = next(e for e in events if e["action"] == "task.submitted")
        assert event["actor"]["name"] == "张三"
        assert event["actor"]["account"] is None
        assert event["actor"]["email"] == alice + "@test.invalid"
        assert event["target"]["name"] == "照明智能体的任务"
        assert event["container"] == {"id": cids[0], "name": "照明智能体"}
        assert event["status_label"] == "已提交"  # not the task's current failed status
        assert next(e for e in events if e["action"] == "policy.update")["target"]["name"] == tid
        assert (
            next(e for e in events if e["action"] == "user.create" and e["target_id"] == bob)[
                "target"
            ]["name"]
            == bob
        )
        unknown = next(e for e in events if e["action"] == "future.action")
        assert unknown["action_label"] == "其他操作" and unknown["status"] is None
        assert unknown["actor"]["name"] == "用户信息不可用"
        assert all("details" not in e for e in events)
        payload = str(result)
        for secret in (
            "PROMPT_SECRET",
            "KEY_SECRET",
            "RESULT_SECRET",
            "ERROR_SECRET",
            "TOKEN_SECRET",
            "CREDENTIAL_SECRET",
            "PASSWORD_SECRET",
            "SHIM_SECRET",
            "FOREIGN USER",
            "FOREIGN CONTAINER",
            "FOREIGN TENANT",
            "foreign@test.invalid",
            "foreign.only",
        ):
            assert secret not in payload
        latest = await operations.audit_history(p, db, 2)
        assert [e["id"] for e in latest["events"]] == [e["id"] for e in events[:2]]
        # Inserts above deliberately remain uncommitted and roll back with this session.


async def test_staff_actor_and_system_and_removed_members(database):
    factory, tid, alice, bob, cids = database
    async with factory() as db:
        await db.execute(
            t.users.update().where(t.users.c.id == bob).values(is_staff=True, name="平台管理员")
        )
        await db.execute(t.memberships.delete().where(t.memberships.c.user_id == bob))
        await db.execute(
            t.memberships.update().where(t.memberships.c.user_id == alice).values(status="disabled")
        )
        now = datetime.now(UTC)
        rows = [
            {
                "id": 1,
                "ts": now,
                "action": "staff.impersonate",
                "actor_type": "staff",
                "actor_id": bob,
                "target_type": "tenant",
                "target_id": tid,
                "details": {"tenant_id": tid},
            },
            {
                "id": 2,
                "ts": now,
                "action": "container.recover",
                "actor_type": "system",
                "actor_id": None,
                "target_type": "container",
                "target_id": cids[0],
                "details": None,
            },
            {
                "id": 3,
                "ts": now,
                "action": "membership.disable",
                "actor_type": "admin",
                "actor_id": bob,
                "target_type": "user",
                "target_id": alice,
                "details": {"tenant_id": tid},
            },
        ]
        events = await enrich_events(db, tid, rows)
        assert events[0]["actor"]["name"] == "平台管理员"
        assert events[0]["status_label"] == "—"
        assert events[1]["actor"]["name"] == "系统"
        assert events[1]["status_label"] == "成功"
        assert events[2]["target"]["name"] == alice
