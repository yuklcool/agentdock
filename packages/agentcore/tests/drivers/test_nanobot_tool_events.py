from __future__ import annotations

import json
from typing import Any

import pytest

from agentcore.drivers.nanobot import NanobotDriver

pytestmark = pytest.mark.unit


class FakeWebSocket:
    def __init__(self, frames: list[dict[str, Any]]) -> None:
        self.frames = list(frames)

    async def recv(self) -> str:
        assert self.frames, "consume() requested more frames than the test supplied"
        return json.dumps(self.frames.pop(0))


async def test_structured_tool_events_are_normalized_for_timeline() -> None:
    frames: list[dict[str, Any]] = [
        {
            "event": "message",
            "chat_id": "other-chat",
            "kind": "tool_hint",
            "text": "must not leak",
            "tool_events": [
                {
                    "phase": "start",
                    "call_id": "leak-call",
                    "name": "secret_tool",
                    "arguments": {"secret": True},
                }
            ],
        },
        {
            "event": "message",
            "chat_id": "chat-1",
            "kind": "tool_hint",
            "text": "",
            "tool_events": [
                {
                    "version": 1,
                    "phase": "start",
                    "call_id": "call-read",
                    "name": "read_file",
                    "arguments": {"path": "README.md"},
                },
                {
                    "version": 1,
                    "phase": "start",
                    "call_id": "call-db",
                    "name": "query_database",
                    "arguments": {"sql": "SELECT COUNT(*) FROM light_pole"},
                },
            ],
        },
        {
            "event": "message",
            "chat_id": "chat-1",
            "kind": "progress",
            "text": "",
            "tool_events": [
                {
                    "version": 1,
                    "phase": "end",
                    "call_id": "call-read",
                    "name": "read_file",
                    "result": {"lines": 42, "ok": True},
                    "duration_ms": 17,
                },
                {
                    "version": 1,
                    "phase": "error",
                    "call_id": "call-db",
                    "name": "query_database",
                    "error": {"message": "database unavailable"},
                },
            ],
        },
        {
            "event": "file_edit",
            "chat_id": "chat-1",
            "phase": "end",
            "edits": [
                {
                    "path": "src/app.py",
                    "operation": "modify",
                    "diff": "@@ -1 +1 @@",
                    "added": 1,
                    "deleted": 1,
                }
            ],
        },
        {"event": "delta", "chat_id": "chat-1", "stream_id": "s1", "text": "Done"},
        {
            "event": "stream_end",
            "chat_id": "chat-1",
            "stream_id": "s1",
            "text": "Done",
        },
        {
            "event": "turn_end",
            "chat_id": "chat-1",
            "usage": {"prompt_tokens": 10, "completion_tokens": 3},
        },
    ]
    ws = FakeWebSocket(frames)
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        events.append((kind, payload))

    output = await NanobotDriver.consume(ws, "chat-1", emit)  # type: ignore[arg-type]

    assert output == "Done"

    tool_calls = [payload for kind, payload in events if kind == "tool_call"]
    assert tool_calls == [
        {
            "tool_use_id": "call-read",
            "name": "read_file",
            "input": {"path": "README.md"},
        },
        {
            "tool_use_id": "call-db",
            "name": "query_database",
            "input": {"sql": "SELECT COUNT(*) FROM light_pole"},
        },
    ]

    tool_results = [payload for kind, payload in events if kind == "tool_result"]
    assert len(tool_results) == 2
    assert tool_results[0]["tool_use_id"] == "call-read"
    assert tool_results[0]["ok"] is True
    assert tool_results[0]["duration_ms"] == 17
    assert json.loads(tool_results[0]["content"]) == {"lines": 42, "ok": True}
    assert tool_results[1]["tool_use_id"] == "call-db"
    assert tool_results[1]["ok"] is False
    assert tool_results[1]["duration_ms"] >= 0
    assert json.loads(tool_results[1]["content"]) == {"message": "database unavailable"}

    file_events = [payload for kind, payload in events if kind == "file_changed"]
    assert file_events == [
        {
            "operation": "modify",
            "path": "src/app.py",
            "diff": "@@ -1 +1 @@",
            "added": 1,
            "deleted": 1,
        }
    ]

    assert not [payload for kind, payload in events if kind == "log" and not payload["message"]]
    assert "secret_tool" not in json.dumps(events)


async def test_progress_without_structured_tool_event_keeps_legacy_log() -> None:
    ws = FakeWebSocket(
        [
            {
                "event": "message",
                "chat_id": "chat-1",
                "kind": "progress",
                "text": "Working",
            },
            {"event": "turn_end", "chat_id": "chat-1", "usage": {}},
        ]
    )
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        events.append((kind, payload))

    await NanobotDriver.consume(ws, "chat-1", emit)  # type: ignore[arg-type]
    assert ("log", {"level": "info", "message": "Working"}) in events
