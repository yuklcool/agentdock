from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from control_plane.auth.principal import Principal
from control_plane.errors import APIError
from control_plane.routers import credentials as routes

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("value, expected", [
    (None, None), ("  ", None),
    (" https://proxy.example/v1/ ", "https://proxy.example/v1"),
    ("http://10.0.0.7:3000/v1", "http://10.0.0.7:3000/v1"),
])
def test_normalize_endpoint(value, expected):
    body = routes.SetCredential(provider="openai", api_key="test", base_url=value)
    assert body.base_url == expected
    assert routes.UpdateCredentialEndpoint(base_url=value).base_url == expected


@pytest.mark.parametrize("value", [
    "file:///etc/passwd", "ftp://example.com", "example.com/v1",
    "https://user:secret@example.com", "https://example.com?key=secret",
    "https://example.com/#secret", "https://exa mple.com", "https://example.com/\nkey",
    "https://example.com\\@other.com", "https://example.com:99999", 123,
])
def test_reject_invalid_or_secret_bearing_urls(value):
    with pytest.raises(ValidationError):
        routes.SetCredential(provider="openai", api_key="test", base_url=value)
    with pytest.raises(ValidationError):
        routes.UpdateCredentialEndpoint(base_url=value)


def test_patch_requires_explicit_value():
    with pytest.raises(ValidationError):
        routes.UpdateCredentialEndpoint()


@pytest.mark.parametrize("endpoint", ["https://proxy.example/v1", None])
async def test_edit_endpoint_preserves_key_and_scopes_every_query(monkeypatch, endpoint):
    row = dict(id="cred_1", tenant_id="ten_1", provider="openai", auth_method="api_key",
               key_last4="1234", key_ciphertext=b"encrypted", created_by="usr_1", created_at=None)
    result = Mock()
    result.mappings.return_value.first.return_value = row
    conn = AsyncMock()
    conn.execute.return_value = result
    audit = AsyncMock()
    monkeypatch.setattr(routes, "audit", audit)
    p = Principal(tenant_id="ten_1", role="admin", user_id="usr_1", is_staff=False)
    saved = await routes.update_credential_endpoint(
        "cred_1", routes.UpdateCredentialEndpoint(base_url=endpoint), p=p, conn=conn,
    )
    assert saved["base_url"] == endpoint
    assert "key_ciphertext" not in saved and "api_key" not in saved
    for call in conn.execute.call_args_list:
        assert "ten_1" in call.args[0].compile().params.values()
    update = conn.execute.call_args_list[1].args[0].compile().params
    assert update["base_url"] == endpoint
    assert "key_ciphertext" not in update and "key_last4" not in update
    assert audit.call_args.kwargs["details"] == {"custom_endpoint": endpoint is not None}
    conn.commit.assert_awaited_once()


@pytest.mark.parametrize("row, code", [
    (None, "not_found"),
    ({"provider": "openai", "auth_method": "oauth_subscription"}, "validation_error"),
    ({"provider": "opencode", "auth_method": "api_key"}, "validation_error"),
])
async def test_edit_cannot_change_missing_or_unsupported_credential(row, code):
    result = Mock()
    result.mappings.return_value.first.return_value = row
    conn = AsyncMock()
    conn.execute.return_value = result
    p = Principal(tenant_id="ten_1", role="admin", user_id="usr_1", is_staff=False)
    with pytest.raises(APIError) as exc:
        await routes.update_credential_endpoint(
            "cred_1", routes.UpdateCredentialEndpoint(base_url=None), p=p, conn=conn,
        )
    assert exc.value.code == code
    conn.commit.assert_not_awaited()
