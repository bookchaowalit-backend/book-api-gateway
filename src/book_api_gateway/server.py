"""Dependency-free HTTP gateway used for the local migration pilot."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import socket
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import GatewayConfig
from .limiter import FixedWindowRateLimiter


REQUEST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
CONTENT_LENGTH_RE = re.compile(r"[0-9]{1,19}")
FORWARD_HEADERS = ("Accept", "Content-Type", "If-Match", "If-None-Match")
ALLOWED_METHODS = frozenset({"GET", "POST"})

# Route access matrix. "public" needs no credentials and may only serve safe
# methods; "service" needs `Authorization: Bearer $GATEWAY_API_TOKEN`.
# `_dispatch` reads the method sets from here and
# tests/test_route_access.py pins every entry, so a new route class or
# method has to be classified on purpose.
PUBLIC = "public"
SERVICE = "service"
ROUTE_ACCESS: dict[str, tuple[str, frozenset[str]]] = {
    "/healthz": (PUBLIC, frozenset({"GET"})),
    "/readyz": (PUBLIC, frozenset({"GET"})),
    "/api/*": (SERVICE, ALLOWED_METHODS),
}
TelemetrySink = Callable[[dict[str, Any]], None]


class _NoRedirectHandler(HTTPRedirectHandler):
    """Keep an upstream from redirecting the gateway to an untrusted host."""

    def redirect_request(self, *_args: Any, **_kwargs: Any):
        return None


def _unsafe_api_path(path: str) -> bool:
    """Return True when a path could escape the ``/api/`` prefix upstream.

    Upstream servers and intermediaries commonly normalise dot segments and
    backslashes, so ``/api/../admin`` or ``/api/%2e%2e/admin`` would otherwise
    reach a route outside the gateway's allow-listed prefix.
    """

    if "\\" in path or "%5c" in path.casefold():
        return True
    decoded = unquote(path)
    if "\\" in decoded or any(ord(char) < 0x20 or ord(char) == 0x7F for char in decoded):
        return True
    segments = decoded.split("/")[1:]
    return any(segment in {".", ".."} for segment in segments)


def _default_telemetry(event: dict[str, Any]) -> None:
    """Emit only already-redacted event fields to stderr."""

    print(json.dumps(event, separators=(",", ":"), sort_keys=True), file=sys.stderr, flush=True)


def upstream_tcp_probe(base_url: str, timeout_seconds: float) -> bool:
    """Return True when a TCP connection to the upstream host:port succeeds.

    Only reachability is checked: no HTTP request is sent, so readiness probes
    never create upstream traffic, credentials or telemetry on the upstream.
    """

    parsed = urlsplit(base_url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((parsed.hostname or "", port), timeout=timeout_seconds):
            return True
    except (OSError, ValueError):
        return False


@dataclass
class GatewayState:
    config: GatewayConfig
    limiter: FixedWindowRateLimiter
    telemetry_sink: TelemetrySink = _default_telemetry
    opener: Any = None
    upstream_probe: Callable[[], bool] | None = None

    def __post_init__(self) -> None:
        if self.opener is None:
            self.opener = build_opener(_NoRedirectHandler())
        if self.upstream_probe is None:
            base_url = self.config.upstream_base_url
            timeout = min(self.config.upstream_timeout_seconds, 1.0)
            self.upstream_probe = lambda: upstream_tcp_probe(base_url, timeout)


class GatewayHTTPServer(ThreadingHTTPServer):
    """HTTP server carrying immutable gateway configuration and state."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], state: GatewayState):
        super().__init__(server_address, GatewayRequestHandler)
        self.gateway_state = state


class GatewayRequestHandler(BaseHTTPRequestHandler):
    """Authenticate, limit, and proxy the small versioned API surface."""

    protocol_version = "HTTP/1.1"
    server_version = "book-api-gateway"

    def version_string(self) -> str:
        # Do not advertise the Python runtime version to clients.
        return self.server_version

    @property
    def state(self) -> GatewayState:
        return self.server.gateway_state  # type: ignore[attr-defined]

    def _request_id(self) -> str:
        incoming = self.headers.get("X-Request-ID", "").strip()
        if REQUEST_ID_RE.fullmatch(incoming):
            return incoming
        return uuid.uuid4().hex

    def _send_bytes(
        self,
        status: int,
        payload: bytes,
        request_id: str,
        *,
        content_type: str = "application/json",
        extra_headers: dict[str, str] | None = None,
    ) -> int:
        if not 100 <= status <= 599:
            status = 502
            payload = json.dumps(
                {"error": "invalid upstream response", "request_id": request_id},
                separators=(",", ":"),
            ).encode("utf-8")
            content_type = "application/json"
        self._last_status = status
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Request-ID", request_id)
        if self._body_pending():
            # The declared request body was not read, so the remaining bytes on
            # this connection cannot be trusted as the next request.
            self.send_header("Connection", "close")
        for name, value in (extra_headers or {}).items():
            if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
                continue
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(payload)
            except BrokenPipeError:
                pass
        return status

    def _body_pending(self) -> bool:
        if getattr(self, "_body_consumed", False):
            return False
        if self.headers.get("Transfer-Encoding") is not None:
            return True
        return self.headers.get("Content-Length", "0").strip() not in {"", "0"}

    def _send_json(
        self,
        status: int,
        payload: dict[str, Any],
        request_id: str,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> int:
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return self._send_bytes(
            status,
            encoded,
            request_id,
            content_type="application/json; charset=utf-8",
            extra_headers=extra_headers,
        )

    def _error(
        self,
        status: int,
        error: str,
        request_id: str,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> int:
        return self._send_json(status, {"error": error, "request_id": request_id}, request_id, extra_headers=extra_headers)

    def _authenticate(self, request_id: str) -> str | None:
        token = self.state.config.api_token
        if not token:
            self._error(503, "gateway authentication unavailable", request_id)
            return None
        authorization = self.headers.get("Authorization", "")
        scheme, separator, credentials = authorization.partition(" ")
        if (
            not separator
            or scheme.casefold() != "bearer"
            or not credentials
            or credentials != credentials.strip()
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in credentials)
            or not hmac.compare_digest(credentials, token)
        ):
            self._error(401, "authentication required", request_id, extra_headers={"WWW-Authenticate": "Bearer"})
            return None
        return "token:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]

    def _dispatch(self, request_id: str) -> int:
        try:
            parsed = urlsplit(self.path)
        except ValueError:
            return self._error(400, "invalid request target", request_id)
        path = parsed.path or "/"
        if parsed.query:
            return self._error(400, "query parameters are not supported by the pilot", request_id)

        if path in {"/healthz", "/readyz"}:
            methods = ROUTE_ACCESS[path][1]
            if self.command not in methods:
                return self._error(
                    405, "method not allowed", request_id, extra_headers={"Allow": ", ".join(sorted(methods))}
                )
            auth_configured = bool(self.state.config.api_token)
            if path == "/healthz":
                # Liveness: the process answers; no dependency checks.
                payload = {
                    "contract": "book-api-gateway.v1",
                    "status": "ok",
                    "auth_configured": auth_configured,
                }
                return self._send_json(200, payload, request_id)
            # Readiness: only route traffic here when /api/* can succeed.
            upstream_reachable = self.state.upstream_probe()
            ready = auth_configured and upstream_reachable
            payload = {
                "contract": "book-api-gateway.v1",
                "status": "ok" if ready else "not_ready",
                "auth_configured": auth_configured,
                "upstream_reachable": upstream_reachable,
            }
            return self._send_json(200 if ready else 503, payload, request_id)

        if not path.startswith("/api/") or path == "/api/":
            return self._error(404, "route not found", request_id)
        if _unsafe_api_path(path):
            return self._error(400, "invalid request path", request_id)
        api_methods = ROUTE_ACCESS["/api/*"][1]
        if self.command not in api_methods:
            return self._error(
                405, "method not allowed", request_id, extra_headers={"Allow": ", ".join(sorted(api_methods))}
            )

        subject_hash = self._authenticate(request_id)
        if subject_hash is None:
            return self._last_status
        self._subject_hash = subject_hash
        allowed, retry_after = self.state.limiter.allow(subject_hash)
        if not allowed:
            return self._error(
                429,
                "rate limit exceeded",
                request_id,
                extra_headers={"Retry-After": str(retry_after)},
            )

        if self.headers.get("Transfer-Encoding") is not None:
            return self._error(411, "chunked request bodies are not supported", request_id)
        raw_lengths = {value.strip() for value in self.headers.get_all("Content-Length", [])}
        if len(raw_lengths) > 1:
            return self._error(400, "invalid content length", request_id)
        raw_length = raw_lengths.pop() if raw_lengths else "0"
        if not CONTENT_LENGTH_RE.fullmatch(raw_length):
            return self._error(400, "invalid content length", request_id)
        length = int(raw_length)
        if length > self.state.config.max_body_bytes:
            return self._error(413, "request body too large", request_id)
        body = self.rfile.read(length) if length else b""
        self._body_consumed = True
        if len(body) != length:
            return self._error(400, "incomplete request body", request_id)
        return self._proxy(path, body, request_id, subject_hash)

    def _proxy(self, path: str, body: bytes, request_id: str, subject_hash: str) -> int:
        upstream_url = f"{self.state.config.upstream_base_url}{path}"
        headers: dict[str, str] = {}
        for name in FORWARD_HEADERS:
            value = self.headers.get(name)
            if value is not None:
                headers[name] = value
        headers["X-Request-ID"] = request_id
        headers["X-Authenticated-Subject"] = subject_hash
        request = Request(
            upstream_url,
            data=body if body else None,
            headers=headers,
            method=self.command,
        )
        try:
            with self.state.opener.open(request, timeout=self.state.config.upstream_timeout_seconds) as response:
                payload = response.read(self.state.config.max_body_bytes + 1)
                if len(payload) > self.state.config.max_body_bytes:
                    return self._error(502, "upstream response too large", request_id)
                status = int(response.getcode())
                content_type = response.headers.get("Content-Type", "application/octet-stream")
                if len(content_type) > 200 or any(
                    ord(char) < 0x20 or ord(char) == 0x7F for char in content_type
                ):
                    content_type = "application/octet-stream"
                return self._send_bytes(
                    status,
                    payload,
                    request_id,
                    content_type=content_type,
                )
        except (socket.timeout, TimeoutError):
            return self._error(504, "upstream timeout", request_id)
        except HTTPError as exc:
            exc.close()
            return self._error(502, "upstream unavailable", request_id)
        except URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                return self._error(504, "upstream timeout", request_id)
            return self._error(502, "upstream unavailable", request_id)
        except (OSError, ValueError):
            return self._error(502, "upstream unavailable", request_id)

    def _handle_request(self) -> None:
        started = time.monotonic()
        request_id = self._request_id()
        self._last_status = 500
        self._subject_hash: str | None = None
        self._body_consumed = False
        try:
            self._dispatch(request_id)
        except Exception:
            # Keep unexpected implementation/configuration details out of the
            # response and out of telemetry. The request remains traceable by ID.
            self._error(500, "gateway error", request_id)
        finally:
            event: dict[str, Any] = {
                "event": "request.completed",
                "method": self.command,
                "route": self._route_class(),
                "request_id": request_id,
                "status": self._last_status,
                "latency_ms": round((time.monotonic() - started) * 1000, 2),
            }
            if self._subject_hash is not None:
                event["subject_hash"] = self._subject_hash
            try:
                self.state.telemetry_sink(event)
            except Exception:
                pass

    def _route_class(self) -> str:
        try:
            path = urlsplit(self.path).path
        except ValueError:
            return "invalid"
        if path == "/healthz":
            return "healthz"
        if path == "/readyz":
            return "readyz"
        if path.startswith("/api/"):
            return "api"
        return "other"

    def do_GET(self) -> None:  # noqa: N802
        self._handle_request()

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle_request()

    def do_POST(self) -> None:  # noqa: N802
        self._handle_request()

    def do_PUT(self) -> None:  # noqa: N802
        self._handle_request()

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle_request()

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle_request()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._handle_request()

    def log_message(self, *_args: Any) -> None:
        # Default HTTP server logs include the request target, which can carry
        # user identifiers or query values. Structured telemetry is redacted.
        return


def create_server(
    config: GatewayConfig,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    telemetry_sink: TelemetrySink = _default_telemetry,
) -> GatewayHTTPServer:
    """Create a gateway server without starting its serving thread."""

    state = GatewayState(
        config=config,
        limiter=FixedWindowRateLimiter(config.rate_limit_per_minute),
        telemetry_sink=telemetry_sink,
    )
    return GatewayHTTPServer((host, port), state)


def main() -> int:
    """Run the gateway as a local process using environment configuration."""

    from .config import ConfigError

    try:
        config = GatewayConfig.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    host = os.getenv("GATEWAY_HOST", "127.0.0.1")
    try:
        port = int(os.getenv("GATEWAY_PORT", "8080"))
    except ValueError:
        print("configuration error: GATEWAY_PORT must be an integer", file=sys.stderr)
        return 2
    server = create_server(config, host=host, port=port)
    print(f"book-api-gateway listening on {host}:{server.server_port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
