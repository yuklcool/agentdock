"""Run the pinned real gateway against a local deterministic OpenAI endpoint.

Install Nanobot separately and put its CLI on PATH, then set
AGENTDOCK_TEST_NANOBOT=1. No external LLM credentials or Docker needed.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

import pytest

from agentcore import sandbox
from agentcore.drivers.nanobot import NanobotDriver
from agentcore.models import AgentConfig, ResolvedLimits, ShimSkill, TaskBody

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENTDOCK_TEST_NANOBOT") != "1" or not shutil.which("nanobot"),
    reason="opt-in: requires the pinned official Nanobot CLI",
)


async def test_real_gateway_streaming_history_and_restart(monkeypatch):
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    ):
        monkeypatch.delenv(key, raising=False)
    test_home = tempfile.mkdtemp(prefix="agentdock-live-home-")
    Path(test_home).chmod(0o777)
    monkeypatch.setattr(sandbox, "AGENT_HOME", test_home)
    local_uid = os.environ.get("AGENTDOCK_TEST_LOCAL_UID") == "1"
    if local_uid:
        # Only for user-namespace test hosts that cannot change uid/groups.
        # Production spawn code is unchanged; Docker verifies the uid boundary.
        monkeypatch.setattr(sandbox, "drop_kwargs", lambda: {})
        monkeypatch.setattr(sandbox, "AGENT_UID", os.getuid())
        monkeypatch.setattr(sandbox, "AGENT_GID", os.getgid())
    received = []

    async def api(reader, writer):
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            length = next(
                int(line.split(b":", 1)[1])
                for line in headers.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            )
            body = json.loads(await reader.readexactly(length))
            received.append(body)
            response = "AgentDock live answer"
            if body.get("stream"):
                payload = {
                    "id": "test",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "gpt-4o",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": response},
                            "finish_reason": None,
                        }
                    ],
                }
                final = {**payload, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                wire = (
                    f"data: {json.dumps(payload)}\n\ndata: {json.dumps(final)}\n\ndata: [DONE]\n\n"
                ).encode()
                kind = "text/event-stream"
            else:
                wire = json.dumps(
                    {
                        "id": "test",
                        "object": "chat.completion",
                        "created": 1,
                        "model": "gpt-4o",
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {"role": "assistant", "content": response},
                            }
                        ],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
                    }
                ).encode()
                kind = "application/json"
            writer.write(
                (
                    f"HTTP/1.1 200 OK\r\nContent-Type: {kind}\r\n"
                    f"Content-Length: {len(wire)}\r\nConnection: close\r\n\r\n"
                ).encode()
                + wire
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(api, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    persistent = os.environ.get("AGENTDOCK_TEST_WORKSPACE")
    workspace = persistent or tempfile.mkdtemp(prefix="agentdock-live-")
    Path(workspace).mkdir(exist_ok=True)
    marker = "DOCK-" + os.environ.get("AGENTDOCK_TEST_INSTANCE", "123")
    restoring = os.environ.get("AGENTDOCK_TEST_RESTORE") == "1"
    # Production layout: root-owned workspace with sticky bit; agent can write.
    Path(workspace).chmod(0o1777)
    events = []

    async def emit(kind, payload):
        events.append((kind, payload))

    driver = NanobotDriver()
    args = dict(
        config=AgentConfig(driver="nanobot", model="gpt-4o"),
        limits=ResolvedLimits(max_iterations=3, max_tokens=1000, timeout_seconds=60),
        credential="local-test-only",
        emit=emit,
        cancel=asyncio.Event(),
        workspace=workspace,
        session_id="persistent-session",
        skills=[
            ShimSkill(
                name="dock-test",
                description="AgentDock test skill",
                body="Remember only this workspace: " + marker,
            )
        ],
        env={
            "AGENTDOCK_NANOBOT_API_BASE": f"http://127.0.0.1:{port}/v1",
            "NO_PROXY": "127.0.0.1,localhost",
        },
    )
    try:
        result = await driver.run(
            task=TaskBody(prompt="Recall the marker" if restoring else "Remember marker " + marker),
            session_is_continuation=restoring,
            **args,
        )
        if restoring:
            assert marker in json.dumps(received[-1]["messages"])
        assert result.success, (result, events)
        assert "AgentDock live answer" in result.output["output"]
        assert any(kind == "assistant_delta" for kind, _ in events)
        # Separate sessions in the SAME instance must not share chat history.
        other = {**args, "session_id": "isolated-session", "skills": []}
        result = await driver.run(task=TaskBody(prompt="Hello from a separate chat"), **other)
        assert result.success, result
        assert marker not in json.dumps(received[-1]["messages"])
        assert not (Path(workspace) / "nanobot/skills/dock-test").exists()
        await driver.close()
        driver = NanobotDriver()
        result = await driver.run(
            task=TaskBody(prompt="Recall the marker"), session_is_continuation=True, **args
        )
        assert result.success, (result, events)
        wire = json.dumps(received[-1]["messages"])
        assert marker in wire
        assert set(re.findall(r"DOCK-[0-9]+", wire)) == {marker}
        if persistent and not restoring:
            (Path(workspace) / "acceptance-ready").touch()
            async with asyncio.timeout(120):
                while not (Path(workspace) / "acceptance-resume").exists():  # noqa: ASYNC110 — host process signals via volume
                    await asyncio.sleep(0.1)
            result = await driver.run(
                task=TaskBody(prompt="After Docker pause"), session_is_continuation=True, **args
            )
            assert result.success, result
            assert marker in json.dumps(received[-1]["messages"])
        runtime = driver.runtimes[str(Path(workspace).resolve())]
        assert runtime.process is not None
        # Gateway children use the existing sandbox privilege-drop boundary.
        if os.geteuid() == 0 and not local_uid:
            status = Path(f"/proc/{runtime.process.pid}/status").read_text()
            assert f"Uid:\t{sandbox.AGENT_UID}\t" in status
    finally:
        await driver.close()
        server.close()
        await server.wait_closed()
        if not persistent:
            shutil.rmtree(workspace)
        shutil.rmtree(test_home)
