"""A small filtering forward proxy.

Handles two request shapes:
  * plain HTTP proxy:  `GET http://host/path HTTP/1.1`  -> we fetch and relay.
  * HTTPS tunnel:      `CONNECT host:443 HTTP/1.1`       -> we splice raw bytes.

Every destination host is classified by proxy.policy.classify before any
outbound connection is made. Blocked hosts get a 403; allowed hosts are
relayed and logged. Threaded one-thread-per-connection; this proxy serves a
single host's handful of agent containers, not internet-scale traffic.

An allowed request is then *routed*: straight to the origin, or chained through
a configured upstream proxy (a rotating service such as Webshare) per
``UpstreamPolicy``. Routing never overrides the allow/block decision — a blocked
host is blocked whether or not an upstream is configured.
"""

from __future__ import annotations

import http.client
import ipaddress
import select
import socket
import socketserver
import sys
import urllib.parse

from proxy.logfmt import log_line
from proxy.policy import (
    Decision,
    EgressPolicy,
    Resolver,
    UpstreamPolicy,
    _system_resolve,
    classify,
)

_HEADER_LINE_LIMIT_BYTES = 64 * 1024
_TUNNEL_BUFFER_BYTES = 64 * 1024
_CONNECT_TIMEOUT_SECONDS = 10
_TUNNEL_IDLE_TIMEOUT_SECONDS = 60
_HTTP_TIMEOUT_SECONDS = 30
_MAX_REQUEST_BODY_BYTES = 8 * 1024 * 1024
_HOP_HEADERS = {
    "connection",
    "proxy-connection",
    "keep-alive",
    "transfer-encoding",
    "te",
    "trailer",
    "upgrade",
    "proxy-authorization",
    "proxy-authenticate",
}


class _Handler(socketserver.StreamRequestHandler):
    server: ProxyServer

    def handle(self) -> None:
        try:
            request_line = self.rfile.readline(_HEADER_LINE_LIMIT_BYTES).decode("latin-1").strip()
        except OSError:
            return
        if not request_line:
            return
        parts = request_line.split(" ")
        if len(parts) != 3:
            self._respond(400, b"bad request line")
            return
        method, target, _version = parts

        # Drain the rest of the request headers (we only need the first line
        # for routing; plain-HTTP relay re-issues a clean request via urllib).
        try:
            headers = self._read_headers()
        except ValueError:
            self._respond(400, b"invalid headers")
            return

        if method == "CONNECT":
            self._handle_connect(target)
        else:
            self._handle_plain(method, target, headers)

    def _read_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        while True:
            line = self.rfile.readline(_HEADER_LINE_LIMIT_BYTES).decode("latin-1")
            if line in ("\r\n", "\n", ""):
                break
            if ":" in line:
                k, _, v = line.partition(":")
                if k.strip().lower() in {"content-length", "transfer-encoding"} and (
                    k.strip().lower() in headers
                ):
                    raise ValueError("duplicate framing header")
                headers[k.strip().lower()] = v.strip()
        return headers

    def _respond(self, status: int, body: bytes = b"") -> None:
        reason = {
            200: "OK",
            400: "Bad Request",
            403: "Forbidden",
            413: "Payload Too Large",
            502: "Bad Gateway",
        }.get(status, "Error")
        self.wfile.write(f"HTTP/1.1 {status} {reason}\r\n".encode())
        self.wfile.write(b"Content-Type: text/plain\r\n")
        self.wfile.write(f"Content-Length: {len(body)}\r\n".encode())
        self.wfile.write(b"Connection: close\r\n\r\n")
        if body:
            self.wfile.write(body)

    def _resolve_upstream_ip(self, host: str) -> tuple[Decision, str | None]:
        # Denylist before DNS. For hostnames, resolve exactly once, then pass the
        # cached result into classify so the decision and the connection target
        # are tied to the same DNS answer set.
        if host.lower() in self.server.policy.denylist:
            return Decision(False, "denylisted"), None
        try:
            ipaddress.ip_address(host)
            decision = classify(host, self.server.policy, resolve=self.server.resolve)
            return (decision, host) if decision.allowed else (decision, None)
        except ValueError:
            pass
        try:
            ips = self.server.resolve(host)
        except OSError:
            return Decision(False, "dns_failure"), None
        if not ips:
            return Decision(False, "dns_failure"), None
        decision = classify(host, self.server.policy, resolve=lambda _host: ips)
        return (decision, ips[0]) if decision.allowed else (decision, None)

    def _emit(self, decision: Decision, method: str, host: str, port: int, route: str) -> None:
        sys.stdout.write(
            log_line(
                level="info" if decision.allowed else "warn",
                msg="egress",
                decision="allow" if decision.allowed else "block",
                reason=decision.reason,
                method=method,
                host=host,
                port=port,
                route=route,
            )
            + "\n"
        )
        sys.stdout.flush()

    def _route(self, host: str, origin_ip: str | None = None) -> str:
        """ "upstream" if this host chains through the configured proxy, else "direct"."""
        up = self.server.upstream
        if origin_ip and ipaddress.ip_address(origin_ip).is_private:
            return (
                "direct"  # explicitly allowed LAN targets are local, never sent to public proxies
            )
        return "upstream" if up is not None and up.route(host) else "direct"

    def _emit_fallback(self, method: str, host: str, port: int) -> None:
        # A security-relevant downgrade: the upstream failed and, because the
        # operator opted in, this request is going direct — leaking the VM's IP.
        # Always log it so the fallback is visible, not silent.
        sys.stdout.write(
            log_line(
                level="warn",
                msg="egress_upstream_fallback",
                method=method,
                host=host,
                port=port,
                route="direct",
            )
            + "\n"
        )
        sys.stdout.flush()

    def _connect_target(self, host: str, port: int, origin_ip: str, route: str) -> socket.socket:
        """Open the outbound socket for a CONNECT tunnel: via the upstream when
        routed there, else straight to the origin. If the upstream dial fails and
        fallback-direct is enabled, dial the origin instead (and log the downgrade)."""
        if route != "upstream":
            return socket.create_connection((origin_ip, port), timeout=_CONNECT_TIMEOUT_SECONDS)
        try:
            return self._dial_upstream(host, port)
        except OSError:
            if not self.server.upstream.fallback_direct:
                raise  # fail closed: 502, never a silent direct dial
            self._emit_fallback("CONNECT", host, port)
            return socket.create_connection((origin_ip, port), timeout=_CONNECT_TIMEOUT_SECONDS)

    def _dial_upstream(self, host: str, port: int) -> socket.socket:
        """Open a tunnel to host:port *through* the configured upstream proxy.

        A fresh connection per request is what gives us IP rotation for free:
        rotating services hand out a new exit IP per upstream connection.

        Note: DNS for `host` is resolved by the upstream's exit node, so the local
        classify() above is a best-effort pre-check rather than a guarantee that
        the IP we classified is the IP that gets connected. That is acceptable —
        the exit nodes are on the public internet and cannot reach this VM's
        private ranges or metadata endpoint, which is what the IP checks defend.
        Denylisted hostnames are still enforced locally, before we get here.
        """
        up: UpstreamPolicy = self.server.upstream
        sock = socket.create_connection((up.host, up.port), timeout=_CONNECT_TIMEOUT_SECONDS)
        try:
            req = [f"CONNECT {host}:{port} HTTP/1.1", f"Host: {host}:{port}"]
            auth = up.proxy_authorization()
            if auth:
                req.append(f"Proxy-Authorization: {auth}")
            sock.sendall(("\r\n".join(req) + "\r\n\r\n").encode("latin-1"))

            reader = sock.makefile("rb")
            status_line = reader.readline(_HEADER_LINE_LIMIT_BYTES).decode("latin-1")
            parts = status_line.split(" ", 2)
            if len(parts) < 2 or parts[1] != "200":
                raise OSError(f"upstream refused CONNECT: {status_line.strip()!r}")
            # Drain the upstream's response headers so only tunnelled bytes remain.
            while True:
                line = reader.readline(_HEADER_LINE_LIMIT_BYTES).decode("latin-1")
                if line in ("\r\n", "\n", ""):
                    break
            reader.close()  # detach the buffered wrapper; we splice the raw socket
        except OSError:
            sock.close()
            raise
        return sock

    # ---- HTTPS tunnelling -------------------------------------------------
    def _handle_connect(self, authority: str) -> None:
        try:
            parsed = urllib.parse.urlsplit("//" + authority)
            host, port = parsed.hostname or "", parsed.port or 443
        except ValueError:
            self._respond(400, b"invalid CONNECT target")
            return
        decision, origin_ip = self._resolve_upstream_ip(host)
        route = self._route(host, origin_ip)
        self._emit(decision, "CONNECT", host, port, route)
        if not decision.allowed or origin_ip is None:
            self._respond(403, decision.reason.encode())
            return
        try:
            upstream = self._connect_target(host, port, origin_ip, route)
        except OSError:
            self._respond(502, b"upstream connect failed")
            return
        # RFC 7231 §4.3.6: a 2xx CONNECT switches the connection to tunnel mode.
        # The response MUST be a bare status line with NO entity headers — the
        # generic _respond() adds Content-Length:0 + Connection: close, which
        # strict clients (Bun's fetch, used by opencode) treat as a non-tunnel /
        # closing connection, breaking their HTTPS requests over the tunnel.
        self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        self.wfile.flush()
        self._splice(self.connection, upstream)

    def _splice(self, a: socket.socket, b: socket.socket) -> None:
        # Full-duplex byte relay with correct TCP half-close handling: when one
        # side closes its write half (EOF), forward that as a half-close to the
        # peer and stop reading the closed side, but KEEP relaying the other
        # direction. Tearing the whole tunnel down on the first EOF (the old
        # behaviour) truncates an in-flight response when a client half-closes
        # its send side after sending its request — which Bun's fetch does, so
        # opencode saw empty/'Cache input stream was empty' response bodies.
        sockets = [a, b]
        try:
            while sockets:
                readable, _, errored = select.select(
                    sockets,
                    [],
                    sockets,
                    _TUNNEL_IDLE_TIMEOUT_SECONDS,
                )
                if errored or not readable:
                    break
                for s in readable:
                    other = b if s is a else a
                    data = s.recv(_TUNNEL_BUFFER_BYTES)
                    if not data:
                        try:
                            other.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass
                        sockets.remove(s)
                        continue
                    other.sendall(data)
        except OSError:
            return
        finally:
            try:
                b.close()
            except OSError:
                pass

    # ---- plain HTTP relay -------------------------------------------------
    def _request_body(self, headers: dict[str, str]) -> bytes:
        """Decode bounded HTTP request framing, including streamed/chunked SDK bodies."""
        self.connection.settimeout(_HTTP_TIMEOUT_SECONDS)
        transfer = headers.get("transfer-encoding", "").lower()
        if transfer and (transfer != "chunked" or "content-length" in headers):
            raise ValueError("invalid request framing")
        length = int(headers.get("content-length", "0"))
        if length < 0:
            raise ValueError("negative content length")
        if length > _MAX_REQUEST_BODY_BYTES:
            raise OverflowError("request body too large")
        if headers.get("expect", "").lower() == "100-continue":
            self.wfile.write(b"HTTP/1.1 100 Continue\r\n\r\n")
            self.wfile.flush()
        if not transfer:
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("incomplete request body")
            return body
        body = bytearray()
        while True:
            line = self.rfile.readline(_HEADER_LINE_LIMIT_BYTES)
            if not line.endswith(b"\r\n"):
                raise ValueError("invalid chunk header")
            size = int(line.split(b";", 1)[0].strip(), 16)
            if size < 0:
                raise ValueError("invalid chunk size")
            if size + len(body) > _MAX_REQUEST_BODY_BYTES:
                raise OverflowError("request body too large")
            if size == 0:
                # Discard bounded trailers; forwarding them would change auth/framing.
                total = 0
                while True:
                    trailer = self.rfile.readline(_HEADER_LINE_LIMIT_BYTES)
                    total += len(trailer)
                    if not trailer or total > _HEADER_LINE_LIMIT_BYTES:
                        raise ValueError("invalid trailers")
                    if trailer == b"\r\n":
                        return bytes(body)
            chunk = self.rfile.read(size)
            if len(chunk) != size or self.rfile.read(2) != b"\r\n":
                raise ValueError("incomplete chunk")
            body.extend(chunk)

    def _handle_plain(self, method: str, target: str, headers: dict[str, str]) -> None:
        try:
            parsed = urllib.parse.urlsplit(target)
            host = parsed.hostname or ""
            port = parsed.port or 80
            if parsed.scheme != "http" or not host or parsed.username is not None:
                raise ValueError("invalid proxy target")
        except ValueError:
            self._respond(400, b"invalid HTTP target")
            return
        decision, origin_ip = self._resolve_upstream_ip(host)
        route = self._route(host, origin_ip)
        self._emit(decision, method, host, port, route)
        if not decision.allowed or origin_ip is None:
            self._respond(403, decision.reason.encode())
            return
        # Re-issue a clean HTTP request while preserving Host. We intentionally do
        # not auto-follow redirects; if a client follows one, it comes back through
        # this proxy and is classified again. Incoming header keys are lowercased,
        # so "host" is dropped here and re-added as the canonical "Host" — otherwise
        # both would be forwarded and strict upstreams reject the duplicate with 400.
        try:
            body = self._request_body(headers)
        except OverflowError:
            self._respond(413, b"request body too large")
            return
        except (ValueError, OSError):
            self._respond(400, b"invalid request body")
            return
        connection_headers = {h.strip().lower() for h in headers.get("connection", "").split(",")}
        excluded = _HOP_HEADERS | connection_headers | {"host", "content-length", "expect"}
        fwd_headers = {k: v for k, v in headers.items() if k not in excluded}
        host_header = f"[{host}]" if ":" in host else host
        fwd_headers["Host"] = host_header if port == 80 else f"{host_header}:{port}"
        if body or "content-length" in headers or "transfer-encoding" in headers:
            fwd_headers["Content-Length"] = str(len(body))
        origin_path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))

        # Only connection/response headers are obtained before committing a reply.
        # The body is relayed immediately: MCP SSE streams need not reach EOF.
        try:
            if route == "upstream":
                try:
                    conn, resp = self._fetch_via_upstream(method, target, fwd_headers, body)
                except Exception:  # noqa: BLE001 — any upstream transport failure
                    if not self.server.upstream.fallback_direct or method not in {"GET", "HEAD"}:
                        raise  # fail closed
                    self._emit_fallback(method, host, port)
                    conn, resp = self._fetch_direct(
                        method, origin_ip, port, origin_path, fwd_headers, body
                    )
            else:
                conn, resp = self._fetch_direct(
                    method, origin_ip, port, origin_path, fwd_headers, body
                )
        except Exception:  # noqa: BLE001 — nothing reachable -> 502
            self._respond(502, b"upstream error")
            return
        try:
            self._write_plain_response(resp)
        except (OSError, http.client.HTTPException):
            pass  # headers already sent; close instead of appending a second HTTP response
        finally:
            resp.close()
            conn.close()

    def _fetch_via_upstream(
        self, method: str, absolute_uri: str, fwd_headers: dict[str, str], body: bytes = b""
    ) -> tuple:
        # Proxy request form: dial the upstream and ask for the absolute URI.
        up_policy: UpstreamPolicy = self.server.upstream
        headers = dict(fwd_headers)
        auth = up_policy.proxy_authorization()
        if auth:
            headers["Proxy-Authorization"] = auth
        return self._fetch_direct(
            method, up_policy.host, up_policy.port, absolute_uri, headers, body
        )

    def _fetch_direct(
        self,
        method: str,
        conn_host: str,
        conn_port: int,
        path: str,
        fwd_headers: dict[str, str],
        body: bytes = b"",
    ) -> tuple:
        conn = http.client.HTTPConnection(conn_host, conn_port, timeout=_HTTP_TIMEOUT_SECONDS)
        try:
            conn.request(method, path, body=body, headers=fwd_headers)
            return conn, conn.getresponse()
        except Exception:
            conn.close()
            raise

    def _write_plain_response(self, response: http.client.HTTPResponse) -> None:
        self.wfile.write(f"HTTP/1.1 {response.status} {response.reason}\r\n".encode())
        excluded = _HOP_HEADERS | {
            h.strip().lower() for h in (response.getheader("Connection") or "").split(",")
        }
        for k, v in response.getheaders():
            if k.lower() not in excluded and not (
                response.chunked and k.lower() == "content-length"
            ):
                self.wfile.write(f"{k}: {v}\r\n".encode())
        self.wfile.write(b"Connection: close\r\n\r\n")
        self.wfile.flush()
        while chunk := response.read1(_TUNNEL_BUFFER_BYTES):
            self.wfile.write(chunk)
            self.wfile.flush()


class ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        policy: EgressPolicy,
        *,
        resolve: Resolver = _system_resolve,
        upstream: UpstreamPolicy | None = None,
    ):
        self.policy = policy
        self.resolve = resolve
        self.upstream = upstream  # None => every allowed request dials the origin directly
        super().__init__(address, _Handler)
