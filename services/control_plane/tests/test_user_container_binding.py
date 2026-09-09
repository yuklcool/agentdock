"""Single binding invariant and workspace service access on real PostgreSQL."""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from test_private_instances import database as shared_database
from test_private_instances import owner_creation as shared_creation

from agentcore.models import AgentConfig
from control_plane import lifecycle
from control_plane.access import assert_container_access, bind_principal
from control_plane.app import create_app
from control_plane.auth.principal import Principal
from control_plane.config import Settings
from control_plane.errors import APIError
from control_plane.models_db import containers, events, tasks
from control_plane.routers import containers as routes
from control_plane.schemas import CreateContainerRequest
from control_plane.tenant_defaults import merge_limits

database = shared_database
owner_creation = shared_creation
migration = importlib.import_module("control_plane.migrations.versions.0033_single_user_container")


@pytest.mark.unit
def test_workspace_key_access_is_tenant_scoped_without_expanding_sessions():
    for method, tenant, allowed in [
        ("api_key", "a", True),
        ("api_key", "b", False),
        ("session", "a", False),
        ("bootstrap", "a", False),
    ]:
        p = Principal(
            tenant_id=tenant, user_id=None, role="member", is_staff=False, auth_method=method
        )
        row = SimpleNamespace(tenant_id="a", owner_user_id="alice")
        if allowed:
            assert_container_access(row, p)
        else:
            with pytest.raises(APIError):
                assert_container_access(row, p)
    assert (
        merge_limits({"max_private_containers_per_user": 99})["max_private_containers_per_user"]
        == 1
    )
    params = create_app(Settings.from_env()).openapi()["paths"]["/v1/containers"]["get"][
        "parameters"
    ]
    assert any(p["name"] == "owner_user_id" and p["in"] == "query" for p in params)


async def test_unbound_default_and_explicit_null_allow_multiple(database, owner_creation):
    factory, tid, alice, _, _ = database
    routes, request, calls = owner_creation
    p = Principal(tenant_id=tid, user_id=alice, role="admin", is_staff=False)
    for extra in ({}, {"owner_user_id": None}, {"owner_user_id": None}):
        async with factory() as db:
            result = await routes.create_container(
                request,
                CreateContainerRequest(
                    name="unbound",
                    config=AgentConfig(driver="nanobot", model="gpt-4o"),
                    **extra,
                ),
                p,
                db,
            )
            assert result["owner_user_id"] is None
    assert len(calls) == 3


@pytest.mark.parametrize("status", ["running", "paused", "archived", "error", "provisioning"])
async def test_every_non_destroyed_binding_rejects_duplicate(database, owner_creation, status):
    factory, tid, alice, bob, cids = database
    routes, request, calls = owner_creation
    p = Principal(tenant_id=tid, user_id=bob, role="admin", is_staff=False)
    async with factory() as db:
        await db.execute(
            containers.update().where(containers.c.id == cids[0]).values(status=status)
        )
        with pytest.raises(APIError) as exc:
            await routes.create_container(
                request,
                CreateContainerRequest(
                    name="duplicate",
                    owner_user_id=alice,
                    config=AgentConfig(driver="nanobot", model="gpt-4o"),
                ),
                p,
                db,
            )
        assert (exc.value.status_code, exc.value.code) == (409, "user_container_already_bound")
        assert not calls


async def test_unique_index_and_safe_duplicate_migration(database):
    factory, tid, alice, _, cids = database
    async with factory() as db:
        with pytest.raises(sa.exc.IntegrityError, match="uq_container_owner_per_tenant"):
            async with db.begin_nested():
                await db.execute(
                    containers.update()
                    .where(containers.c.id == cids[2])
                    .values(owner_user_id=alice)
                )
        # Model an existing database with duplicates; all DDL stays in a rollback.
        await db.execute(sa.text("DROP INDEX uq_container_owner_per_tenant"))
        await db.execute(
            containers.update().where(containers.c.id == cids[2]).values(owner_user_id=alice)
        )

        def upgrade(connection):
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()

        connection = await db.connection()
        with pytest.raises(RuntimeError, match="No ownership or data was changed") as exc:
            await connection.run_sync(upgrade)
        assert tid in str(exc.value) and alice in str(exc.value) and cids[2] in str(exc.value)
        await db.execute(
            containers.update().where(containers.c.id == cids[0]).values(status="destroyed")
        )
        await connection.run_sync(upgrade)
        # Successfully recreated a real unique index without altering ownership.
        assert (
            await db.execute(
                sa.select(containers.c.owner_user_id).where(containers.c.id == cids[0])
            )
        ).scalar_one() == alice
        await db.rollback()


async def test_same_user_in_two_workspaces_and_cross_tenant_denial(database):
    from control_plane import tables as t

    factory, tid, alice, _, cids = database
    other = tid + "other"
    async with factory() as db:
        await db.execute(t.tenants.insert().values(id=other, name=other, limits={}))
        await db.execute(
            t.memberships.insert().values(
                id=other, tenant_id=other, user_id=alice, role="member", status="active"
            )
        )
        row = dict(
            (await db.execute(sa.select(containers).where(containers.c.id == cids[0])))
            .mappings()
            .one()
        )
        row.update(id=other, tenant_id=other, docker_name=other, volume_name=other)
        await db.execute(containers.insert().values(**row))
        p = Principal(
            tenant_id=other, user_id=None, role="member", is_staff=False, auth_method="api_key"
        )
        bind_principal(db, p)
        listing = await routes.list_containers(None, p, db, owner_user_id=alice)
        assert [r["id"] for r in listing["containers"]] == [other]
        with pytest.raises(APIError) as exc:
            await routes.get_container(cids[0], None, p, db)
        assert exc.value.status_code == 404
        await db.rollback()


async def test_destroyed_purge_does_not_reacquire_binding(database, monkeypatch):
    factory, _, alice, _, cids = database
    for name in ("stop", "rm", "volume_rm"):
        monkeypatch.setattr(lifecycle.docker_ctl, name, AsyncMock())
    async with factory() as db:
        await db.execute(
            containers.update().where(containers.c.id == cids[0]).values(status="destroyed")
        )
        await db.execute(
            containers.update().where(containers.c.id == cids[2]).values(owner_user_id=alice)
        )
        assert await lifecycle.delete(db, None, AsyncMock(), cids[0])
        assert (
            await db.execute(
                sa.select(containers.c.owner_user_id).where(containers.c.id == cids[2])
            )
        ).scalar_one() == alice
        await db.rollback()


async def test_http_workspace_lookup_sessions_tasks_events_and_member_boundary(
    database, monkeypatch
):
    factory, tid, alice, bob, cids = database
    app = create_app(Settings.from_env())
    app.state.session_factory = factory
    app.state.settings = Settings.from_env()
    from control_plane.routers import tasks as task_routes

    monkeypatch.setattr(lifecycle, "bring_to_running", AsyncMock())
    monkeypatch.setattr(
        task_routes,
        "resolve_task_credential",
        AsyncMock(return_value=("test-key", "api_key", {}, "api_key")),
    )
    monkeypatch.setattr(
        task_routes, "forward_to_shim", AsyncMock(return_value={"status": "running"})
    )
    monkeypatch.setattr(task_routes, "_ingest_events_to_db", AsyncMock())
    key = Principal(
        tenant_id=tid, user_id=None, role="member", is_staff=False, auth_method="api_key"
    )
    current = key
    app.dependency_overrides[routes._principal] = lambda: current
    async with factory() as db:
        await db.execute(
            tasks.insert().values(
                id=cids[0] + "task",
                tenant_id=tid,
                container_id=cids[0],
                status="completed",
                driver="nanobot",
                model="gpt-4o",
                body={"prompt": "test"},
                config_snapshot={"driver": "nanobot", "model": "gpt-4o"},
                session_id="ses_test",
            )
        )
        await db.execute(
            events.insert().values(
                task_id=cids[0] + "task", seq=1, type="stream_delta", payload={"text": "hello"}
            )
        )
        await db.commit()
    prefix = f"/v1/containers/{cids[0]}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        default = (await client.get("/v1/containers")).json()["containers"]
        assert {r["id"] for r in default} == {cids[2]}
        selected = await client.get("/v1/containers", params={"owner_user_id": alice})
        assert [r["id"] for r in selected.json()["containers"]] == [cids[0]]
        assert (await client.get("/v1/containers", params={"owner_user_id": "missing"})).json()[
            "containers"
        ] == []
        paths = [
            prefix,
            prefix + "/tasks",
            prefix + "/sessions",
            prefix + f"/tasks/{cids[0]}task/events",
        ]
        for path in paths:
            response = await client.get(path)
            assert response.status_code == 200, response.text
        submitted = await client.post(prefix + "/tasks", json={"prompt": "hello"})
        assert submitted.status_code == 200, submitted.text
        async with factory() as db:
            actor = (
                await db.execute(
                    sa.select(tasks.c.submitted_by).where(tasks.c.id == submitted.json()["task_id"])
                )
            ).scalar_one()
            assert actor is None  # service identity never impersonates the bound user
        current = Principal(tenant_id=tid, user_id=bob, role="member", is_staff=False)
        assert (await client.get("/v1/containers", params={"owner_user_id": alice})).json()[
            "containers"
        ] == []
        assert (await client.post(prefix + "/tasks", json={"prompt": "denied"})).status_code == 404
        for path in paths:
            assert (await client.get(path)).status_code == 404
        current = Principal(
            tenant_id=tid + "other",
            user_id=None,
            role="member",
            is_staff=False,
            auth_method="api_key",
        )
        assert (await client.post(prefix + "/tasks", json={"prompt": "denied"})).status_code == 404
        for path in paths:
            assert (await client.get(path)).status_code == 404
