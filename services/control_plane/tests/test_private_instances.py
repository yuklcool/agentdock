"""Ownership and atomic admission exercised on PostgreSQL (CI service).

Set AGENTDOCK_TEST_DATABASE_URL to an expendable, migrated database.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane import tables as t
from control_plane.access import (
    actor_principal,
    assert_container_access,
    assert_target_access,
    bind_principal,
    visible_containers,
    visible_steps,
)
from control_plane.admission import admit_task, check_budget
from control_plane.auth.principal import Principal
from control_plane.errors import APIError
from control_plane.models_db import containers, tasks, workflows
from control_plane.routers.personal_agents import binding_key


@pytest.mark.unit
@pytest.mark.parametrize(
    "tenant,user,role,owner,allowed",
    [
        ("a", "alice", "member", "alice", True),
        ("a", "bob", "member", "alice", False),
        ("a", None, "member", "alice", False),
        ("a", None, "member", None, True),
        ("a", "bob", "admin", "alice", True),
        ("b", "alice", "owner", "alice", False),
    ],
)
def test_instance_access(tenant, user, role, owner, allowed):
    p = Principal(tenant_id=tenant, user_id=user, role=role, is_staff=False)
    row = SimpleNamespace(tenant_id="a", owner_user_id=owner)
    if allowed:
        assert_container_access(row, p)
    else:
        with pytest.raises(APIError) as exc:
            assert_container_access(row, p)
        assert exc.value.status_code == 404


@pytest.mark.unit
def test_budget_reserves_active_tasks_and_binding_is_scoped():
    with pytest.raises(APIError) as exc:
        check_budget(used=20, reserved=70, requested=11, budget=100, code="budget")
    assert exc.value.status_code == 429
    check_budget(used=20, reserved=70, requested=10, budget=100, code="budget")
    assert len({binding_key("a", u, tpl) for u in ("alice", "bob") for tpl in ("x", "y")}) == 4
    assert binding_key("a", "alice", "x") != binding_key("b", "alice", "x")


@pytest_asyncio.fixture
async def database():
    url = os.getenv("AGENTDOCK_TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires expendable migrated PostgreSQL; enabled in CI")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    prefix = uuid.uuid4().hex
    tid, alice, bob = [prefix + suffix for suffix in ("tenant", "alice", "bob")]
    async with factory() as db:
        await db.execute(t.tenants.insert().values(id=tid, name=tid, limits={}, status="active"))
        for uid in (alice, bob):
            await db.execute(
                t.users.insert().values(
                    id=uid,
                    name=uid,
                    email=uid + "@test.invalid",
                    password_hash="unused",
                    is_staff=False,
                    must_change_password=False,
                    status="active",
                )
            )
            await db.execute(
                t.memberships.insert().values(
                    id=uid, user_id=uid, tenant_id=tid, role="member", status="active"
                )
            )
        cids = [prefix + suffix for suffix in ("a", "b", "shared")]
        for cid, owner in zip(cids, (alice, bob, None), strict=True):
            await db.execute(
                containers.insert().values(
                    id=cid,
                    tenant_id=tid,
                    name=cid,
                    docker_name=cid,
                    volume_name=cid,
                    shim_token="unused",
                    image_tag="test",
                    owner_user_id=owner,
                    config={"driver": "nanobot", "model": "gpt-4o"},
                    status="running",
                )
            )
        await db.commit()
    try:
        yield factory, tid, alice, bob, cids
    finally:
        async with factory() as db:
            await db.execute(tasks.delete().where(tasks.c.tenant_id == tid))
            await db.execute(workflows.delete().where(workflows.c.tenant_id == tid))
            await db.execute(containers.delete().where(containers.c.tenant_id == tid))
            await db.execute(t.memberships.delete().where(t.memberships.c.tenant_id == tid))
            await db.execute(t.users.delete().where(t.users.c.id.in_((alice, bob))))
            await db.execute(t.tenants.delete().where(t.tenants.c.id == tid))
            await db.commit()
        await engine.dispose()


@pytest.mark.integration
async def test_sql_visibility_and_revoked_actor(database):
    factory, tid, alice, bob, cids = database
    principal = Principal(tenant_id=tid, user_id=alice, role="member", is_staff=False)
    async with factory() as db:
        bind_principal(db, principal)
        ids = set(
            (
                await db.execute(sa.select(containers.c.id).where(visible_containers(principal)))
            ).scalars()
        )
        assert ids == {cids[0], cids[2]}
        with pytest.raises(APIError):
            await assert_target_access(db, {cids[1]})
        await assert_target_access(db, {cids[0], cids[2]})
        for i, cid in enumerate(cids):
            await db.execute(
                workflows.insert().values(
                    id=cid,
                    tenant_id=tid,
                    name=str(i),
                    steps=[{"container_id": cid, "prompt_id": "p"}],
                )
            )
        found = set(
            (
                await db.execute(
                    sa.select(workflows.c.id).where(
                        workflows.c.tenant_id == tid, visible_steps(workflows.c.steps, principal)
                    )
                )
            ).scalars()
        )
        assert found == {cids[0], cids[2]}
        await db.execute(
            t.memberships.update().where(t.memberships.c.user_id == alice).values(status="disabled")
        )
        with pytest.raises(APIError) as exc:
            await actor_principal(db, tid, alice)
        assert exc.value.code == "automation_actor_inactive"


@pytest.mark.integration
async def test_concurrent_admission_cannot_exceed_budget(database):
    factory, tid, alice, bob, cids = database

    async def submit(index):
        async with factory() as db:
            try:
                await admit_task(
                    db,
                    tenant_id=tid,
                    user_id=alice,
                    container_id=cids[index],
                    max_tokens=60,
                    worker_cap=1,
                    limits={"daily_token_budget": 100, "default_max_tokens": 60},
                )
                await asyncio.sleep(0.03)  # competing transaction must wait until insertion commits
                await db.execute(
                    tasks.insert().values(
                        id=uuid.uuid4().hex,
                        tenant_id=tid,
                        submitted_by=alice,
                        container_id=cids[index],
                        driver="nanobot",
                        config_snapshot={},
                        status="pending",
                        body={"prompt": "test", "limits": {"max_tokens": 60}},
                    )
                )
                await db.commit()
                return "accepted"
            except APIError as exc:
                await db.rollback()
                return exc.code

    assert sorted(await asyncio.gather(submit(0), submit(1))) == [
        "accepted",
        "daily_token_budget_exceeded",
    ]


@pytest.mark.integration
async def test_template_provisioning_is_idempotent_and_private(database, monkeypatch):
    import control_plane.routers.containers as routes
    from control_plane.config import Settings
    from control_plane.models_db import templates, user_agent_bindings
    from control_plane.routers.personal_agents import ensure_my_agent

    factory, tid, alice, bob, _ = database
    template_id = uuid.uuid4().hex
    async with factory() as db:
        await db.execute(
            templates.insert().values(
                id=template_id,
                tenant_id=tid,
                name="Personal Nanobot",
                driver="nanobot",
                model="gpt-4o",
                is_builtin=False,
                tools=[],
                skills=[],
                context={},
                limits={},
            )
        )
        await db.commit()
    calls = []

    async def provision(**kwargs):
        calls.append(kwargs["container_id"])
        await asyncio.sleep(0.05)
        return SimpleNamespace(host_shim_url=None)

    monkeypatch.setattr(routes, "provision_container", provision)
    principal = Principal(tenant_id=tid, user_id=alice, role="member", is_staff=False)
    request = SimpleNamespace(
        state=SimpleNamespace(),
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=Settings(database_url=os.environ["AGENTDOCK_TEST_DATABASE_URL"])
            )
        ),
    )

    async def ensure():
        async with factory() as db:
            bind_principal(db, principal)
            try:
                return await ensure_my_agent(template_id, request, principal, db)
            except APIError as exc:
                return exc.code

    try:
        results = await asyncio.gather(ensure(), ensure())
        assert any(isinstance(result, dict) for result in results)
        final = await ensure()
        assert final["created"] is False
        assert final["container"]["owner_user_id"] == alice
        assert len(calls) == 1
        async with factory() as db:
            other = Principal(tenant_id=tid, user_id=bob, role="member", is_staff=False)
            bind_principal(db, other)
            with pytest.raises(APIError):
                await routes._load_owned_container(db, tid, final["container"]["id"])
    finally:
        async with factory() as db:
            await db.execute(
                user_agent_bindings.delete().where(user_agent_bindings.c.template_id == template_id)
            )
            await db.execute(containers.delete().where(containers.c.template_id == template_id))
            await db.execute(templates.delete().where(templates.c.id == template_id))
            await db.commit()
