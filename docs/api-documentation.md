# API Documentation

Base URL: `/api/v1` (through nginx: `http://localhost:8080/api/v1`; direct: `http://127.0.0.1:8000/api/v1`).
Interactive docs (development): `http://127.0.0.1:8000/docs`.

**Auth:** `Authorization: Bearer <access_token>` on every endpoint except register, login and health.

**Errors** always look like:

```json
{"error": {"code": "not_found", "message": "Document not found"}}
```

| Status | `code` | When |
|---|---|---|
| 401 | `unauthorized` | Missing/invalid/expired token, wrong credentials (`WWW-Authenticate: Bearer`) |
| 403 | `permission_denied` | Authenticated but not allowed (e.g. non-admin on `/admin`) |
| 404 | `not_found` | Not found **or owned by another user** (existence is never revealed) |
| 409 | `conflict` | Duplicate email/upload, document not ready, wrong state for retry |
| 413 | `payload_too_large` | File above `MAX_UPLOAD_SIZE_MB` |
| 415 | `unsupported_media_type` | Not a PDF (extension, content type or magic bytes) |
| 422 | `unprocessable` / validation | Corrupt or password-protected PDF; invalid input (`{"detail": [{"loc", "msg", "type"}]}` — submitted values are never echoed) |
| 429 | `rate_limited` | Too many requests (`Retry-After` header, seconds) |
| 500 | `internal_error` | Unexpected error (details only in server logs) |

Every response carries `X-Request-ID` and `X-Response-Time-ms`.

---

## Authentication

### `POST /auth/register`
```json
// request (JSON)
{"name": "Asha", "email": "asha@example.com", "password": "Secur3Pass"}
// 201
{"id": "5b1c…", "name": "Asha", "email": "asha@example.com", "role": "user",
 "is_active": true, "created_at": "2026-11-24T10:00:00Z"}
```
Password: 8–128 characters, at least one letter and one digit. Email is normalised to lowercase. Rate limit: 5/hour per IP.

### `POST /auth/login`
OAuth2 password form (`application/x-www-form-urlencoded`): `username=<email>&password=<password>`
```json
// 200
{"access_token": "eyJhbGciOi…", "token_type": "bearer", "expires_in": 3600}
```
Rate limits: 10/min per IP and 10/15 min per account.

### `GET /auth/me` → the current user (same shape as register).

---

## Documents

### `POST /documents` — upload
`multipart/form-data`, field `file` (PDF ≤ 20 MB).
```json
// 201
{"id": "c793…", "filename": "handbook.pdf", "file_size": 184233, "mime_type": "application/pdf",
 "status": "queued", "total_pages": 12, "error_message": null,
 "created_at": "…", "updated_at": "…", "processed_at": null}
```
Processing happens in the background: `uploaded → queued → processing → completed | failed`.

### `GET /documents?limit=20&offset=0&status=completed`
```json
{"items": [ /* documents */ ], "total": 7, "limit": 20, "offset": 0}
```

### `GET /documents/{id}` · `GET /documents/{id}/status`
Status payload (for polling): `{"id", "status", "total_pages", "error_message", "updated_at"}`.

### `GET /documents/{id}/chunks?limit=20&offset=0`
```json
{"items": [{"id": "…", "chunk_index": 0, "content": "All full-time employees…",
            "page_start": 1, "page_end": 2, "token_count": 174}], "total": 2, "limit": 20, "offset": 0}
```

### `POST /documents/{id}/reprocess` → `202` status payload (`queued`). Allowed for `failed` or `completed` documents.

### `DELETE /documents/{id}` → `204`. Removes the row, chunks/vectors (cascade) and the file.

---

## Chat

### `POST /chat/ask`
```json
// request
{"question": "How long does it last?",
 "conversation_id": "8991e38a-…",          // optional: omit to start a new conversation
 "document_ids": ["c793…"]}                // optional: restrict the search
```
```json
// 200
{
  "conversation_id": "8991e38a-…",
  "conversation_title": "How many vacation days do I get?",
  "search_query": "what about the parental one? How long does it last?",
  "cached": false,
  "timings_ms": {"rewrite": 0.1, "retrieval": 92.3, "generation": 81.0, "total": 210.4},
  "user_message": {"id": "…", "role": "user", "content": "How long does it last?",
                   "created_at": "…", "citations": [], "document_ids": ["c793…"]},
  "assistant_message": {
    "id": "…", "role": "assistant", "created_at": "…",
    "content": "Here is what your documents say:\n- Parents receive 26 weeks of paid parental leave. [1]",
    "found": true, "mode": "extractive", "grounded": true,
    "citations": [{
      "number": 1, "document_id": "c793…", "chunk_id": "…", "filename": "handbook.pdf",
      "page_start": 2, "page_end": 2, "chunk_page_start": 1, "chunk_page_end": 2,
      "excerpt": "Parents receive 26 weeks of paid parental leave.", "score": 0.41
    }]
  }
}
```

| Field | Meaning |
|---|---|
| `found` | `false` → "The uploaded documents do not contain enough information to answer this question." |
| `mode` | `llm` (OpenAI-compatible model), `extractive` (quoted sentences, no LLM), `none` (nothing relevant retrieved) |
| `grounded` | The answer cites at least one real source |
| `citations[].page_start/end` | Exact page(s) of the quoted excerpt; `chunk_page_*` = whole passage range |
| `cached` | Served from the Redis answer cache |

Errors: `404` unknown/foreign conversation or document id · `409` selected document not processed yet · `429` rate limit (30/min per user).

---

## Conversations

| Method | Path | Response |
|---|---|---|
| GET | `/conversations?limit=20&offset=0` | `{"items": [{"id", "title", "created_at", "updated_at", "message_count", "last_message_preview", "last_message_at"}], "total", …}` — most recently active first |
| GET | `/conversations/{id}` | One conversation summary |
| GET | `/conversations/{id}/messages?limit=100&offset=0` | Messages oldest first (same shape as `user_message` / `assistant_message`) |
| DELETE | `/conversations/{id}` | `204` — messages are deleted too |

---

## Admin (role `admin`)

| Method | Path | Description |
|---|---|---|
| GET | `/admin/users?limit&offset` | Users (never password hashes) |
| GET | `/admin/documents/stats` | Counts by status, documents, chunks, chunks with embeddings, storage bytes |
| GET | `/admin/documents/failed?limit&offset` | Failed documents: filename, owner email, error (metadata only, no content) |
| POST | `/admin/documents/{id}/retry` | Re-queue a failed document → `202` |
| GET | `/admin/metrics` | App uptime/mode/model, DB pool, Redis, queue length, cache hit/miss, rate-limit rejections, counts |

Create the first admin: `python -m scripts.create_admin --email admin@example.com`.

---

## Health

| Path | Use | Response |
|---|---|---|
| `GET /health/live` | Liveness probe (process up) | `{"status": "ok"}` |
| `GET /health/ready` | Readiness probe (PostgreSQL + Redis) | `200 {"status": "ok", "checks": {"database": {"status": "ok", "pgvector": "0.8.7"}, "redis": {"status": "ok"}}}` or `503 degraded` |

## Example session (curl)

```bash
B=http://localhost:8080/api/v1
curl -s -X POST $B/auth/register -H 'Content-Type: application/json' \
     -d '{"name":"Asha","email":"asha@example.com","password":"Secur3Pass"}'
TOKEN=$(curl -s -X POST $B/auth/login -d 'username=asha@example.com&password=Secur3Pass' | jq -r .access_token)
curl -s -H "Authorization: Bearer $TOKEN" -F "file=@handbook.pdf;type=application/pdf" $B/documents
curl -s -H "Authorization: Bearer $TOKEN" $B/documents          # wait for "completed"
curl -s -X POST $B/chat/ask -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"question":"How many vacation days do I get?"}' | jq .assistant_message
```
