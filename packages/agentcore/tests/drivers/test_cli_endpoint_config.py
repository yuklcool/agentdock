import json
import tomllib
from pathlib import Path

import pytest

from agentcore.drivers.codex import (
    CUSTOM_OPENAI_PROVIDER,
    build_app_server_command,
    endpoint_args,
    thread_resume_params,
    thread_start_params,
)
from agentcore.drivers.opencode import (
    build_env,
    opencode_config_path,
    write_opencode_endpoint,
    write_opencode_mcp,
)
from agentcore.models import ShimMcpServer


def test_opencode_uses_chat_compatible_provider_and_env_key(tmp_path):
    selected = write_opencode_endpoint(str(tmp_path), "openai/gpt-4o", "https://gateway/v1/")
    assert selected == "agentdock-openai/gpt-4o"
    path = Path(opencode_config_path(str(tmp_path)))
    provider = json.loads(path.read_text())["provider"]["agentdock-openai"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"] == {
        "baseURL": "https://gateway/v1",
        "apiKey": "{env:OPENAI_API_KEY}",
    }
    assert "gpt-4o" in provider["models"]
    assert build_env({}, provider="openai", credential="secret")["OPENAI_API_KEY"] == "secret"
    assert "secret" not in path.read_text()


def test_opencode_updates_and_clears_only_managed_provider(tmp_path):
    workspace = str(tmp_path)
    path = Path(opencode_config_path(workspace))
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"instructions": ["custom.md"], "provider": {"other": {"name": "other"}}})
    )
    write_opencode_endpoint(workspace, "gpt-4o", "https://first/v1")
    write_opencode_mcp(workspace, [ShimMcpServer(name="test", url="https://mcp/mcp")])
    write_opencode_endpoint(workspace, "gpt-4o", "https://second/api")
    assert (
        json.loads(path.read_text())["provider"]["agentdock-openai"]["options"]["baseURL"]
        == "https://second/api"
    )
    assert write_opencode_endpoint(workspace, "gpt-4o", None) == "openai/gpt-4o"
    remaining = json.loads(path.read_text())
    assert remaining["instructions"] == ["custom.md"]
    assert "test" in remaining["mcp"] and remaining["provider"] == {"other": {"name": "other"}}


def test_opencode_bad_config_fails_instead_of_falling_back(tmp_path):
    path = Path(opencode_config_path(str(tmp_path)))
    path.parent.mkdir(parents=True)
    path.write_text("invalid json")
    with pytest.raises(ValueError):
        write_opencode_endpoint(str(tmp_path), "gpt-4o", "https://gateway/v1")
    with pytest.raises(ValueError):
        write_opencode_endpoint(str(tmp_path), "gpt-4o", None)


@pytest.mark.parametrize("url", ["http://gateway/api", "https://gateway/v1"])
def test_codex_config_uses_responses_and_env_auth_without_a_literal_secret(url):
    args = endpoint_args(url)
    cfg = {}
    for i in range(0, len(args), 2):
        assert args[i] == "-c"
        # Parse overrides as TOML to prove escaping and exact field values.
        parsed = tomllib.loads(args[i + 1])
        if "model_provider" in parsed:
            cfg["model_provider"] = parsed["model_provider"]
        else:
            cfg.setdefault("provider", {}).update(parsed["model_providers"][CUSTOM_OPENAI_PROVIDER])
    assert cfg["model_provider"] == CUSTOM_OPENAI_PROVIDER
    assert cfg["provider"]["base_url"] == url
    assert cfg["provider"]["wire_api"] == "responses"
    assert cfg["provider"]["env_key"] == "CODEX_API_KEY"
    assert cfg["provider"]["requires_openai_auth"] is False
    assert cfg["provider"]["supports_websockets"] is False
    assert build_app_server_command(tools=[], base_url=url)[-len(args) :] == args
    assert endpoint_args(None) == []


def test_codex_start_and_resume_can_switch_provider_explicitly():
    start = thread_start_params(
        workspace="/w", model="gpt-4o", instructions="", model_provider=CUSTOM_OPENAI_PROVIDER
    )
    resumed = thread_resume_params(
        thread_id="t",
        workspace="/w",
        model="gpt-4o",
        instructions="",
        model_provider=CUSTOM_OPENAI_PROVIDER,
    )
    reset = thread_resume_params(
        thread_id="t", workspace="/w", model="gpt-4o", instructions="", model_provider="openai"
    )
    assert start["modelProvider"] == resumed["modelProvider"] == CUSTOM_OPENAI_PROVIDER
    assert reset["modelProvider"] == "openai"
