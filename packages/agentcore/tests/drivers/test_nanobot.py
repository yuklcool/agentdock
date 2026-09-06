from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from agentcore.drivers.base import get_driver
from agentcore.drivers.nanobot import NanobotDriver, NanobotError, NanobotRuntime, runtime_config
from agentcore.models import AgentConfig, ResolvedLimits, ShimMcpServer, ShimSkill, TaskBody

pytestmark = pytest.mark.unit

CONFIG = AgentConfig(driver="nanobot", model="gpt-4o")
LIMITS = ResolvedLimits(max_iterations=3, max_tokens=1000, timeout_seconds=3)


class Peer:
    def __init__(self, mode="normal"):
        self.mode = mode
        self.chats = []
        self.messages = []
        self.stopped = asyncio.Event()
        self.received = asyncio.Event()

    async def handle(self, ws):
        await ws.send(json.dumps({"event": "ready", "chat_id": "unused"}))
        async for raw in ws:
            frame = json.loads(raw)
            if frame["type"] in {"new_chat", "attach"}:
                chat_id = frame.get("chat_id") or f"chat-{len(self.chats)}"
                self.chats.append(chat_id)
                await ws.send(json.dumps({"event": "attached", "chat_id": chat_id}))
                continue
            self.messages.append(frame)
            self.received.set()
            cid = frame["chat_id"]

            async def send(event_kind, cid=cid, **kw):
                await ws.send(json.dumps({"event": event_kind, "chat_id": cid, **kw}))

            if frame["content"] == "/stop":
                self.stopped.set()
                await send("message", text="Stopped 1 task(s).")
                continue
            if self.mode == "wait":
                continue
            if self.mode == "disconnect":
                await ws.close()
                return
            if self.mode == "error":
                await send("error", detail="secret-must-not-be-emitted")
                continue
            if self.mode == "simple":
                await send("delta", stream_id="s1", text="All done")
                await send("stream_end", stream_id="s1", text="All done")
                await send("turn_end", usage={"prompt_tokens": 10, "completion_tokens": 3})
                continue
            await ws.send(json.dumps({"event": "delta", "chat_id": "unrelated", "text": "LEAK"}))
            await send("reasoning_delta", stream_id="r1", text="Consider")
            await send("reasoning_end", stream_id="r1")
            await send("message", kind="progress", text="Working")
            await send("delta", stream_id="s1", text="Before tool")
            await send("stream_end", stream_id="s1", resuming=True)
            await send("delta", stream_id="s2", text="After ")
            await send("delta", stream_id="s2", text="tool")
            await send("stream_end", stream_id="s2", text="After tool!")
            await send("turn_end", usage={"prompt_tokens": 12, "completion_tokens": 8})


async def run_peer(tmp_path, peer, operation):
    async with serve(peer.handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        driver = NanobotDriver()
        runtime = NanobotRuntime(str(tmp_path))
        runtime.ensure_started = AsyncMock()
        runtime.close = AsyncMock()
        runtime.connection = lambda: connect(f"ws://127.0.0.1:{port}", proxy=None)
        driver.runtimes[str(tmp_path)] = runtime
        return await operation(driver, runtime)


async def invoke(driver, workspace, **kwargs):
    events = []

    async def emit(kind, payload):
        events.append((kind, payload))

    args = dict(
        task=TaskBody(prompt="Hello"),
        config=CONFIG,
        limits=LIMITS,
        credential="secret",
        emit=emit,
        cancel=asyncio.Event(),
        workspace=str(workspace),
        session_id="session-1",
    )
    args.update(kwargs)
    result = await driver.run(**args)
    return result, events


def test_registration_and_config():
    driver = get_driver("nanobot")
    assert driver.capabilities.supports_mcp and driver.capabilities.supports_skills
    config = runtime_config(
        CONFIG,
        LIMITS,
        "llm-secret",
        "/workspace",
        "ws-secret",
        [
            ShimMcpServer(
                name="db",
                url="https://mcp.example.com/mcp",
                auth_type="bearer",
                secret="mcp-secret",
            ),
        ],
        {},
    )
    assert config["channels"]["websocket"]["host"] == "127.0.0.1"
    assert config["channels"]["websocket"]["websocketRequiresToken"]
    assert config["agents"]["defaults"]["maxToolIterations"] == 3
    assert config["tools"]["mcpServers"]["db"]["headers"]["Authorization"] == "Bearer mcp-secret"
    assert not config["gateway"]["heartbeat"]["enabled"]


async def test_streams_wait_for_turn_end_and_ignore_other_chats(tmp_path):
    async def check(driver, runtime):
        result, events = await invoke(driver, tmp_path)
        assert result.success
        assert result.output["output"] == "Before tool\n\nAfter tool!"
        assert "LEAK" not in json.dumps(events)
        assert [p for k, p in events if k == "token_update"] == [{"tokens_in": 12, "tokens_out": 8}]
        assert any(k == "reasoning_delta" for k, _ in events)
        assert events[-1][1]["to"] == "completed"

    await run_peer(tmp_path, Peer(), check)


def test_ssrf_exceptions_are_host_owned(monkeypatch):
    monkeypatch.delenv("AGENTDOCK_NANOBOT_SSRF_WHITELIST", raising=False)
    env = {"AGENTDOCK_NANOBOT_SSRF_WHITELIST": "0.0.0.0/0"}
    native = runtime_config(CONFIG, LIMITS, "secret", "/workspace", "token", [], env)
    assert native["tools"]["ssrfWhitelist"] == []
    monkeypatch.setenv("AGENTDOCK_NANOBOT_SSRF_WHITELIST", "10.0.0.7/32 ::1/128")
    native = runtime_config(CONFIG, LIMITS, "secret", "/workspace", "token", [], env)
    assert native["tools"]["ssrfWhitelist"] == ["10.0.0.7/32", "::1/128"]
    monkeypatch.setenv("AGENTDOCK_NANOBOT_SSRF_WHITELIST", "bad-secret-input")
    with pytest.raises(NanobotError, match="^nanobot_invalid_ssrf_whitelist$"):
        runtime_config(CONFIG, LIMITS, "secret", "/workspace", "token", [], env)


async def test_sessions_survive_new_driver_and_are_distinct(tmp_path):
    peer = Peer()

    async def check(driver, runtime):
        assert (await invoke(driver, tmp_path))[0].success
        # A newly constructed runtime uses only disk state for the mapping.
        runtime.chat = NanobotRuntime(str(tmp_path)).chat
        assert (await invoke(driver, tmp_path, session_is_continuation=True))[0].success
        assert (await invoke(driver, tmp_path, session_id="../../another"))[0].success

    await run_peer(tmp_path, peer, check)
    assert peer.chats[0] == peer.chats[1]
    assert peer.chats[2] != peer.chats[0]
    assert len(list((tmp_path / ".agent-runtime/nanobot/sessions").glob("*.json"))) == 2


async def test_missing_continuation_fails_closed(tmp_path):
    async def check(driver, runtime):
        result, _ = await invoke(driver, tmp_path, session_is_continuation=True)
        assert result.reason == "session_state_lost"

    await run_peer(tmp_path, Peer(), check)


@pytest.mark.parametrize("times_out", [False, True])
async def test_cancel_and_timeout_stop_current_chat(tmp_path, times_out):
    peer = Peer("wait")

    async def check(driver, runtime):
        cancel = asyncio.Event()
        limits = LIMITS.model_copy(update={"timeout_seconds": 1})
        task = asyncio.create_task(invoke(driver, tmp_path, cancel=cancel, limits=limits))
        await peer.received.wait()
        if not times_out:
            cancel.set()
        result, _ = await task
        assert result.reason == ("timeout" if times_out else "cancelled")
        assert peer.stopped.is_set()
        runtime.close.assert_not_called()

    await run_peer(tmp_path, peer, check)


@pytest.mark.parametrize("mode", ["error", "disconnect"])
async def test_protocol_and_connection_failure_do_not_hang_or_leak(tmp_path, mode):
    async def check(driver, runtime):
        result, events = await invoke(driver, tmp_path)
        assert not result.success
        assert "secret-must-not" not in json.dumps(events)

    await run_peer(tmp_path, Peer(mode), check)


async def test_skill_selection_removes_only_managed_files(tmp_path):
    runtime = NanobotRuntime(str(tmp_path))
    user_file = tmp_path / "nanobot/skills/personal/SKILL.md"
    user_file.parent.mkdir(parents=True)
    user_file.write_text("user content")
    skill = ShimSkill(name="lighting", description="Lighting", body="Analyze alarms")
    await runtime.sync_skills([skill])
    assert (tmp_path / "nanobot/skills/lighting/SKILL.md").is_file()
    await runtime.sync_skills([])
    assert not (tmp_path / "nanobot/skills/lighting").exists()
    assert user_file.read_text() == "user content"
    with pytest.raises(NanobotError, match="path_conflict"):
        await runtime.sync_skills([skill.model_copy(update={"name": "personal"})])


async def test_config_secret_is_temporary_and_data_persistent(tmp_path, monkeypatch):
    from agentcore import sandbox

    monkeypatch.setattr(sandbox, "chown_to_agent", lambda path: None)
    monkeypatch.setattr(
        sandbox, "makedirs_agent", lambda path, **kw: Path(path).mkdir(parents=True, exist_ok=True)
    )
    parent = tmp_path / "volatile"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = NanobotRuntime(str(workspace), runtime_parent=str(parent))
    process = AsyncMock()
    process.returncode = None
    process.terminate = Mock()
    monkeypatch.setattr(sandbox, "spawn_untrusted", AsyncMock(return_value=process))
    runtime.ready = AsyncMock(return_value=True)
    native = runtime_config(CONFIG, LIMITS, "llm-secret", str(workspace), runtime.token, [], {})
    await runtime.ensure_started(native, [], {})
    directory = runtime.directory
    assert directory is not None
    assert (directory / "config.json").stat().st_mode & 0o777 == 0o600
    (directory / "sessions/history.json").write_text("history")
    assert (workspace / ".agent-state/nanobot/data/sessions/history.json").read_text() == "history"
    assert not any("llm-secret" in p.read_text() for p in workspace.rglob("*") if p.is_file())
    await runtime.ensure_started(native, [], {})
    assert sandbox.spawn_untrusted.await_count == 1
    native["providers"]["openai"]["apiKey"] = "rotated-secret"
    await runtime.ensure_started(native, [], {})
    assert sandbox.spawn_untrusted.await_count == 2
    assert not directory.exists()
    assert (workspace / ".agent-state/nanobot/data/sessions/history.json").exists()
    await runtime.close()
