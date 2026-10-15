"""Exercise snapshot creation and template CRUD on the CI PostgreSQL service."""

import json
from types import SimpleNamespace

import httpx
import sqlalchemy as sa
from test_private_instances import database as shared_database

from control_plane.app import create_app
from control_plane.auth.principal import Principal, resolve_principal
from control_plane.config import Settings
from control_plane.models_db import containers, templates
from control_plane.routers import containers as routes

database = shared_database


async def test_template_crud_clone_reset_and_container_snapshot(database, monkeypatch):
    factory, tid, alice, _, _ = database
    app = create_app(Settings.from_env())
    app.state.session_factory = factory
    principal = Principal(tenant_id=tid, user_id=alice, role="admin", is_staff=False)
    app.dependency_overrides[resolve_principal] = lambda: principal
    provisioned = []

    async def provision(**kwargs):
        provisioned.append(kwargs)
        return SimpleNamespace(host_shim_url=None)

    monkeypatch.setattr(routes, "provision_container", provision)
    overrides = {"SOUL.md": "v1", "config.json": json.dumps({
        "agents": {"defaults": {"model": "wrong-template-model", "dream": {"enabled": True}}},
    })}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            response = await client.post("/v1/templates", json={
                "name": "Initialization", "driver": "nanobot", "nanobot_initial_files": overrides,
                "model": "legacy-model", "mem_limit": "8g",
            })
            assert response.status_code == 200, response.text
            template = response.json()
            assert template["model"] is None and template["mem_limit"] is None
            assert template["nanobot_initial_files"] == overrides
            clone = await client.post(f"/v1/templates/{template['id']}/clone", json={})
            assert clone.status_code == 200 and clone.json()["nanobot_initial_files"] == overrides
            created = await client.post("/v1/containers", json={
                "name": "Snapshot", "template_id": template["id"],
                "config": {"driver": "nanobot", "model": "gpt-4o"},
            })
            assert created.status_code == 201, created.text
            container = created.json()
            assert container["nanobot_initial_files"] == overrides
            assert provisioned[-1]["nanobot_initial_files"] == overrides
            assert container["config"]["model"] == "gpt-4o"
            changed = await client.patch(f"/v1/templates/{template['id']}", json={
                "nanobot_initial_files": {"SOUL.md": "v2", "USER.md": None},
            })
            assert changed.status_code == 200
            assert changed.json()["nanobot_initial_files"] == {"SOUL.md": "v2"}
            reset = await client.patch(f"/v1/templates/{template['id']}", json={
                "nanobot_initial_files": {"SOUL.md": None},
            })
            assert reset.json()["nanobot_initial_files"] == {}
            async with factory() as db:
                stored = (await db.execute(sa.select(containers.c.nanobot_initial_files).where(
                    containers.c.id == container["id"]))).scalar_one()
                assert stored == overrides
            invalid = await client.patch(f"/v1/templates/{template['id']}", json={
                "nanobot_initial_files": {"config.json": '{"apiKey":"do-not-echo"}'},
            })
            assert invalid.status_code == 400 and "do-not-echo" not in invalid.text
    finally:
        async with factory() as db:
            await db.execute(containers.update().where(containers.c.tenant_id == tid).values(
                template_id=None))
            await db.execute(templates.delete().where(templates.c.tenant_id == tid))
            await db.commit()
