# API Surface

Contract: `book-api-gateway.v1` (REST over HTTP, JSON errors).

| Method | Path | Authentication | Downstream behavior |
|---|---|---|---|
| `GET` | `/healthz` | Public | No upstream call; reports process health |
| `GET` | `/readyz` | Public | No upstream call; reports pilot readiness |
| `GET`, `POST` | `/api/*` | `Authorization: Bearer <configured token>` | Proxies to the configured, allow-listed upstream |

The gateway does not own domain objects or make object-level authorization
decisions. The downstream platform remains responsible for tenant, role and
resource authorization. The pilot token is a local compatibility credential;
the identity platform is the dependency for a production subject assertion.

Every `/api/*` request receives a validated `X-Request-ID`, is rate-limited per
authenticated subject, and forwards only `Accept`, `Content-Type`, conditional
request headers, `X-Request-ID` and a hashed subject marker. The original
`Authorization` header is never forwarded. Query parameters are rejected by
the pilot, request and response bodies are capped at 1 MiB, upstream redirects
are disabled, and upstream calls time out after the configured interval.

Telemetry is a redacted completion event containing method, route class,
request ID, status, latency and (for authenticated calls) a short subject hash.
It never contains authorization headers, request bodies, query values or
upstream response bodies.
