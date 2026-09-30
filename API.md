# API Surface

Contract: `book-api-gateway.v1` (REST over HTTP, JSON errors). The
machine-readable interface list is `interfaces` in [`contract.json`](contract.json);
`scripts/check.py` fails when an interface listed there is missing from this page.

| ID | Method | Path | Authentication | Downstream behavior |
|---|---|---|---|---|
| `api-gateway.http.healthz` | `GET` | `/healthz` | Public | No upstream call; reports process health |
| `api-gateway.http.readyz` | `GET` | `/readyz` | Public | No upstream call; reports pilot readiness |
| `api-gateway.http.proxy` | `GET`, `POST` | `/api/*` | `Authorization: Bearer <configured token>` | Proxies to the configured, allow-listed upstream |

The health payload is `{"contract": "book-api-gateway.v1", "status": "ok",
"auth_configured": <bool>}`. Other methods on the health routes return 405.

The gateway does not own domain objects or make object-level authorization
decisions. The downstream platform remains responsible for tenant, role and
resource authorization. The pilot token is a local compatibility credential;
the identity platform is the dependency for a production subject assertion
(`api-gateway.consumes.identity`, planned).

## Request handling

Every `/api/*` request receives a validated `X-Request-ID`, is rate-limited per
authenticated subject, and forwards only `Accept`, `Content-Type`, conditional
request headers, `X-Request-ID` and a hashed subject marker
(`X-Authenticated-Subject`) to the upstream (`api-gateway.http.upstream`). The
original `Authorization` header is never forwarded. Query parameters are
rejected by the pilot, request and response bodies are capped at 1 MiB,
upstream redirects are disabled, and upstream calls time out after the
configured interval.

Ingress hardening:

- Paths with `.` or `..` segments (raw or percent-encoded) or backslashes are
  rejected with 400 so an upstream cannot normalise them outside `/api/`.
- `Transfer-Encoding` request bodies are rejected with 411; `Content-Length`
  must be a plain decimal, and conflicting duplicate values are rejected with
  400.
- When a response is sent before a declared request body was read (for example
  401, 413 or 429), the gateway replies with `Connection: close` so the unread
  bytes are never parsed as another request.
- The `Server` header is `book-api-gateway` and does not disclose the runtime
  version.

| Status | `error` | Cause |
|---|---|---|
| 400 | `invalid request path`, `invalid content length`, `incomplete request body`, `query parameters are not supported by the pilot` | Rejected request shape |
| 401 | `authentication required` | Missing or wrong bearer token (`WWW-Authenticate: Bearer`) |
| 404 | `route not found` | Outside `/healthz`, `/readyz`, `/api/*` |
| 405 | `method not allowed` | Method outside the route's `Allow` list |
| 411 | `chunked request bodies are not supported` | `Transfer-Encoding` present |
| 413 | `request body too large` | Body above `MAX_BODY_BYTES` |
| 429 | `rate limit exceeded` | Per-subject fixed window exhausted (`Retry-After`) |
| 502 | `upstream unavailable`, `upstream response too large` | Upstream error, redirect or oversize body; upstream bodies are not relayed |
| 503 | `gateway authentication unavailable` | No gateway token configured (fails closed) |
| 504 | `upstream timeout` | Upstream exceeded `UPSTREAM_TIMEOUT_SECONDS` |

Every error body is `{"error": "<message>", "request_id": "<id>"}`.

## Telemetry

`api-gateway.event.request-completed` is a redacted completion event containing
`event: "request.completed"`, method, route class (`healthz`, `readyz`, `api`,
`other`), request ID, status, latency and (for authenticated calls) a short
subject hash. It never contains authorization headers, request bodies, query
values or upstream response bodies.
