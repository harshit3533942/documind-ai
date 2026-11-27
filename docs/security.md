# Security

## Controls

| Area | Control | Where |
|---|---|---|
| **Passwords** | Argon2id (memory-hard, salted); outdated hashes upgraded on login; 8–128 chars with letter + digit | `core/security.py`, `schemas/user.py` |
| **Login** | Identical error + timing for unknown email vs wrong password (no account enumeration via login); rate limit per IP *and* per account | `services/auth_service.py`, `core/rate_limit.py` |
| **JWT** | HS256 with pinned algorithm (no `alg: none`), issuer, expiry (60 min), required claims; role re-read from DB on every request; secret ≥ 32 chars, required at startup, never in `repr` | `core/security.py`, `core/dependencies.py`, `core/config.py` |
| **Authorization** | Admin routes protected at router level; public sign-up can never create admins; admins see metadata only, never document content | `routers/admin.py`, `scripts/create_admin.py` |
| **Data isolation** | Every document/chunk/conversation query takes `user_id`; vector search filters `document_chunks.user_id`; another user's id → **404** (existence not revealed) | repositories, `services/retrieval_service.py` |
| **Uploads** | Extension + content type + `%PDF-` magic bytes + PyMuPDF parse; size limit enforced by `Content-Length` *and* while streaming; page limit; corrupt/encrypted rejected | `services/document_service.py`, `services/pdf_service.py` |
| **Path traversal** | User filenames never touch the disk; files stored as `<user uuid>/<random hex>.pdf`; key regex + resolved-path containment check | `services/storage_service.py` |
| **SQL injection** | ORM with bound parameters everywhere; the only f-string SQL (`SET LOCAL hnsw.ef_search`) uses a validated integer setting, never user input | `services/retrieval_service.py` |
| **Prompt injection** | Document text wrapped in escaped `<source>` delimiters; system prompt forbids following instructions inside sources; isolation is enforced in SQL, not by the LLM | `services/rag_service.py` |
| **Rate limiting** | Redis fixed window: login, register, chat, upload → 429 + `Retry-After`; identities hashed in keys | `core/rate_limit.py` |
| **Error responses** | Unhandled errors → generic `500 internal_error` (details only in server logs); 422 bodies **do not echo submitted values** (FastAPI's default would echo passwords) | `core/exceptions.py` |
| **HTTP headers** | `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Permissions-Policy`, strict CSP on API responses, `Cache-Control: no-store` on `/api/*`, HSTS when `HSTS_ENABLED=true` | `core/middleware.py` |
| **Logging** | IDs only — never emails, passwords, tokens, questions or document text; access log without query strings; request IDs validated before being echoed (no header injection) | all services, `core/middleware.py` |
| **Secrets** | `.env` git-ignored, `.env.example` has empty secret values; production refuses to start with the dev DB password, `DEBUG=true`, `CORS=*` or a placeholder JWT secret | `.gitignore`, `core/config.py` |
| **Surface** | Swagger UI / OpenAPI disabled by default in production; Celery accepts JSON only (no pickle → no RCE via the broker) | `main.py`, `tasks/celery_app.py` |
| **Email tokens** | Verification / reset links: 256-bit random, only SHA-256 stored, single use (atomic `UPDATE … WHERE used_at IS NULL RETURNING`), short expiry, newer link invalidates older; token removed from the URL after reading | `services/auth_service.py`, `pages/AccountPages.tsx` |
| **Password reset** | Same `202` answer for unknown accounts (no enumeration), email sent after the response (no timing leak), rate-limited per IP and per email, **all refresh tokens revoked** on reset | `routers/auth.py` |
| **New upload formats** | DOCX: ZIP entry count + total uncompressed size checked before parsing (zip bomb); parsed from memory. TXT/MD: NUL bytes rejected; Markdown rendered with raw HTML and images disabled | `services/conversion_service.py` |
| **Quotas** | Per-user documents / storage (row lock against concurrent uploads) / questions per day; admin-only overrides | `services/quota_service.py` |
| **Metrics** | `/metrics` not routed by nginx (internal network only), optional bearer token compared in constant time; route templates as labels (no ids, bounded cardinality) | `routers/health.py`, `core/metrics.py` |
| **Semantic cache** | Strictly per user + document scope + document version; conservative threshold + number/negation guards so a different question never gets a cached answer | `services/cache_service.py` |
| **Frontend** | React escapes all rendered text (no `dangerouslySetInnerHTML`); no third-party scripts; 401 → token cleared | `frontend/src` |

## Audit results (2026-11-24)

| Tool | Scope | Result |
|---|---|---|
| `bandit -r app scripts` | Static analysis | **0 issues** (fixed: `assert` used for runtime checks → explicit `if/raise`; MD5 in the test-only embedder marked `usedforsecurity=False`; one false positive annotated) |
| `pip-audit -r requirements.txt` | Python dependency CVEs | **0 known vulnerabilities** — was **60 advisories in 7 packages** (torch, pyjwt, starlette, python-multipart, transformers, sentence-transformers, pytest) before upgrading; full suite + browser E2E re-run green on the upgraded stack |
| `npm audit` | Frontend dependency CVEs | **0 vulnerabilities** — was 6 (react-router open redirect, Vite dev-server path traversal, PostCSS, esbuild); upgraded to React Router 7, Vite 8 |
| `pytest app/tests/test_security.py` | Hardening regression tests | 22 tests: error bodies, headers, docs exposure, production guard, injection payloads, secret hygiene |

Re-run before every release:

```powershell
bandit -r app scripts -x app/tests
pip-audit -r requirements.txt
cd frontend; npm audit
```

## Known limitations / trade-offs

| Topic | Current choice | Production upgrade path |
|---|---|---|
| Token storage | JWT in `localStorage` (simple; vulnerable if an XSS bug ever exists) | httpOnly + `Secure` + `SameSite` cookie with CSRF token |
| Logout | Stateless JWT stays valid until expiry (≤ 60 min) | Short access token + refresh token; Redis deny-list of `jti` on logout |
| Registration enumeration | `409` reveals that an email is registered (password reset does not) | Always "check your email" on sign-up |
| Rate-limit algorithm | Fixed window (burst up to 2× at window edges) | Sliding window / token bucket in Redis (Lua) |
| File scanning | PDFs are parsed but not virus-scanned | ClamAV scan in the worker before processing |
| Encryption at rest | Relies on disk/volume encryption | Encrypted volumes / S3 SSE-KMS for uploads, managed PostgreSQL encryption |
| HTTPS | Terminated by the reverse proxy (not included in local Docker) | TLS at the load balancer + `HSTS_ENABLED=true` |
