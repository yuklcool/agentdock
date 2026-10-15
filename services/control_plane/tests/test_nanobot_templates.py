import json
from types import SimpleNamespace

import pytest

from agentcore.models import AgentConfig
from control_plane.errors import APIError
from control_plane.routers.containers import _resolve_create_config
from control_plane.routers.templates import initial_files_from_body
from control_plane.schemas import CreateContainerRequest


def test_default_reset_and_invalid_template_json():
    assert initial_files_from_body({"SOUL.md": None}, "nanobot") == {}
    assert initial_files_from_body({"USER.md": ""}, "nanobot") == {"USER.md": ""}
    for files, driver in [({"SOUL.md": "x"}, "api"), ({"config.json": "[1]"}, "nanobot")]:
        with pytest.raises(APIError) as error:
            initial_files_from_body(files, driver)
        assert error.value.field == "nanobot_initial_files"


async def test_container_runtime_is_explicit_and_snapshot_is_independent_of_inline_config():
    row = SimpleNamespace(
        id="tpl", driver="nanobot", model="wrong-template-model", effort=None,
        system_prompt="", system_prompt_mode="augment", tools=[], context={}, skills=[],
        mcp_servers=[], image_variant="slim", mem_limit="99g", cpus=20, env_vars=[{"old": True}],
        nanobot_initial_files={"SOUL.md": "v1"},
    )

    class Session:
        async def execute(self, *args):
            return SimpleNamespace(first=lambda: row)

    request = CreateContainerRequest(name="x", template_id="tpl",
                                     config=AgentConfig(driver="nanobot", model="gpt-4o"))
    config, template, runtime = await _resolve_create_config(Session(), request)
    assert config.model == "gpt-4o" and template == "tpl"
    assert runtime.nanobot_initial_files == {"SOUL.md": "v1"}
    assert (runtime.image_variant, runtime.mem_limit, runtime.cpus, runtime.env_vars) == (
        None, None, None, None)
    with pytest.raises(APIError, match="provide config.model"):
        await _resolve_create_config(Session(), CreateContainerRequest(name="x", template_id="tpl"))


def test_openapi_documents_initialization_and_snapshot():
    from control_plane.app import create_app
    from control_plane.config import Settings

    api = create_app(Settings.from_env()).openapi()
    field = api["paths"]["/v1/templates"]["post"]["requestBody"]["content"][
        "application/json"]["schema"]["properties"]["nanobot_initial_files"]
    assert "PATCH replaces" in field["description"]
    assert "nanobot_initial_files" in api["components"]["schemas"]["ContainerOut"]["properties"]
    assert "apiKey" not in json.dumps(field)
