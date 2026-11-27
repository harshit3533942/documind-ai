# Interview Preparation — DocuMind AI

Answers are short on purpose: say the one-line answer first, then go deeper
using *this project* as the example. Interviewers remember concrete details.

## 2-minute project pitch

> "DocuMind AI is a RAG platform: users upload PDFs and ask questions, and get
> answers with exact page citations. The backend is FastAPI with PostgreSQL +
> pgvector. Uploads are validated and stored, then a Celery worker extracts text
> with PyMuPDF, splits it into page-aware chunks, embeds them with a local
> Sentence-Transformers model and stores the vectors. A question is embedded,
> searched with cosine similarity — always filtered by the user's id — and the
> top chunks go to an LLM with a strict 'answer only from these sources' prompt.
> Citations come from database metadata, so page numbers can't be hallucinated.
> Redis is the Celery broker, the cache and the rate limiter. It has a React
> frontend, ~300 tests including a browser end-to-end test, and runs with one
> `docker compose up`. Along the way I fixed real problems like a non-thread-safe
> tokenizer found by load testing and a dependency mismatch that only appeared
> at runtime."

## The 12 core questions

**1. What is RAG?**
Retrieval-Augmented Generation: first *retrieve* relevant text from your own data,
then let the LLM *generate* an answer using only that text. Like an open-book exam.
Benefits: answers about private/new data without retraining, fewer hallucinations,
verifiable citations. In DocuMind: retrieve top-5 chunks with pgvector → numbered
sources in the prompt → answer cites [n] → mapped to file + page from the DB.

**2. LLM vs embedding model?**
An embedding model turns text into a fixed-size vector (here 384 numbers) so
similar meanings are close together — used for *search*. An LLM generates text —
used for the *answer*. Embedding model: small (90 MB), fast, local, deterministic.
LLM: large, slower, costly. We embed every chunk once; we call the LLM once per question.

**3. Why do we need vector databases?**
Keyword search can't match "vacation days" to "paid annual leave". Vectors capture
meaning, but finding the nearest of millions of vectors needs an index — HNSW,
an approximate-nearest-neighbour graph — or every query scans everything (O(N)).
We use pgvector *inside PostgreSQL* so the `user_id` security filter, metadata and
vectors live in one transactional database.

**4. How does cosine similarity work?**
cos θ = (A·B) / (|A||B|): the angle between two vectors, 1 = same direction (same
meaning), 0 = unrelated. We normalise vectors to length 1, so it's just a dot
product. pgvector's `<=>` returns cosine *distance* = 1 − similarity. Real numbers
from this project: relevant chunks 0.26–0.74, unrelated questions < 0.16 →
threshold 0.20.

**5. What happens when a PDF is uploaded?**
Rate-limit check → extension + content-type → streamed to disk in 1 MB pieces under
a random name with size limit and SHA-256 → `%PDF-` magic bytes → PyMuPDF opens it
(corrupt / encrypted / page limit) → duplicate check → row inserted `uploaded` →
job enqueued → `queued` → `201`. The worker then claims it atomically, extracts page
by page, chunks, embeds in batches and writes everything in one transaction →
`completed` (or `failed` with a user-safe message).

**6. Why background processing?**
Embedding a 300-page PDF takes seconds to minutes. In the request it would block
the user, risk proxy timeouts, and tie up API threads. With Celery the API answers
in ~20 ms, workers scale separately, jobs survive restarts (`acks_late`), transient
errors are retried with exponential backoff, and permanent errors are not retried.

**7. How does Redis work (and how is it used here)?**
In-memory key-value store, single-threaded command execution (each command is
atomic), optional persistence (AOF). Here: (1) Celery broker — jobs are list entries;
(2) cache — answers and query embeddings with TTL, invalidated by a per-user version
counter; (3) rate limiting — `INCR` + `EXPIRE NX` in one transaction per window.
All Redis features fail open.

**8. How would you handle 10,000 simultaneous users?**
Measure first (our benchmark: one CPU process, ~2 s p50 at 8 parallel requests →
CPU-bound queueing). Then: stateless API → many replicas behind a load balancer;
`uvicorn --workers`; GPU or hosted embeddings; answer + embedding caches (cached
answer: ~40 ms); LLM streaming; PgBouncer for DB connections; read replicas;
autoscale Celery workers on queue length; S3 for files; per-user rate limits to
protect fairness.

**9. How would you optimise a slow vector search?**
`EXPLAIN ANALYZE` to confirm the HNSW index is used; tune `hnsw.ef_search`
(recall vs speed); with filters use pgvector 0.8 iterative scan (we do) so the
`user_id` filter doesn't return too few rows; build HNSW with suitable `m` /
`ef_construction`; smaller vectors (384-d) or quantisation; partition by user/tenant;
enough RAM for the index; cache frequent queries.

**10. How do you prevent one user from reading another user's documents?**
Defence in depth: every repository method requires `user_id` in the WHERE clause;
chunks carry `user_id` so vector search filters without a JOIN; another user's id
returns 404 (not 403 — doesn't reveal existence); UUIDs are not enumerable; files
stored under random names; admins see metadata only; isolation is enforced in SQL —
never by the LLM — so prompt injection can't bypass it. Tests try it explicitly
(Bob's perfectly matching chunk never reaches Alice's prompt).

**11. How would you reduce LLM hallucinations?**
Retrieve good context (chunking, threshold); if nothing relevant → refuse *without*
calling the LLM; strict system prompt (only sources, cite everything,
`INSUFFICIENT_CONTEXT` escape); low temperature; delimit/escape sources; validate
citation numbers; page numbers from DB metadata; `grounded=false` flag when nothing is
cited; evaluation set of known Q&A pairs to measure faithfulness.

**12. Synchronous vs asynchronous APIs?**
Two meanings. (a) *Request style*: synchronous = client waits for the result
(`/chat/ask`); asynchronous = server accepts and returns immediately, client polls
or gets notified (upload → `202/queued` → status polling). (b) *Python execution*:
`async def` runs on the event loop and must never block; `def` endpoints run in a
thread pool. We use `def` for CPU work (Argon2, embeddings) and the sync DB driver,
so the event loop stays free.

## System design walkthrough (whiteboard order)

1. **Requirements** — upload PDFs, ask questions, citations, history, isolation, admin.
   Non-functional: latency (< 1 s answer without LLM), correctness of citations,
   privacy, cost.
2. **High level** — SPA → nginx → stateless API → PostgreSQL+pgvector, Redis,
   object storage, worker pool, optional LLM (diagram in `architecture.md`).
3. **Data model** — users, documents, document_chunks (vector + page offsets +
   denormalised user_id), conversations, messages (citation snapshot JSONB).
4. **Ingestion pipeline** — validation, async processing, idempotency (atomic claim,
   unique `(document_id, chunk_index)`, single transaction), retries, sweeper.
5. **Query pipeline** — rewrite follow-ups, embed, filtered ANN search, threshold,
   context budget, generation, citation validation, caching.
6. **Scale & reliability** — replicas, queues, caching, rate limits, health checks,
   fail-open dependencies, observability (request id, timings, metrics).
7. **Security** — auth, isolation, upload safety, prompt injection, secrets.
8. **Trade-offs** — pgvector vs dedicated vector DB; local vs hosted embeddings;
   fixed vs sliding window rate limit; localStorage vs cookie tokens.

## Improvement features — likely questions

| Question | Short answer |
|---|---|
| How do you support DOCX/TXT/Markdown without a second pipeline? | Convert to PDF at upload (PyMuPDF Story), so pages/citations/viewer stay identical. The exact text per page goes into a sidecar because re-extracting from the PDF garbles e.g. Devanagari. |
| How do live status updates work? | Worker `PUBLISH`es to a per-user Redis channel; an SSE endpoint subscribes and streams; a DB snapshot on every (re)connect covers lost pub/sub messages; slow polling as fallback. |
| Why is `ILIKE '%term%'` fast here? | pg_trgm GIN index: trigrams make leading-wildcard search index-assisted. |
| How is password reset made safe? | Random token, only hash stored, single-use, 30 min expiry, same response for unknown emails, email sent after the response, all sessions revoked. |
| What do you monitor and why route templates as labels? | RED metrics per route, RAG stage latency, cache hit ratio, queue length. Raw paths with UUIDs would explode the number of time series. |
| Soft delete — what can go wrong? | Every query must exclude trashed rows (search, chat selection, lists); duplicates must point to the trash; storage still counts until purge; a beat job purges after 30 days. |
| How do you stop two parallel uploads from exceeding a quota? | `SELECT … FOR UPDATE` on the user row inside the upload transaction serialises them. |
| What is the risk of a semantic cache? | Serving an answer to a *different* question. Measured: "revenue 2023" vs "2024" = 0.93 similarity. So threshold 0.95 + exact numbers + negation guard, scoped per user/documents/version. |
| What did the load test find? | Connection-pool exhaustion: Argon2 ran while holding a DB connection. Fix: release the connection before hashing + semaphore on concurrent hashes. 2 × 500 → 0. |

## Real bugs from this project (great "tell me about a challenge" answers)

| Situation | Root cause | Fix | Lesson |
|---|---|---|---|
| Concurrent benchmark → `RuntimeError: Already borrowed` | HuggingFace Rust tokenizer isn't thread-safe; FastAPI runs `def` endpoints in a thread pool | Lock around model use + 16-thread regression test (proved it fails without the lock 5/5) | Single-request tests can't find concurrency bugs — load test |
| Uploads silently stuck in `uploaded` | kombu 5.7 needs redis-py ≥ 6.1, checked only at runtime; tests used a fake queue | Pinned compatible kombu + test that opens a real broker channel | Fakes hide integration bugs; pin transitive deps |
| Corrupt PDF couldn't be deleted (Windows) | `raise … from exc` kept PyMuPDF's exception (and file handle) alive | Raise outside `except`, log `str(exc)` | Exception chains hold references |
| Chat messages in random order | `now()` is the *transaction start* time | `clock_timestamp()` | Know your database's time functions |
| Citations "pp. 1–3" for a sentence on p. 2 | Chunk-level page range only | Store character→page offsets per chunk | Precision matters for trust |
| Thresholds wrongly guessed | Assumed scores instead of measuring | Calibrated on 11 real questions, regression-tested | Measure, don't guess |
| 60 CVEs in dependencies | Old pins | pip-audit, upgraded, full suite + E2E green | Audit dependencies continuously |
| Login p50 61 s + HTTP 500 under 20 concurrent users | Argon2 hash computed while holding a pooled DB connection; 40 hashes at once | Release connection before hashing; semaphore = cores / argon2 parallelism | Slow-by-design code must not hold shared resources |
| Hindi text garbled in converted DOCX/TXT | PDF text re-extraction loses complex-script glyph mapping | Keep the original text per page in a sidecar | Don't round-trip data you already have |
| Rejected .docx could not be deleted (Windows) | python-docx's exception kept the ZIP file handle open | Parse from `BytesIO` | Same lesson as the PyMuPDF bug: exceptions hold references |
