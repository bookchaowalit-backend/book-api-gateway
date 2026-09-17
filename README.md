# book-api-gateway

`API Gateway` platform boundary for the Book Platform portfolio.

## Scope

This repository owns the `api-gateway` capability: routing, request identity, rate limiting, ingress policy.
The current source implementation is recorded as `infra/api/server.ts` in the
parent registry. This checkout contains a dependency-free local HTTP pilot; it
does not contain production provider integration, a database writer, or a
customer payload.

## Boundary

- Owner: `bookchaowalit-backend`
- Repository: `book-api-gateway`
- Target remote: `https://github.com/bookchaowalit-backend/book-api-gateway.git` (not created by the bootstrap)
- Contract: `book-platform.contract.v1`
- Status: `pilot` (local only)
- Data owner: the platform boundary identified in `contract.json`

The platform communicates through versioned API or event contracts. Consumers
must not import another platform's database, migration, or private runtime
module. `solo-empire` remains the control plane and compatibility adapter until
parity and rollback evidence permit a cutover.

## Local verification

Run from this repository:

```bash
bash scripts/check.sh
```

The check validates repository shape, contract metadata, routing/authentication
behavior, rate limiting, upstream timeout handling, and telemetry redaction.
It uses only loopback fixtures and does not claim deployment, provider
connectivity, data migration, or production readiness.

The pilot exposes `GET /healthz` and authenticated `GET`/`POST /api/*` routes.
Set `BOOK_API_GATEWAY_TOKEN`, `UPSTREAM_BASE_URL`, and (when the upstream is
not loopback) `UPSTREAM_ALLOWED_HOSTS` before starting
`PYTHONPATH=src python3 -m book_api_gateway.server`.
The complete endpoint and security inventory is in [`API.md`](API.md).

## Migration gate

Before activating a remote or changing a consumer, add sanitized fixtures for
success, duplicate delivery, timeout and provider failure; prove tenant/privacy
isolation; compare the old and new contract; and rehearse rollback on a
disposable state store. Record the evidence in the parent platform registry.
