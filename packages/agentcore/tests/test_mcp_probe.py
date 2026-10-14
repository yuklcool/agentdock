import asyncio

import httpx
import pytest

from agentcore.mcp_probe import probe_mcp_server
from agentcore.models import ShimMcpServer


def _server(url="http://mcp.test/mcp", **kwargs):
    return ShimMcpServer(name="test", url=url, **kwargs)


def _transport(*, capabilities=None, list_error=False, repeating=False, empty=False):
    methods = []

    def handle(request):
        import json

        if request.method != "POST":
            return httpx.Response(405)
        body = json.loads(request.content)
        methods.append(body["method"])
        if "id" not in body:
            return httpx.Response(202)
        if body["method"] == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": capabilities if capabilities is not None else {"tools": {}},
                "serverInfo": {"name": "stub", "version": "1.0"},
            }
        elif body["method"] == "tools/list":
            if list_error:
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "error": {"code": -32603, "message": "failure"},
                    },
                )
            cursor = body.get("params", {}).get("cursor")
            result = {
                "tools": []
                if empty
                else [{"name": "echo2" if cursor else "echo", "inputSchema": {"type": "object"}}]
            }
            if not cursor or repeating:
                result["nextCursor"] = "page2"
        else:
            raise AssertionError(f"Unexpected method {body['method']}")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    return httpx.MockTransport(handle), methods


@pytest.mark.parametrize("scheme", ["http", "https"])
async def test_protocol_handshake_and_pagination_match_for_both_schemes(scheme):
    transport, methods = _transport()
    async with httpx.AsyncClient(transport=transport) as client:
        result = await probe_mcp_server(_server(f"{scheme}://mcp.test/mcp"), http_client=client)
    assert result["status"] == "success"
    assert result["tool_count"] == 2
    assert [t["name"] for t in result["tools"]] == ["echo", "echo2"]
    assert methods[:2] == ["initialize", "notifications/initialized"]
    assert "tools/call" not in methods


async def test_no_tools_capability_is_successful_handshake():
    transport, methods = _transport(capabilities={})
    async with httpx.AsyncClient(transport=transport) as client:
        result = await probe_mcp_server(_server(), http_client=client)
    assert result["status"] == "success" and result["tool_count"] == 0
    assert result["warnings"]
    assert "tools/list" not in methods


@pytest.mark.parametrize("kwargs", [{"list_error": True}, {"repeating": True}])
async def test_discovery_failure_preserves_handshake_result(kwargs):
    transport, _ = _transport(**kwargs)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await probe_mcp_server(_server(), http_client=client)
    assert result["status"] == "partial"
    assert result["stage"] == "tools_list"
    assert result["server_info"]["name"] == "stub"


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (401, "token-reflected", "authentication_failed"),
        (403, "private_range", "network_policy_blocked"),
        (403, "forbidden", "permission_denied"),
        (404, "missing", "endpoint_not_found"),
        (307, "", "redirect_required"),
        (500, "token-reflected", "http_error"),
    ],
)
async def test_errors_are_classified_and_never_echo_remote_secrets(status, body, code):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            status, text=body, headers={"location": "https://other.test"}
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        result = await probe_mcp_server(_server(secret="token-reflected"), http_client=client)
    assert result["status"] == "failed"
    assert result["error"]["code"] == code
    assert "token-reflected" not in str(result)


async def test_timeout_releases_the_session():
    async def handle(request):
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await probe_mcp_server(_server(), http_client=client, timeout_seconds=0.05)
    assert result["error"]["code"] == "timeout"
    assert result["duration_ms"] < 1000


@pytest.mark.parametrize("kwargs", [{}, {"capabilities": {}}, {"repeating": True}])
async def test_runtime_and_diagnostic_use_complete_capability_aware_discovery(monkeypatch, kwargs):
    from agentcore.mcp_runtime import McpRuntime

    transport, _ = _transport(**kwargs)
    original_client = httpx.AsyncClient

    def client(**options):
        options["transport"] = transport
        options["trust_env"] = False
        return original_client(**options)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    runtime = McpRuntime(connect_timeout=1)
    try:
        await runtime.connect([_server()])
        if kwargs.get("repeating"):
            assert runtime.errors and not runtime.tools()
        elif kwargs.get("capabilities") == {}:
            assert not runtime.errors and not runtime.tools()
        else:
            assert not runtime.errors
            assert [tool.spec.name for tool in runtime.tools()] == [
                "mcp__test__echo",
                "mcp__test__echo2",
            ]
    finally:
        await runtime.close()
