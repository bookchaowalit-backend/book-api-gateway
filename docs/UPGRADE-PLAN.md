# Upgrade plan: book-api-gateway

## Current state

Score: 6/10 (was 5/10). A dependency-free local pilot with a schema-validated
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
- Bound the rate limiter's per-subject map (evict expired windows) before more
  than one subject exists.
- Speed up the HTTP tests (server shutdown dominates the ~14 s suite).
- Keep `schema/book-platform.contract.v1.schema.json` identical to
  `bookchaowalit-backend-core/contracts/`.

### P2

- Rollback-route drill documentation and a health check that verifies upstream
  reachability for `/readyz`.

## Done in this pass

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
