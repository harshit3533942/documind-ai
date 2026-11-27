# Load testing (Locust)

`scripts/load/locustfile.py` simulates real users through nginx, exactly like the browser:
register → log in → upload a document (TXT) → wait until the worker has processed it →
then a mix of questions (some repeated → exact cache, some paraphrased → semantic cache,
some new, one unanswerable), library, history, search and usage calls.

## How to run

Locust runs in its own Docker container (its `gevent` dependency conflicts with
Playwright's pinned `greenlet` in the dev venv). The target must not rate-limit the test
client and should not call a paid LLM:

```powershell
# 1. target in load-test mode
$env:RATE_LIMIT_ENABLED="false"; $env:LLM_PROVIDER="extractive"
docker compose up -d api worker

# 2. 20 users, one new user every 5 s, 5 minutes, CSV results in scripts/load/results/
docker run --rm --network documindai_default -v "${PWD}/scripts/load:/mnt/locust" `
  locustio/locust:2.47.0 -f /mnt/locust/locustfile.py --host http://frontend `
  --headless -u 20 -r 0.2 -t 5m --csv /mnt/locust/results/run

# 3. back to normal (rate limits on, LLM as configured)
Remove-Item Env:RATE_LIMIT_ENABLED, Env:LLM_PROVIDER; docker compose up -d api worker
```

Without `--headless` Locust serves its web UI (add `-p 8089:8089`). Watch the Grafana
dashboard (`docker compose --profile monitoring up -d`, http://localhost:3000) during a run.

Test machine: laptop, 8 CPU cores, everything (API, worker, PostgreSQL, Redis, nginx,
Locust) in Docker Desktop on the same machine, CPU-only models, one Celery worker.

## Results

### Run 1 — 20 users arriving within 10 s (`-r 2`, 3 min): found a bug

| Request | p50 | max | Failures |
|---|---|---|---|
| `POST /auth/login` | **61 s** | 62 s | **2 × HTTP 500** |
| `POST /auth/register` | 26 s | 31 s | 0 |
| `POST /chat/ask` | 6 s | 27 s | 0 |

The 500s were `QueuePool limit of size 5 overflow 10 reached` — the database connection
pool was exhausted. **Root cause:** login and registration held a pooled DB connection
while running Argon2id, which is slow *on purpose* (64 MiB, several passes). With 20
simultaneous logins, all 15 connections were busy waiting for password hashes, and up to
40 hashes ran at once (FastAPI's thread pool), each starving the others of CPU.

**Fix** (`core/security.py`, `services/auth_service.py`, regression tests in
`tests/test_load_regressions.py`):

1. Never hold a DB connection while hashing: register hashes before the first query;
   login ends the read transaction (connection back to the pool) before verifying.
2. Bound concurrent hashes with a semaphore: `cores / argon2 parallelism` (= 2 on 8 cores,
   because each hash already uses 4 threads). Waiting for a slot is cheaper than 40 hashes
   fighting over the CPU.

### Run 2 — same burst after the fix (3 min)

| Request | p50 | Failures |
|---|---|---|
| `POST /auth/login` | 24 s (was 61 s) | **0** (was 2) |
| `POST /auth/register` | 16 s (was 26 s) | 0 |

Still slow: 20 accounts created in 10 s means 40 Argon2 hashes + 20 uploads + embedding
work on 8 cores at once — a burst no real product sees, and the users never got past
sign-up within 3 minutes. That run measured the burst, not normal use.

### Run 3 — realistic ramp-up: 20 users, one every 5 s, 5 min

**2,156 requests, 0 failures, 7.3 requests/s.**

| Request | Count | p50 | p95 |
|---|---|---|---|
| `POST /chat/ask` (all) | 744 | **350 ms** | 3.2 s |
| ↳ exact cache hit | 218 | 2 ms | 15 ms |
| ↳ semantic cache hit | 46 | 10 ms | 36 ms |
| `GET /documents` | 225 | 30 ms | 120 ms |
| `GET /conversations` | 211 | 33 ms | 140 ms |
| `GET /conversations?q=` (search) | 114 | 44 ms | 160 ms |
| `POST /documents` (upload TXT) | 20 | 410 ms | 6.4 s |
| `POST /auth/register` | 20 | 1.3 s | 9.1 s |
| `POST /auth/login` | 20 | 650 ms | 8.7 s |
| upload → searchable (worker) | 20 | **36 s** | 76 s |

35 % of questions were answered from a cache (29 % exact, 6 % semantic) in ≤ 15 ms
instead of hundreds of milliseconds.

## What limits throughput (and how to scale)

| Bottleneck | Evidence | Scaling option |
|---|---|---|
| **One Celery worker** processes documents one at a time | upload → searchable p50 36 s although one document takes a few seconds: it waits in the queue | `WORKER_CONCURRENCY` / more worker containers (each loads its own model) |
| **CPU-bound models in the API** (query embedding, cross-encoder re-ranking) | `chat: ask` p95 3.2 s while the worker embeds documents on the same cores | more API replicas on more machines, GPU, or `RAG_RERANK_ENABLED=false` |
| **Argon2id** (by design) | sign-up/login p95 ~9 s while 2 hashes run at a time | acceptable — it is what makes stolen hashes expensive to crack; scale API replicas |
| Everything on one laptop | Locust, DB and workers compete for the same 8 cores | separate machines / managed PostgreSQL in production |

Lessons: a load test finds problems that functional tests cannot (the connection-pool
exhaustion only appears with concurrency), and the arrival pattern matters — measure the
burst you expect *and* steady state.
