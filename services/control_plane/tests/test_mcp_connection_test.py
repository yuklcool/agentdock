from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from control_plane.app import create_app
from control_plane.auth.crypto import encrypt_secret
from control_plane.auth.principal import Principal, resolve_principal
from control_plane.config import Settings
from control_plane.routers.mcp_servers import apply_mcp_patch

KEY = b"0" * 32


class Session:
    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def execute(self, statement):
        self.statements.append(str(statement))
        return self

    def fetchone(self):
        return type("Row", (), {"_mapping": self.rows[0]})() if self.rows else None


@pytest.fixture
def setup(monkeypatch):
    app = create_app(
        Settings(
            database_url="postgresql+asyncpg://x:x@localhost/x",
            seed_tenant_id="ten_seed",
            seed_api_key="tk_live_seed",
            seed_llm_api_key="",
            agent_image_tag="test",
            internal_network="test",
            readyz_timeout_seconds=1.0,
            shim_port=8080,
        )
    )
    app.dependency_overrides[resolve_principal] = lambda: Principal(
        tenant_id="ten_1",
        role="admin",
        is_staff=False,
        user_id="usr_a",
    )
    session = Session([])
    app.state.session_factory = lambda: session
    probe = AsyncMock(return_value={"status": "success", "tools": []})
    monkeypatch.setattr("control_plane.routers.mcp_servers.probe_mcp_server", probe)
    monkeypatch.setattr("control_plane.routers.mcp_servers.load_key_from_env", lambda: KEY)
    with TestClient(app) as client:
        yield client, session, probe, app


def _row():
    return {
        "id": "mcp_1",
        "tenant_id": "ten_1",
        "name": "test",
        "description": "d",
        "url": "http://mcp.test/mcp",
        "auth_type": "bearer",
        "auth_header_name": None,
        "secret_ciphertext": encrypt_secret("stored-token", KEY),
        "enabled": False,
    }


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_draft_does_not_save_or_require_name(setup, scheme):
    client, session, probe, _ = setup
    response = client.post("/v1/mcp-servers/test", json={"url": f"{scheme}://mcp.test/mcp"})
    assert response.status_code == 200
    assert session.statements == []
    assert probe.await_args.kwargs["proxy_url"] == "http://egress-proxy:8888"


def test_saved_secret_is_used_without_returning_or_saving_it(setup):
    client, session, probe, _ = setup
    session.rows = [_row()]
    response = client.post("/v1/mcp-servers/mcp_1/test", json={})
    assert response.status_code == 200
    assert probe.await_args.args[0].secret == "stored-token"
    assert "stored-token" not in response.text
    assert len(session.statements) == 1
    assert "tenant_id" in session.statements[0]


def test_temporary_overrides_do_not_modify_saved_config(setup):
    client, session, probe, _ = setup
    row = _row()
    session.rows = [row]
    response = client.post(
        "/v1/mcp-servers/mcp_1/test",
        json={
            "url": "https://other.test/mcp",
            "secret": "new-token",
        },
    )
    assert response.status_code == 200
    assert probe.await_args.args[0].url == "https://other.test/mcp"
    assert probe.await_args.args[0].secret == "new-token"
    assert row["url"] == "http://mcp.test/mcp"
    assert len(session.statements) == 1


def test_changed_origin_cannot_receive_old_secret(setup):
    client, session, probe, _ = setup
    session.rows = [_row()]
    response = client.post("/v1/mcp-servers/mcp_1/test", json={"url": "http://other.test/mcp"})
    assert response.status_code == 400
    assert response.json()["error"]["field"] == "secret"
    probe.assert_not_awaited()
    from control_plane.errors import APIError

    with pytest.raises(APIError):
        apply_mcp_patch(_row(), {"url": "http://other.test/mcp"})


def test_foreign_or_missing_id_is_not_found(setup):
    client, session, probe, _ = setup
    response = client.post("/v1/mcp-servers/foreign/test", json={})
    assert response.status_code == 404
    assert "tenant_id" in session.statements[0]
    probe.assert_not_awaited()


@pytest.mark.parametrize("path", ["/v1/mcp-servers/test", "/v1/mcp-servers/mcp_1/test"])
def test_member_cannot_probe_or_use_stored_secret(setup, path):
    client, session, probe, app = setup
    app.dependency_overrides[resolve_principal] = lambda: Principal(
        tenant_id="ten_1",
        role="member",
        is_staff=False,
        user_id="usr_m",
    )
    assert client.post(path, json={"url": "http://mcp.test/mcp"}).status_code == 403
    assert not session.statements
    probe.assert_not_awaited()


@pytest.mark.parametrize(
    "body",
    [
        {"url": "file:///etc/passwd"},
        {"url": "http://mcp.test/mcp", "auth_type": "bearer"},
        {
            "url": "http://mcp.test/mcp",
            "auth_type": "header",
            "auth_header_name": "Host",
            "secret": "x",
        },
        {"url": "http://mcp.test/mcp", "secret": "x\r\ninjected"},
    ],
)
def test_invalid_input_never_connects(setup, body):
    client, _, probe, _ = setup
    assert client.post("/v1/mcp-servers/test", json=body).status_code == 400
    probe.assert_not_awaited()
