"""Workspace credentials and upgrade of the retired credential schema."""

from __future__ import annotations

import importlib
from unittest.mock import AsyncMock

import httpx
import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from test_private_instances import database as shared_database

from control_plane import tables as t
from control_plane.access import assert_container_access
from control_plane.app import create_app
from control_plane.auth.principal import DbPrincipalRepo, Principal, resolve_from_inputs
from control_plane.config import Settings
from control_plane.errors import APIError
from control_plane.models_db import containers, metadata, templates
from control_plane.routers.api_keys import (
    CreateKey,
    build_api_key_row,
    create_key,
    list_keys,
    revoke_key,
)

database = shared_database
migration = importlib.import_module("control_plane.migrations.versions.0032_remove_legacy_bindings")


@pytest.mark.unit
async def test_removed_routes_are_not_in_openapi_and_return_404():
    app = create_app(Settings.from_env())
    paths = app.openapi()["paths"]
    retired = [
        ("GET", "/v1/me/api-keys"),
        ("POST", "/v1/me/api-keys"),
        ("DELETE", "/v1/me/api-keys/key_old"),
        ("POST", "/v1/templates/tpl_old/my-agent"),
    ]
    assert "/v1/api-keys" in paths
    assert "/v1/templates" in paths
    assert "user_agent_bindings" not in metadata.tables
    assert "owner_user_id" not in t.api_keys.c
    assert "owner_user_id" in containers.c
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for method, path in retired:
            assert path not in paths
            assert (await client.request(method, path)).status_code == 404
    assert "/v1/templates/{template_id}/my-agent" not in paths


@pytest.mark.unit
async def test_workspace_key_never_inherits_its_creator_identity():
    secret, row = build_api_key_row(tenant_id="tenant", name="service", created_by="admin")
    repo = AsyncMock()
    repo.get_active_api_keys_by_prefix.return_value = [row]
    p = await resolve_from_inputs(
        repo, authorization=f"Bearer {secret}", cookie_token=None, admin_api_key_env=None
    )
    assert p == Principal(
        tenant_id="tenant", user_id=None, role="member", is_staff=False, auth_method="api_key"
    )
    repo.get_user.assert_not_awaited()
    repo.get_active_memberships.assert_not_awaited()


async def test_workspace_key_create_list_authenticate_and_revoke(database):
    factory, tid, alice, _, cids = database
    principal = Principal(tenant_id=tid, user_id=alice, role="admin", is_staff=False)
    kid = None
    try:
        async with factory() as db:
            created = await create_key(CreateKey(name="workspace integration"), principal, db)
            kid = created["id"]
            listing = await list_keys(principal, db)
            assert kid in {row["id"] for row in listing["keys"]}
            assert created["key"] not in str(listing)
            assert created["created_by"] == alice
            repo = DbPrincipalRepo(db)
            args = dict(
                authorization="Bearer " + created["key"], cookie_token=None, admin_api_key_env=None
            )
            p = await resolve_from_inputs(repo, **args)
            assert p.user_id is None and p.role == "member"
            shared = (
                await db.execute(sa.select(containers).where(containers.c.id == cids[2]))
            ).first()
            private = (
                await db.execute(sa.select(containers).where(containers.c.id == cids[0]))
            ).first()
            assert_container_access(shared, p)
            with pytest.raises(APIError) as denied:
                assert_container_access(private, p)
            assert denied.value.status_code == 404
            # The creator leaving does not redefine a workspace service identity.
            await db.execute(
                t.memberships.update()
                .where(t.memberships.c.user_id == alice)
                .values(status="disabled")
            )
            assert (await resolve_from_inputs(repo, **args)).user_id is None
            await db.rollback()
            await revoke_key(kid, principal, db)
            assert await resolve_from_inputs(repo, **args) is None
    finally:
        async with factory() as db:
            if kid:
                await db.execute(t.api_keys.delete().where(t.api_keys.c.id == kid))
            await db.commit()


async def test_existing_schema_cleanup_preserves_workspace_keys_and_private_containers(database):
    factory, tid, alice, bob, cids = database
    async with factory() as db:
        # These DDL statements model a populated installation before this release;
        # they are not application fixtures for the retired functionality.
        await db.execute(
            sa.text("ALTER TABLE api_keys ADD COLUMN owner_user_id TEXT REFERENCES users(id)")
        )
        template_id = tid + "template"
        await db.execute(
            templates.insert().values(
                id=template_id,
                tenant_id=tid,
                name="existing template",
                driver="nanobot",
                model="gpt-4o",
                is_builtin=False,
                tools=[],
                skills=[],
                context={},
                limits={},
            )
        )
        await db.execute(
            sa.text("""
            CREATE TABLE user_agent_bindings (
                tenant_id TEXT REFERENCES tenants(id), user_id TEXT REFERENCES users(id),
                template_id TEXT REFERENCES templates(id),
                container_id TEXT REFERENCES containers(id) ON DELETE SET NULL,
                lease_id TEXT NOT NULL, lease_until TIMESTAMPTZ NOT NULL,
                status TEXT CHECK (status IN ('provisioning', 'ready', 'error')),
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (tenant_id, user_id, template_id)
            )
        """)
        )
        await db.execute(
            sa.text("""
            INSERT INTO user_agent_bindings
            (tenant_id,user_id,template_id,container_id,lease_id,lease_until,status)
            VALUES (:tenant,:user,:template,:container,'old',now(),'ready')
        """),
            {"tenant": tid, "user": alice, "template": template_id, "container": cids[0]},
        )
        secrets = []
        key_ids = []
        workspace_hash = None
        for index, owner in enumerate((None, alice, bob)):
            secret, row = build_api_key_row(
                tenant_id=tid, name=f"existing {index}", created_by=alice
            )
            await db.execute(t.api_keys.insert().values(**row))
            await db.execute(
                sa.text("UPDATE api_keys SET owner_user_id=:owner WHERE id=:id"),
                {"owner": owner, "id": row["id"]},
            )
            if index == 2:
                await db.execute(
                    t.api_keys.update().where(t.api_keys.c.id == row["id"]).values(status="revoked")
                )
            if index == 0:
                workspace_hash = row["key_hash"]
            secrets.append(secret)
            key_ids.append(row["id"])
        before = (
            (await db.execute(sa.select(containers).where(containers.c.id == cids[0])))
            .mappings()
            .one()
        )

        def upgrade(connection):
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()

        connection = await db.connection()
        await connection.run_sync(upgrade)
        await connection.run_sync(upgrade)  # fresh/already-clean schema is also valid
        assert (
            await db.execute(sa.text("SELECT to_regclass('user_agent_bindings')"))
        ).scalar_one() is None
        assert (
            await db.execute(
                sa.text("""
            SELECT count(*) FROM information_schema.columns
            WHERE table_schema=current_schema() AND table_name='api_keys'
              AND column_name='owner_user_id'
        """)
            )
        ).scalar_one() == 0
        rows = (
            (await db.execute(sa.select(t.api_keys).where(t.api_keys.c.id.in_(key_ids))))
            .mappings()
            .all()
        )
        assert len(rows) == 1 and rows[0]["id"] == key_ids[0]
        assert rows[0]["key_hash"] == workspace_hash and rows[0]["created_by"] == alice
        after = (
            (await db.execute(sa.select(containers).where(containers.c.id == cids[0])))
            .mappings()
            .one()
        )
        assert dict(before) == dict(after)
        assert (
            await db.execute(sa.select(templates.c.id).where(templates.c.id == template_id))
        ).scalar_one() == template_id
        repo = DbPrincipalRepo(db)
        for index, secret in enumerate(secrets):
            p = await resolve_from_inputs(
                repo, authorization=f"Bearer {secret}", cookie_token=None, admin_api_key_env=None
            )
            if index == 0:
                assert p.user_id is None and p.tenant_id == tid
            else:
                assert p is None
        # All historical test data and DDL are rolled back on session exit.
