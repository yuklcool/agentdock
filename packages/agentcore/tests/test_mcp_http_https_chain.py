"""Real TLS + filtering proxy + SDK handshake/discovery/call, without Docker."""

import asyncio
import datetime
import socket
import ssl
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from agentcore.mcp_probe import probe_mcp_server
from agentcore.mcp_runtime import McpRuntime
from agentcore.models import ShimMcpServer
from agentcore.tools.base import ToolContext


@pytest.fixture(scope="module")
def endpoints(tmp_path_factory):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings

    directory = tmp_path_factory.mktemp("mcp-tls")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mcp.test")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("mcp.test")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "cert.pem"
    key_path = directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    servers = []
    result = {"cert": str(cert_path)}
    for scheme in ("http", "https"):
        mcp = FastMCP(
            f"stub-{scheme}",
            stateless_http=True,
            transport_security=TransportSecuritySettings(
                allowed_hosts=["mcp.test:*"], allowed_origins=[]
            ),
        )

        @mcp.tool()
        def echo(text: str) -> str:
            """Return the provided text."""
            return f"echo:{text}"

        app = mcp.streamable_http_app()
        captured = {}
        result[f"headers_{scheme}"] = captured

        async def observed(scope, receive, send, inner=app, seen=captured):
            if scope["type"] == "http":
                seen.clear()
                seen.update(
                    {key.decode(): value.decode() for key, value in scope.get("headers", [])}
                )
            await inner(scope, receive, send)

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        tls = (
            {"ssl_certfile": str(cert_path), "ssl_keyfile": str(key_path)}
            if scheme == "https"
            else {}
        )
        server = uvicorn.Server(uvicorn.Config(observed, log_level="error", **tls))
        thread = threading.Thread(target=lambda s=server, so=sock: s.run(sockets=[so]), daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while not server.started:
            if time.monotonic() > deadline:
                raise TimeoutError("MCP fixture did not start")
            time.sleep(0.01)
        result[scheme] = f"{scheme}://mcp.test:{sock.getsockname()[1]}/mcp"
        servers.append((server, thread))
    yield result
    for server, thread in servers:
        server.should_exit = True
        thread.join(5)


@pytest.fixture
def proxy_chain(endpoints, monkeypatch):
    monkeypatch.syspath_prepend(
        str(Path(__file__).resolve().parents[3] / "images" / "egress-proxy")
    )
    from proxy.policy import EgressPolicy
    from proxy.server import ProxyServer

    original_connect = socket.create_connection

    def connect(address, timeout=None, source_address=None):
        if address[0] == "93.184.216.34":
            address = ("127.0.0.1", address[1])
        return original_connect(address, timeout, source_address)

    monkeypatch.setattr(socket, "create_connection", connect)
    server = ProxyServer(
        ("127.0.0.1", 0), EgressPolicy(frozenset()), resolve=lambda _: ["93.184.216.34"]
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    proxy_url = f"http://127.0.0.1:{server.server_address[1]}"
    for variable in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.setenv(variable, proxy_url)
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    ssl_context = ssl.create_default_context(cafile=endpoints["cert"])
    original_client = httpx.AsyncClient

    def client(**kwargs):
        kwargs.setdefault("verify", ssl_context)
        return original_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    yield proxy_url
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize("auth_type", ["none", "bearer", "header"])
async def test_probe_and_real_tool_call_through_proxy(
    endpoints, proxy_chain, scheme, auth_type, tmp_path
):
    spec = ShimMcpServer(
        name="stub",
        url=endpoints[scheme],
        auth_type=auth_type,
        auth_header_name="X-Api-Key" if auth_type == "header" else "",
        secret="test-token" if auth_type != "none" else "",
    )
    diagnostic = await probe_mcp_server(spec, proxy_url=proxy_chain)
    assert diagnostic["status"] == "success", diagnostic
    assert diagnostic["tool_count"] == 1
    runtime = McpRuntime(connect_timeout=3)
    try:
        await runtime.connect([spec])
        assert not runtime.errors, runtime.errors
        adapter = runtime.tools()[0]
        result = await adapter.run(
            {"text": "round-trip"},
            ToolContext(
                workspace=str(tmp_path),
                cancel=asyncio.Event(),
            ),
        )
        assert result.ok and "echo:round-trip" in result.content
        headers = endpoints[f"headers_{scheme}"]
        if auth_type == "bearer":
            assert headers.get("authorization") == "Bearer test-token"
        elif auth_type == "header":
            assert headers.get("x-api-key") == "test-token"
        else:
            assert "authorization" not in headers and "x-api-key" not in headers
    finally:
        await runtime.close()


async def test_https_invalid_certificate_fails_with_tls_diagnostic(endpoints, proxy_chain):
    # Supply a client with the normal public trust store, which rejects the local test CA.
    async with httpx.AsyncClient(proxy=proxy_chain, trust_env=False, verify=True) as client:
        diagnostic = await probe_mcp_server(
            ShimMcpServer(name="stub", url=endpoints["https"]),
            http_client=client,
        )
    assert diagnostic["status"] == "failed"
    assert diagnostic["error"]["code"] == "tls_error"
