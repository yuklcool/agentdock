import asyncio

import httpx

import shim.app as module
from shim.app import create_app


async def test_initialization_auth_validation_and_task_exclusion(tmp_path, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def initialize(workspace, files):
        calls.append(files)
        entered.set()
        await release.wait()
        return True

    monkeypatch.setattr(module, "initialize_nanobot", initialize)
    app = create_app(workspace=str(tmp_path), token="shim-only", drivers={})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://shim") as client:
        assert (await client.post("/nanobot/initialize", json={"files": {}})).status_code == 401
        headers = {"Authorization": "Bearer shim-only"}
        for payload in ([], {}, {"files": {"../SOUL.md": "x"}}):
            response = await client.post("/nanobot/initialize", json=payload, headers=headers)
            assert response.status_code == 400
        pending = asyncio.create_task(client.post(
            "/nanobot/initialize", json={"files": {"SOUL.md": "custom"}}, headers=headers))
        await entered.wait()
        assert (await client.post("/tasks", json={}, headers=headers)).status_code == 409
        release.set()
        assert (await pending).json() == {"initialized": True}
        assert calls == [{"SOUL.md": "custom"}]
