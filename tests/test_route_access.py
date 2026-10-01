"""Route access matrix for the gateway.

Every route class the gateway serves is pinned in EXPECTED with its access
level and methods. The test drives each class with every HTTP method as an
anonymous caller, with a wrong token and with the configured token, so a new
route class, a widened method set or a public state-changing route fails CI.
"""

from __future__ import annotations

import sys
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from book_api_gateway.config import GatewayConfig  # noqa: E402
from book_api_gateway.server import PUBLIC, ROUTE_ACCESS, SERVICE, create_server  # noqa: E402
from test_gateway import RecordingUpstreamHandler, start_server  # noqa: E402

EXPECTED = {
    "/healthz": (PUBLIC, {"GET"}),
    "/readyz": (PUBLIC, {"GET"}),
    "/api/*": (SERVICE, {"GET", "POST"}),
}
SAMPLE_PATHS = {"/healthz": "/healthz", "/readyz": "/readyz", "/api/*": "/api/demo/items"}
SAFE_METHODS = {"GET", "HEAD"}
ALL_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
TOKEN = "matrix-gateway-token"


class RouteAccessTableTests(unittest.TestCase):
    def test_table_is_pinned(self) -> None:
        actual = {route: (level, set(methods)) for route, (level, methods) in ROUTE_ACCESS.items()}
        self.assertEqual(actual, EXPECTED)

    def test_public_routes_are_read_only(self) -> None:
        for route, (level, methods) in ROUTE_ACCESS.items():
            if level == PUBLIC:
                self.assertTrue(set(methods) <= SAFE_METHODS, f"{route} is public but allows {sorted(methods)}")


class RouteAccessMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        RecordingUpstreamHandler.records = []
        RecordingUpstreamHandler.mode = "ok"
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), RecordingUpstreamHandler)
        start_server(self.upstream)
        config = GatewayConfig(
            api_token=TOKEN,
            upstream_base_url=f"http://127.0.0.1:{self.upstream.server_port}",
            rate_limit_per_minute=10_000,
        )
        self.gateway = create_server(config, telemetry_sink=lambda _event: None)
        start_server(self.gateway)

    def tearDown(self) -> None:
        self.gateway.shutdown()
        self.gateway.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()

    def _status(self, method: str, path: str, authorization: str | None) -> int:
        connection = HTTPConnection("127.0.0.1", self.gateway.server_port, timeout=2.0)
        headers = {"Content-Length": "0"}
        if authorization is not None:
            headers["Authorization"] = authorization
        try:
            connection.request(method, path, headers=headers)
            response = connection.getresponse()
            response.read()
            return response.status
        finally:
            connection.close()

    def test_every_route_method_and_caller(self) -> None:
        callers = {"anonymous": None, "wrong-token": "Bearer wrong", "service": f"Bearer {TOKEN}"}
        for route, (level, methods) in ROUTE_ACCESS.items():
            path = SAMPLE_PATHS[route]
            for method in ALL_METHODS:
                for caller, authorization in callers.items():
                    with self.subTest(route=route, method=method, caller=caller):
                        status = self._status(method, path, authorization)
                        if method not in methods:
                            self.assertEqual(status, 405)
                        elif level == PUBLIC:
                            self.assertIn(status, {200, 503})
                        elif caller == "service":
                            self.assertEqual(status, 200)
                        else:
                            self.assertEqual(status, 401)
        forwarded = {(record["method"], record["path"]) for record in RecordingUpstreamHandler.records}
        self.assertEqual(forwarded, {("GET", "/api/demo/items"), ("POST", "/api/demo/items")})

    def test_unclassified_paths_are_not_served(self) -> None:
        for path in ("/", "/api", "/api/", "/admin", "/healthz/extra", "/metrics"):
            with self.subTest(path=path):
                self.assertEqual(self._status("GET", path, f"Bearer {TOKEN}"), 404)


if __name__ == "__main__":
    unittest.main()
