import http.client
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from proxy.policy import EgressPolicy, UpstreamPolicy, classify
from proxy.server import ProxyServer


@pytest.fixture
def relay(monkeypatch):
    seen = {}
    first_event = threading.Event()
    release_stream = threading.Event()

    class Origin(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            seen["body"] = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            seen["auth"] = self.headers.get("Authorization")
            seen["custom_auth"] = self.headers.get("X-Api-Key")
            self.send_response(200)
            if self.path == "/stream":
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                event = b"data: first\n\n"
                self.wfile.write(f"{len(event):x}\r\n".encode() + event + b"\r\n")
                self.wfile.flush()
                first_event.set()
                release_stream.wait(3)
                self.wfile.write(b"0\r\n\r\n")
            else:
                self.send_header("Content-Length", str(len(seen["body"])))
                self.end_headers()
                self.wfile.write(seen["body"])

        def log_message(self, *args):
            pass

    origin = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    threading.Thread(target=origin.serve_forever, daemon=True).start()
    real_connection = socket.create_connection

    def connect(address, timeout=None, source_address=None):
        if address[0] == "93.184.216.34":
            address = ("127.0.0.1", origin.server_address[1])
        return real_connection(address, timeout, source_address)

    monkeypatch.setattr(socket, "create_connection", connect)
    proxy = ProxyServer(
        ("127.0.0.1", 0), EgressPolicy(frozenset()), resolve=lambda _: ["93.184.216.34"]
    )
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    yield proxy.server_address[1], seen, first_event, proxy
    release_stream.set()
    proxy.shutdown()
    proxy.server_close()
    origin.shutdown()
    origin.server_close()


@pytest.mark.parametrize("chunked", [False, True])
def test_mcp_post_body_and_auth_survive_proxy(relay, chunked):
    port, seen, _, _ = relay
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    payload = b'{"jsonrpc":"2.0","method":"initialize","id":1}'
    conn.request(
        "POST",
        "http://mcp.test/mcp",
        body=iter([payload[:10], payload[10:]]) if chunked else payload,
        headers={"Authorization": "Bearer test", "X-Api-Key": "custom"},
        encode_chunked=chunked,
    )
    response = conn.getresponse()
    assert response.status == 200
    assert response.read() == payload
    assert seen == {"body": payload, "auth": "Bearer test", "custom_auth": "custom"}
    conn.close()


def test_mcp_sse_event_arrives_before_stream_ends(relay):
    port, _, first_event, _ = relay
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
    conn.request("POST", "http://mcp.test/stream", body=b"{}")
    response = conn.getresponse()
    assert first_event.wait(1)
    assert response.getheader("Content-Type") == "text/event-stream"
    assert response.read1(100) == b"data: first\n\n"
    conn.close()


def test_explicit_lan_allowlist_is_shared_and_does_not_unblock_metadata():
    policy = EgressPolicy.from_env({"EGRESS_ALLOW_CIDRS": "192.168.1.100/32 fd00::10/128"})
    assert classify("192.168.1.100", policy).allowed
    assert classify("fd00::10", policy).allowed
    assert not classify("192.168.1.101", policy).allowed
    assert not classify("127.0.0.1", policy).allowed
    assert not classify("169.254.169.254", policy).allowed
    assert not classify(
        "mcp.test", policy, resolve=lambda _: ["192.168.1.100", "192.168.1.101"]
    ).allowed


def test_allowed_lan_routes_direct_even_with_public_upstream(monkeypatch):
    from proxy.server import _Handler

    handler = object.__new__(_Handler)
    handler.server = type(
        "Server",
        (),
        {
            "upstream": UpstreamPolicy(
                "proxy.test",
                80,
                None,
                None,
                "all",
                frozenset(),
                False,
            )
        },
    )()
    assert handler._route("mcp.lan", "192.168.1.100") == "direct"


def test_post_does_not_retry_after_upstream_failure(relay, monkeypatch):
    from proxy.server import _Handler

    # Retrying POST could execute an MCP tool twice. Only GET/HEAD may fall back.
    port, _, _, proxy = relay
    proxy.upstream = UpstreamPolicy("proxy.test", 80, None, None, "all", frozenset(), True)
    handler_calls = []

    def fail(*args):
        handler_calls.append("upstream")
        raise OSError("failed")

    monkeypatch.setattr(_Handler, "_fetch_via_upstream", fail)
    monkeypatch.setattr(_Handler, "_fetch_direct", lambda *args: handler_calls.append("direct"))
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    conn.request("POST", "http://mcp.test/mcp", body=b"{}")
    assert conn.getresponse().status == 502
    assert handler_calls == ["upstream"]
    conn.close()
