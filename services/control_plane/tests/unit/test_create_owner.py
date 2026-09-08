from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from control_plane.auth.principal import Principal
from control_plane.errors import APIError
from control_plane.routers import containers as routes
from control_plane.schemas import CreateContainerRequest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('role, visibility, target, expected', [
    ('member', 'private', 'alice', 403),
    ('member', 'private', 'bob', 403),
    ('admin', 'shared', 'bob', 400),
    ('owner', 'shared', 'bob', 400),
])
async def test_owner_permission_rejected_before_docker(
    monkeypatch, role, visibility, target, expected,
):
    monkeypatch.setattr(routes, 'load_tenant_limits', AsyncMock(return_value={}))
    provision = AsyncMock()
    monkeypatch.setattr(routes, 'provision_container', provision)
    p = Principal(tenant_id='tenant', user_id='alice', role=role, is_staff=False)
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=None)))
    with pytest.raises(APIError) as exc:
        await routes.create_container(req, CreateContainerRequest(
            name='test', visibility=visibility, owner_user_id=target,
        ), p, AsyncMock())
    assert exc.value.status_code == expected
    provision.assert_not_awaited()


@pytest.mark.parametrize('owner', ['', ' ', 'user id'])
def test_empty_or_malformed_owner_is_not_silently_self(owner):
    with pytest.raises(ValidationError):
        CreateContainerRequest(name='test', owner_user_id=owner)
