from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from control_plane.auth.principal import Principal
from control_plane.errors import not_found
from control_plane.routers import nanobot_ws as mod

pytestmark = pytest.mark.unit


@pytest.fixture
def app(monkeypatch):
    app = FastAPI()
    app.include_router(mod.router, prefix="/v1")
    principal = Principal(tenant_id="tenant-a", user_id="user-a", role="member", is_staff=False)
    app.dependency_overrides[mod._principal_ws] = lambda: principal

    @asynccontextmanager
    async def session():
        yield SimpleNamespace(info={})

    app.state.session_factory = session
    app.state.settings = object()
    row = SimpleNamespace(id="agent-a", config={"driver": "nanobot"})

    async def load(session, tenant_id, cid):
        assert tenant_id == "tenant-a"
        if cid != "agent-a":
            raise not_found()
        return row

    monkeypatch.setattr(mod, "_load_owned_container", load)
    monkeypatch.setattr(mod, "submit_task_core", AsyncMock(return_value={"task_id": "task-a"}))
    return app


def test_rejects_missing_auth_and_cross_origin(app):
    app.dependency_overrides[mod._principal_ws] = lambda: None
    with pytest.raises(WebSocketDisconnect) as error:
        with TestClient(app).websocket_connect("/v1/ws"):
            pass
    assert error.value.code == 4401
    with pytest.raises(WebSocketDisconnect) as error:
        with TestClient(app).websocket_connect(
            "/v1/ws", headers={"origin": "https://attacker.example"}
        ):
            pass
    assert error.value.code == 4403


def test_cross_tenant_request_never_submits(app):
    with TestClient(app).websocket_connect("/v1/ws") as ws:
        assert ws.receive_json()["event"] == "ready"
        ws.send_json({"type": "message", "agent_id": "agent-b", "content": "hello"})
        assert ws.receive_json() == {"event": "error", "code": "not_found"}
    mod.submit_task_core.assert_not_called()


def test_validation_and_keepalive(app):
    with TestClient(app).websocket_connect("/v1/ws") as ws:
        ws.receive_json()
        ws.send_text("not json")
        assert ws.receive_json()["code"] == "invalid_frame"
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"event": "pong"}


def test_message_streams_task_events_with_routing_ids(app, monkeypatch):
    class Shim:
        async def stream_events(self, tid, after_seq):
            assert tid == "task-a" and after_seq == 0
            yield b'data: {"seq": 1, "type": "assistant_delta", "payload": {"text": "Hello"}}\n\n'
            yield b'data: {"seq": 2, "type": "status_change", "payload": {"to": "completed"}}\n\n'

        async def aclose(self):
            pass

    monkeypatch.setattr(mod, "_shim_for", lambda *args: Shim())
    with TestClient(app).websocket_connect("/v1/ws") as ws:
        ws.receive_json()
        ws.send_json(
            {"type": "message", "agent_id": "agent-a", "session_id": "session-a", "content": "hi"}
        )
        attached = ws.receive_json()
        assert attached == {
            "event": "attached",
            "agent_id": "agent-a",
            "session_id": "session-a",
            "task_id": "task-a",
        }
        delta = ws.receive_json()
        assert delta["text"] == "Hello" and delta["task_id"] == "task-a"
        assert ws.receive_json()["event"] == "turn_end"
    args = mod.submit_task_core.call_args.kwargs
    assert args["body"].session_id == "session-a" and args["tenant_id"] == "tenant-a"


def test_cancel_checks_task_ownership(app, monkeypatch):
    monkeypatch.setattr(mod, "_load_owned_task", AsyncMock(side_effect=not_found()))
    shim = AsyncMock()
    monkeypatch.setattr(mod, "_shim_for", lambda *args: shim)
    with TestClient(app).websocket_connect("/v1/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "cancel", "agent_id": "agent-a", "task_id": "foreign-task"})
        assert ws.receive_json()["code"] == "not_found"
    shim.cancel_task.assert_not_called()
