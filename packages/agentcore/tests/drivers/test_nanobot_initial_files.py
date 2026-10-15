import json
from pathlib import Path

import pytest

from agentcore import sandbox
from agentcore.drivers.nanobot import runtime_config
from agentcore.drivers.nanobot_init import initialize_nanobot, initialized_config_path
from agentcore.models import AgentConfig, ResolvedLimits
from agentcore.nanobot_files import validate_nanobot_files


@pytest.mark.parametrize("files", [
    {"../SOUL.md": "x"}, {"/config.json": "{}"}, {"SOUL.md": 1},
    {"SOUL.md": "中" * 22000}, {"config.json": "[]"},
    {"config.json": '{"providers": {}, "providers": {}}'},
    {"config.json": '{"x": NaN}'},
    {"config.json": '{"providers":{"openai":{"api_key":"secret"}}}'},
])
def test_reject_invalid_files_and_credentials(files):
    with pytest.raises(ValueError):
        validate_nanobot_files(files)


def test_sparse_defaults_and_explicit_empty_text():
    assert validate_nanobot_files({"SOUL.md": None, "USER.md": ""}) == {"USER.md": ""}
    assert validate_nanobot_files(None) == {}
    with pytest.raises(ValueError):
        validate_nanobot_files({"SOUL.md": "x"}, driver="codex")


@pytest.fixture
def local_filesystem(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(sandbox, "AGENT_HOME", str(home))
    monkeypatch.setattr(sandbox, "chown_to_agent", lambda path: None)
    monkeypatch.setattr(sandbox, "makedirs_agent",
                        lambda path, **kwargs: Path(path).mkdir(parents=True, exist_ok=True))


async def test_native_onboard_is_sole_default_source_and_runs_after_overrides(
    local_filesystem, tmp_path, monkeypatch,
):
    workspace = tmp_path / "volume"
    workspace.mkdir()
    calls = []

    async def spawn(command, **kwargs):
        calls.append(command)
        native_workspace = Path(command[command.index("--workspace") + 1])
        config = Path(command[command.index("--config") + 1])
        assert (native_workspace / "SOUL.md").read_text() == "custom soul"
        # AgentDock wrote no other file. Simulate a native version that creates
        # AGENTS.md but does NOT create TOOLS.md.
        assert sorted(p.name for p in native_workspace.iterdir()) == ["SOUL.md"]
        assert not config.exists()
        config.write_text('{"agents":{"defaults":{"timezone":"UTC"}}}')
        (native_workspace / "AGENTS.md").write_text("native default")

        class Process:
            returncode = 0

            async def wait(self):
                return 0
        return Process()

    monkeypatch.setattr(sandbox, "spawn_untrusted", spawn)
    assert await initialize_nanobot(str(workspace), {"SOUL.md": "custom soul"})
    assert not (workspace / "nanobot/TOOLS.md").exists()
    assert (workspace / "nanobot/AGENTS.md").read_text() == "native default"
    (workspace / "nanobot/SOUL.md").write_text("edited by user")
    assert not await initialize_nanobot(str(workspace), {"SOUL.md": "template V2"})
    assert (workspace / "nanobot/SOUL.md").read_text() == "edited by user"
    assert len(calls) == 1 and calls[0][0:3] == ["nanobot", "onboard", "--refresh"]


def test_runtime_owns_model_preset_key_base_url_but_preserves_native_features(tmp_path):
    source = initialized_config_path(str(tmp_path))
    source.parent.mkdir(parents=True)
    original = {
        "agents": {"defaults": {"model": "wrong", "provider": "anthropic",
                    "modelPreset": "trap", "fallbackModels": ["trap"],
                    "timezone": "Asia/Shanghai", "dream": {"enabled": True}}},
        "modelPresets": {"trap": {"model": "wrong", "provider": "anthropic"}},
        "gateway": {"heartbeat": {"enabled": True, "intervalS": 7200}},
        "channels": {"telegram": {"enabled": True}},
        "providers": {"openai": {"apiBase": "https://wrong", "extraHeaders": {"X": "old"}}},
    }
    source.write_text(json.dumps(original))
    arguments = (AgentConfig(driver="nanobot", model="gpt-4o"),
                 ResolvedLimits(max_iterations=3, max_tokens=1000, timeout_seconds=10),
                 "runtime-secret", str(tmp_path), "ws-secret", [], {})
    merged = runtime_config(*arguments, base_url="https://correct/v1")
    defaults = merged["agents"]["defaults"]
    assert (defaults["model"], defaults["provider"], defaults["modelPreset"]) == (
        "gpt-4o", "openai", None)
    assert defaults["fallbackModels"] == []
    assert defaults["timezone"] == "Asia/Shanghai" and defaults["dream"]["enabled"]
    assert merged["gateway"]["heartbeat"]["enabled"]
    assert merged["channels"]["telegram"]["enabled"]
    assert merged["channels"]["websocket"]["token"] == "ws-secret"
    assert merged["providers"]["openai"] == {
        "apiKey": "runtime-secret", "apiBase": "https://correct/v1",
        "apiType": "chat_completions",
    }
    assert "apiBase" not in runtime_config(*arguments)["providers"]["openai"]
    assert json.loads(source.read_text()) == original
    assert "runtime-secret" not in source.read_text()


async def test_refuse_existing_workspace_without_initialization_marker(
    local_filesystem, tmp_path,
):
    (tmp_path / "nanobot").mkdir()
    (tmp_path / "nanobot/SOUL.md").write_text("existing")
    with pytest.raises(ValueError, match="new workspace"):
        await initialize_nanobot(str(tmp_path), {"SOUL.md": "replacement"})
    assert (tmp_path / "nanobot/SOUL.md").read_text() == "existing"
