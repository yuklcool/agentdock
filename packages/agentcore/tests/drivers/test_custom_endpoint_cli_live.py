"""Optional native CLI checks against a loopback-only API (no real key/cost).

Set AGENTDOCK_TEST_CLI_DIR to a directory containing the image-pinned opencode
and codex binaries to run these tests. Unit CI without those binaries skips.
"""

import asyncio
import json
import os
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agentcore import sandbox
from agentcore.drivers.codex import CodexDriver
from agentcore.drivers.opencode import OpencodeDriver
from agentcore.models import AgentConfig, ResolvedLimits, TaskBody


@pytest.fixture
def gateway():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "object": "list",
                        "data": [{"id": "gpt-4o", "object": "model", "owned_by": "test"}],
                    }
                ).encode()
            )

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, self.headers.get("Authorization"), body))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.path.endswith("/chat/completions"):
                chunks = [
                    {
                        "id": "chat_1",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": body["model"],
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": "gateway-reply"},
                                "finish_reason": None,
                            }
                        ],
                    },
                    {
                        "id": "chat_1",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": body["model"],
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    },
                ]
                for chunk in chunks:
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
            elif self.path.endswith("/responses"):
                item = {
                    "id": "msg_1",
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": "gateway-reply", "annotations": []}
                    ],
                }
                response = {
                    "id": "resp_1",
                    "object": "response",
                    "created_at": 0,
                    "status": "completed",
                    "model": body["model"],
                    "output": [item],
                    "usage": {
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "total_tokens": 2,
                        "input_tokens_details": {"cached_tokens": 0},
                        "output_tokens_details": {"reasoning_tokens": 0},
                    },
                }
                events = [
                    {
                        "type": "response.created",
                        "response": {**response, "status": "in_progress", "output": []},
                    },
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {**item, "status": "in_progress", "content": []},
                    },
                    {
                        "type": "response.output_text.delta",
                        "item_id": "msg_1",
                        "output_index": 0,
                        "content_index": 0,
                        "delta": "gateway-reply",
                    },
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                    {"type": "response.completed", "response": response},
                ]
                for event in events:
                    self.wfile.write(
                        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
                    )
            self.wfile.flush()
            self.close_connection = True

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/api", requests
    server.shutdown()
    server.server_close()


@pytest.fixture
def native_cli(monkeypatch, tmp_path):
    directory = os.environ.get("AGENTDOCK_TEST_CLI_DIR")
    if not directory:
        pytest.skip("set AGENTDOCK_TEST_CLI_DIR for image-pinned native CLI smoke tests")
    monkeypatch.setenv("PATH", f"{directory}:{os.environ['PATH']}")
    for name in ("codex", "opencode"):
        if not shutil.which(name):
            pytest.skip(f"native CLI {name} not installed")
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OPENCODE_DISABLE_MODELS_FETCH", "1")
    monkeypatch.setenv("OPENCODE_DISABLE_DEFAULT_PLUGINS", "1")
    monkeypatch.setenv("OPENCODE_DISABLE_AUTOUPDATE", "1")
    # Test only transport/config generation, not container uid provisioning.
    # Some managed test filesystems cannot chown or spawn with another uid.
    monkeypatch.setattr(sandbox, "chown_to_agent", lambda path: None)
    monkeypatch.setattr(
        sandbox,
        "ensure_agent_dir",
        lambda path, **kw: Path(path).mkdir(parents=True, exist_ok=True),
    )
    monkeypatch.setattr(
        sandbox, "makedirs_agent", lambda path, **kw: Path(path).mkdir(parents=True, exist_ok=True)
    )

    async def spawn(command, **kwargs):
        return await asyncio.create_subprocess_exec(*command, **kwargs)

    monkeypatch.setattr(sandbox, "spawn_untrusted", spawn)
    # These tests request no tools. Use the most restricted native sandbox so
    # host-managed Codex policies need not allow the container's full-access mode.
    monkeypatch.setattr(
        "agentcore.drivers.codex._THREAD_ACCESS",
        {
            "approvalPolicy": "on-request",
            "sandbox": "read-only",
        },
    )


@pytest.mark.parametrize(
    "driver_type, suffix", [(OpencodeDriver, "/chat/completions"), (CodexDriver, "/responses")]
)
async def test_native_cli_requests_custom_endpoint_with_key(
    native_cli,
    gateway,
    tmp_path,
    driver_type,
    suffix,
):
    base_url, requests = gateway
    events = []

    async def emit(kind, payload):
        events.append((kind, payload))

    driver = driver_type()
    config = AgentConfig(driver=driver.name, model="gpt-4o", tools=[], hot_spare=False)
    result = await driver.run(
        task=TaskBody(prompt="Say hello."),
        config=config,
        limits=ResolvedLimits(max_iterations=3, max_tokens=10000, timeout_seconds=40),
        credential="gateway-smoke-key",
        credential_meta={"base_url": base_url},
        emit=emit,
        cancel=asyncio.Event(),
        workspace=str(tmp_path),
        env={
            "OPENCODE_DISABLE_MODELS_FETCH": "1",
            "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
        },
    )
    assert result.success, (result.reason, events[-5:])
    assert requests and all(path == f"/api{suffix}" for path, _, _ in requests)
    assert all(auth == "Bearer gateway-smoke-key" for _, auth, _ in requests)
    assert all(body["model"] == "gpt-4o" for _, _, body in requests)
    assert "gateway-reply" in str(result.output)
    assert "gateway-smoke-key" not in str(events)
