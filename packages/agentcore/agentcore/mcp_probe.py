"""Short-lived MCP diagnostics. Discovers tools; never calls a business tool."""

from __future__ import annotations

import asyncio
import ssl
import time
from datetime import UTC, datetime
from typing import Any, cast

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agentcore.mcp_runtime import _headers
from agentcore.models import ShimMcpServer

MAX_TOOLS = 500
MAX_PAGES = 20


def _leaves(error: BaseException) -> list[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        return [leaf for nested in error.exceptions for leaf in _leaves(nested)]
    return [error]


def _failure(error: BaseException) -> tuple[str, str]:
    errors = _leaves(error)
    for err in errors:
        if isinstance(err, httpx.HTTPStatusError):
            status = err.response.status_code
            if status == 401:
                return "authentication_failed", "认证失败，请检查密钥。"
            if status == 403:
                if any(
                    x in err.response.text[:1024]
                    for x in (
                        "private_range",
                        "link_local",
                        "loopback",
                        "denylisted",
                    )
                ):
                    return "network_policy_blocked", "地址被出口策略阻止，请检查部署白名单。"
                return "permission_denied", "服务拒绝访问，请检查权限或出口策略。"
            if status in {301, 302, 303, 307, 308}:
                return "redirect_required", "服务返回重定向，请填写最终 MCP 地址。"
            if status == 404:
                return "endpoint_not_found", "未找到 MCP 端点，请检查 URL 路径。"
            return "http_error", f"服务或代理返回 HTTP {status}。"
    if any(isinstance(e, (TimeoutError, httpx.TimeoutException)) for e in errors):
        return "timeout", "测试超时，请检查网络或服务状态。"
    if any(isinstance(e, ssl.SSLError) or "CERTIFICATE_VERIFY_FAILED" in str(e) for e in errors):
        return "tls_error", "TLS 证书验证失败，请检查服务证书。"
    if any(isinstance(e, httpx.ConnectError) for e in errors):
        return "connection_failed", "连接失败，请检查地址、端口和出口代理。"
    return "protocol_error", "MCP 协议交互失败，请检查端点与 Streamable HTTP 支持。"


async def probe_mcp_server(
    server: ShimMcpServer,
    *,
    proxy_url: str | None = None,
    timeout_seconds: float = 15.0,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Use the same SDK/auth as the runtime, with an independent bounded session.

    A supplied http_client is owned by the caller (also useful for transport tests).
    Production callers select the deployment's filtering proxy explicitly.
    """
    from contextlib import AsyncExitStack

    started = time.monotonic()
    result: dict[str, Any] = {
        "status": "failed",
        "test_location": "control_plane",
        "network_route": "egress_proxy" if proxy_url else "direct",
        "stage": "initialize",
        "server_info": None,
        "protocol_version": None,
        "tools": [],
        "tool_count": 0,
        "tools_truncated": False,
        "warnings": [],
        "error": None,
        "tested_at": datetime.now(UTC).isoformat(),
    }
    http_errors: list[httpx.HTTPStatusError] = []

    async def observe_response(response: httpx.Response) -> None:
        # The SDK turns POST 404 into a generic session-terminated protocol
        # error. Preserve the HTTP cause for actionable diagnostics.
        if response.request.method == "POST" and response.status_code >= 300:
            await response.aread()
            http_errors.append(
                httpx.HTTPStatusError(
                    "MCP endpoint returned an error",
                    request=response.request,
                    response=response,
                )
            )

    try:
        async with asyncio.timeout(timeout_seconds), AsyncExitStack() as stack:
            client: httpx.AsyncClient = (
                http_client
                if http_client is not None
                else await stack.enter_async_context(
                    httpx.AsyncClient(
                        headers=_headers(server),
                        proxy=proxy_url,
                        trust_env=False,
                        timeout=httpx.Timeout(timeout_seconds, read=timeout_seconds),
                        follow_redirects=False,
                    )
                )
            )
            hooks = client.event_hooks.setdefault("response", [])
            hooks.append(observe_response)
            stack.callback(hooks.remove, observe_response)
            read, write, _ = await stack.enter_async_context(
                streamable_http_client(server.url, http_client=client)
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            initialized = await session.initialize()
            result["server_info"] = {
                "name": initialized.serverInfo.name[:200],
                "version": initialized.serverInfo.version[:100],
            }
            result["protocol_version"] = initialized.protocolVersion
            result["stage"] = "tools_list"
            if initialized.capabilities.tools is not None:
                cursor = None
                seen_cursors: set[str] = set()
                for _ in range(MAX_PAGES):
                    page = await session.list_tools(cursor=cursor)
                    available = MAX_TOOLS - len(result["tools"])
                    result["tools"].extend(
                        {
                            "name": tool.name[:200],
                            "description": (tool.description or "")[:500],
                        }
                        for tool in page.tools[:available]
                    )
                    cursor = page.nextCursor
                    if len(page.tools) > available or (
                        cursor and len(result["tools"]) >= MAX_TOOLS
                    ):
                        result["tools_truncated"] = True
                        break
                    if not cursor:
                        break
                    if cursor in seen_cursors:
                        raise ValueError("repeated pagination cursor")
                    seen_cursors.add(cursor)
                else:
                    result["tools_truncated"] = bool(cursor)
            else:
                result["warnings"].append("服务未声明工具能力。")
            if not result["tools"] and initialized.capabilities.tools is not None:
                result["warnings"].append("服务当前未提供工具。")
            if result["tools_truncated"]:
                result["warnings"].append("工具预览达到上限，数量仅包含已读取的工具。")
            result["status"] = "success"
            result["stage"] = "complete"
    except Exception as exc:  # SDK failures often arrive as exception groups
        code, message = _failure(http_errors[0] if http_errors else exc)
        result["error"] = {"code": code, "message": message}
        if result["server_info"] is not None:
            result["status"] = "partial"
    result["tool_count"] = len(result["tools"])
    result["duration_ms"] = round((time.monotonic() - started) * 1000)

    # Remote metadata is untrusted too: it must not reflect a submitted token.
    def scrub(value: Any) -> Any:
        if isinstance(value, str):
            return value.replace(server.secret, "[redacted]") if server.secret else value
        if isinstance(value, list):
            return [scrub(v) for v in value]
        if isinstance(value, dict):
            return {k: scrub(v) for k, v in value.items()}
        return value

    return cast(dict[str, Any], scrub(result))
