from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from book_api_gateway.config import ConfigError, GatewayConfig  # noqa: E402
from book_api_gateway.server import create_server  # noqa: E402


class RecordingUpstreamHandler(BaseHTTPRequestHandler):
    records: list[dict] = []
    mode = "ok"

    def _respond(self) -> None:
        if self.mode == "sleep":
            time.sleep(0.2)
        if self.mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "http://example.com/unsafe")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.mode == "error":
            encoded = b'{"upstream_secret":"should-not-cross-boundary"}'
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        record = {
            "method": self.command,
            "path": self.path,
            "body": body,
            "headers": {key.lower(): value for key, value in self.headers.items()},
        }
        self.records.append(record)
        payload = {
            "ok": True,
            "path": self.path,
            "request_id": self.headers.get("X-Request-ID"),
            "subject": self.headers.get("X-Authenticated-Subject"),
            "has_authorization": "authorization" in record["headers"],
        }
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except BrokenPipeError:
            pass

    def do_GET(self) -> None:  # noqa: N802
        self._respond()

    def do_POST(self) -> None:  # noqa: N802
        self._respond()

    def log_message(self, *_args: object) -> None:
        return


def start_server(server: ThreadingHTTPServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def call(url: str, *, method: str = "GET", headers: dict[str, str] | None = None, body: bytes | None = None):
    request = Request(url, method=method, headers=headers or {}, data=body)
    try:
        with urlopen(request, timeout=1.0) as response:
            return response.status, dict(response.headers), json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        try:
            return error.code, dict(error.headers), json.loads(error.read().decode("utf-8"))
        finally:
            error.close()


class GatewayTests(unittest.TestCase):
    token = "super-secret-token"

    def setUp(self) -> None:
        RecordingUpstreamHandler.records = []
        RecordingUpstreamHandler.mode = "ok"
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), RecordingUpstreamHandler)
        start_server(self.upstream)
        self.telemetry: list[dict] = []
        self.gateway: ThreadingHTTPServer | None = None

    def tearDown(self) -> None:
        if self.gateway is not None:
            self.gateway.shutdown()
            self.gateway.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()

    def start_gateway(self, *, rate_limit: int = 60, timeout: float = 0.2) -> str:
        config = GatewayConfig(
            api_token=self.token,
            upstream_base_url=f"http://127.0.0.1:{self.upstream.server_port}",
            upstream_timeout_seconds=timeout,
            rate_limit_per_minute=rate_limit,
        )
        self.gateway = create_server(config, telemetry_sink=self.telemetry.append)
        start_server(self.gateway)
        return f"http://127.0.0.1:{self.gateway.server_port}"

    def test_health_is_public_and_sets_request_id(self) -> None:
        base_url = self.start_gateway()
        status, headers, payload = call(f"{base_url}/healthz")

        self.assertEqual(status, 200)
        self.assertEqual(payload["contract"], "book-api-gateway.v1")
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(headers["X-Request-ID"])

    def test_api_requires_bearer_token_without_echoing_secret(self) -> None:
        base_url = self.start_gateway()

        status, _headers, payload = call(f"{base_url}/api/demo")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"], "authentication required")
        self.assertNotIn(self.token, json.dumps(payload))

        status, _headers, payload = call(
            f"{base_url}/api/demo", headers={"Authorization": "Bearer wrong-token"}
        )
        self.assertEqual(status, 401)
        self.assertNotIn(self.token, json.dumps(payload))

    def test_valid_request_routes_and_does_not_forward_authorization(self) -> None:
        base_url = self.start_gateway()
        status, _headers, payload = call(
            f"{base_url}/api/demo/hello",
            headers={"Authorization": f"Bearer {self.token}", "X-Request-ID": "req-123"},
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["path"], "/api/demo/hello")
        self.assertEqual(payload["request_id"], "req-123")
        self.assertFalse(payload["has_authorization"])
        self.assertTrue(payload["subject"].startswith("token:"))
        self.assertEqual(len(RecordingUpstreamHandler.records), 1)

    def test_rate_limit_is_deterministic_per_subject(self) -> None:
        base_url = self.start_gateway(rate_limit=1)
        headers = {"Authorization": f"Bearer {self.token}"}

        self.assertEqual(call(f"{base_url}/api/demo/one", headers=headers)[0], 200)
        status, response_headers, payload = call(f"{base_url}/api/demo/two", headers=headers)

        self.assertEqual(status, 429)
        self.assertGreaterEqual(int(response_headers["Retry-After"]), 1)
        self.assertEqual(payload["error"], "rate limit exceeded")

    def test_upstream_timeout_returns_sanitized_504(self) -> None:
        RecordingUpstreamHandler.mode = "sleep"
        base_url = self.start_gateway(timeout=0.05)
        status, _headers, payload = call(
            f"{base_url}/api/demo/slow", headers={"Authorization": f"Bearer {self.token}"}
        )

        self.assertEqual(status, 504)
        self.assertEqual(payload["error"], "upstream timeout")
        self.assertNotIn("Traceback", json.dumps(payload))

    def test_upstream_failure_and_redirect_are_sanitized(self) -> None:
        RecordingUpstreamHandler.mode = "error"
        base_url = self.start_gateway()
        headers = {"Authorization": f"Bearer {self.token}"}
        status, _headers, payload = call(f"{base_url}/api/demo/error", headers=headers)
        self.assertEqual(status, 502)
        self.assertEqual(payload["error"], "upstream unavailable")
        self.assertNotIn("should-not-cross-boundary", json.dumps(payload))

        RecordingUpstreamHandler.mode = "redirect"
        status, _headers, payload = call(f"{base_url}/api/demo/redirect", headers=headers)
        self.assertEqual(status, 502)
        self.assertEqual(payload["error"], "upstream unavailable")

    def test_query_parameters_are_rejected_before_proxying(self) -> None:
        base_url = self.start_gateway()
        status, _headers, payload = call(
            f"{base_url}/api/demo?private=secret-value",
            headers={"Authorization": f"Bearer {self.token}"},
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "query parameters are not supported by the pilot")
        self.assertEqual(RecordingUpstreamHandler.records, [])

    def test_missing_gateway_token_fails_closed(self) -> None:
        config = GatewayConfig(
            api_token="",
            upstream_base_url=f"http://127.0.0.1:{self.upstream.server_port}",
        )
        self.gateway = create_server(config, telemetry_sink=self.telemetry.append)
        start_server(self.gateway)
        base_url = f"http://127.0.0.1:{self.gateway.server_port}"
        status, _headers, payload = call(f"{base_url}/api/demo", headers={})

        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], "gateway authentication unavailable")

    def test_telemetry_is_redacted(self) -> None:
        base_url = self.start_gateway()
        body = b"private-body"
        status, _headers, _payload = call(
            f"{base_url}/api/demo/private",
            method="POST",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "text/plain"},
            body=body,
        )

        self.assertEqual(status, 200)
        self.assertTrue(self.telemetry)
        event = self.telemetry[-1]
        self.assertEqual(event["event"], "request.completed")
        self.assertEqual(event["status"], 200)
        self.assertTrue(event["request_id"])
        self.assertTrue(event["subject_hash"].startswith("token:"))
        rendered = json.dumps(self.telemetry)
        self.assertNotIn(self.token, rendered)
        self.assertNotIn(body.decode(), rendered)

    def test_upstream_base_url_rejects_credentials_and_non_allowlisted_hosts(self) -> None:
        with self.assertRaises(ConfigError):
            GatewayConfig(api_token=self.token, upstream_base_url="http://user:pass@127.0.0.1:9000")
        with self.assertRaises(ConfigError):
            GatewayConfig(api_token=self.token, upstream_base_url="http://example.com:9000")


if __name__ == "__main__":
    unittest.main()
