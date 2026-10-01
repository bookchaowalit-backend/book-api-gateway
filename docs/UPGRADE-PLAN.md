# Upgrade plan: book-api-gateway

## Current state

Score: 6.5/10 (was 6/10 after pass 1, 5/10 originally). Readiness now
reflects upstream reachability and limiter memory is bounded. A dependency-free local pilot with a schema-validated
contract, documented surface, ingress hardening and CI. It still uses a single
local pilot token, has one upstream, and has no shadow-parity evidence against
`solo-empire/infra/api/server.ts`.

## Backlog

### P0

- Replace the pilot token with the identity platform's subject assertion
  (`api-gateway.consumes.identity`) once `book-identity-platform` defines it;
  keep the fail-closed 503 when identity is unavailable.
- Record shadow-parity fixtures: replay sanitized `solo-empire` API requests
  through the gateway and compare status classes, headers and latency.

### P1

- Route table (prefix -> allow-listed upstream) instead of one
  `UPSTREAM_BASE_URL`, with a versioned route contract (registry gate).
- The remaining ~4 s of the suite is `raw_exchange` sleeping for socket
  reads; read until the response is complete instead of fixed waits.
- Replace the vendored schema and its pin with a reusable workflow or tagged
  package from `bookchaowalit-backend-core` once one exists (the pin check
  already fails on drift).

### P2

- Rollback-route drill documentation.
- Cache the `/readyz` probe result for a second or two so an aggressive
  orchestrator cannot turn readiness checks into upstream connection load.

## Done in this pass (pass 2)
- `UPSTREAM_TIMEOUT_SECONDS=nan`/`inf` is rejected at startup. `float()`
  accepts them and NaN passes `<= 0`, so the gateway started and then
  failed every proxied request inside `socket.settimeout`
  (`test_upstream_timeout_must_be_finite`).

- `/readyz` is a real readiness check: 503 `not_ready` unless a token is
  configured and a TCP connect to the upstream succeeds (no HTTP request, no
  upstream address in the payload). `/healthz` stays liveness-only.
- Rate limiter memory is bounded (`max_keys`, default 10,000): expired windows
  are dropped first, then the oldest; eviction can only reset a count, never
  block a request.
- Test suite 16 s -> ~4.5 s (servers polled every 20 ms instead of 0.5 s);
  8 new tests (readiness, probe, limiter bounds). Offline only.
- P0 items (identity subject assertion, shadow-parity fixtures) remain blocked
  on `book-identity-platform` and sanitized `solo-empire` captures.
- Schema pin: `schema/book-platform.contract.v1.schema.json.sha256` pins the
  canonical digest; `scripts/check_schema_pin.py` fails on drift (and, with
  `--canonical`, compares with a local backend-core checkout). It runs in
  `scripts/check.sh` and as its own CI step.
- `scripts/check_registry_alignment.py --solo-empire PATH` compares the
  contract with `repository-catalog/registries/platforms.yaml` (local,
  read-only; not in CI). Interface sources that exist in this repository are
  skipped instead of reported as missing solo-empire paths.
- `tests/test_drift_checks.py` covers both checks offline. Verified against
  `solo-empire` `5b43c85` (no drift, no warnings) and
  `bookchaowalit-backend-core/scripts/check_platform_sync.py --require-pin`.
- Route access matrix: `ROUTE_ACCESS` in `server.py` (`/healthz`,
  `/readyz` public GET; `/api/*` service token, GET/POST) is what
  `_dispatch` reads for method checks. `tests/test_route_access.py` pins it,
  forbids public non-safe methods, and drives every route x 7 methods x
  (anonymous, wrong token, token): 405 / 401 / 200 as classified, only GET
  and POST reach the upstream, unclassified paths 404. Network exposure:
  the server already binds `127.0.0.1` by default (`GATEWAY_HOST`); no
  compose or Dockerfile in this repository.

## Done in pass 1


- Fixed: `/api/../x`, percent-encoded dot segments and backslashes reached the
  upstream and could escape the `/api/` prefix; now rejected with 400.
- Fixed: a response sent before reading a declared body (401/413/429) left the
  body on the keep-alive connection, where it was parsed as a second request;
  now answered with `Connection: close`.
- Fixed: `Transfer-Encoding: chunked` bodies were silently dropped and the chunk
  bytes desynchronised the connection; now 411. Conflicting or non-decimal
  `Content-Length` values are rejected.
- The `Server` header no longer discloses the Python version.
- Fixed a race in `test_telemetry_is_redacted` (telemetry is emitted after the
  response flush).
- Contract: registry-aligned `source_paths`, `migration_gates`, `interfaces`;
  JSON schema validation in `scripts/check.py`; `tests/test_contract_check.py`;
  `.github/workflows/check.yml`; `API.md` error table.
