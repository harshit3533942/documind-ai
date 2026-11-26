# DocuMind AI

**Ask questions about your PDFs and get answers with exact page citations.**
A full-stack Retrieval-Augmented Generation (RAG) platform: FastAPI · PostgreSQL + pgvector ·
Celery + Redis · Sentence Transformers · React + TypeScript · Docker.

![tests](https://img.shields.io/badge/tests-307%20passing-brightgreen) ![coverage](https://img.shields.io/badge/app%20coverage-97%25-brightgreen) ![security](https://img.shields.io/badge/pip--audit%20%7C%20npm%20audit%20%7C%20bandit-0%20issues-brightgreen) ![python](https://img.shields.io/badge/python-3.13-blue)

## Features

- **Secure accounts** — registration, login, JWT, Argon2id, role-based admin access, rate-limited login
- **PDF upload** — drag & drop with progress; validated (type, magic bytes, size, corrupt, password-protected), streamed to storage
- **Background processing** — Celery workers extract text (PyMuPDF), chunk it page-aware, embed it locally, store vectors in pgvector; live status `uploaded → queued → processing → completed | failed`, automatic retries
- **Grounded answers** — semantic search over *your own* documents only; answers cite **file + exact page + excerpt**; says *"the documents do not contain enough information"* instead of guessing
- **Conversations** — history, continue later, follow-up questions ("and sick leave?"), choose which documents to search
- **Works without an API key** — local extractive mode out of the box; plug in any OpenAI-compatible LLM (OpenAI, Groq, Ollama) via `.env`
- **Fast** — Redis answer cache (~40 ms vs ~300 ms), query-embedding cache, versioned cache invalidation
- **Admin** — users, processing statistics, failed-task monitoring and retry, system metrics
- **Production-minded** — strict per-user isolation, security headers, safe error responses, health checks, one-command Docker deployment, CI

## Architecture

```mermaid
flowchart LR
    UI[React SPA] --> NG[nginx] --> API[FastAPI]
    API --> PG[(PostgreSQL + pgvector)]
    API --> RD[(Redis: queue · cache · rate limits)]
    RD --> W[Celery worker<br/>extract → chunk → embed]
    W --> PG
    API -.optional.-> LLM[[LLM API]]
```

RAG flow: *question → (rewrite follow-up) → embed → cosine search filtered by user →
threshold → numbered context → LLM or extractive answer → citations validated against the database.*

| Document | Contents |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Layers, request sequence diagrams, design decisions, scaling |
| [docs/database-design.md](docs/database-design.md) | ER diagram, relationships, indexes, HNSW |
| [docs/rag-pipeline.md](docs/rag-pipeline.md) | Retrieval, hallucination controls, citations, score calibration |
| [docs/api-documentation.md](docs/api-documentation.md) | Every endpoint with sample requests/responses and error codes |
| [docs/deployment.md](docs/deployment.md) | Docker architecture, configuration, production checklist, cloud options |
| [docs/security.md](docs/security.md) | Security controls, audit results, known trade-offs |
| [docs/testing.md](docs/testing.md) | Test strategy, requirement → test map, bugs the tests caught |
| [docs/interview-prep.md](docs/interview-prep.md) | Project pitch, 12 core Q&As, system-design walkthrough |

## Quick start (Docker — recommended)

Prerequisite: Docker Desktop.

```powershell
copy .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(64))"   # put this into JWT_SECRET_KEY in .env
docker compose up -d --build
```

Open **http://localhost:8080** → register → upload a PDF → ask a question.
Details and production checklist: [docs/deployment.md](docs/deployment.md).

## Local development

Prerequisites: Python 3.13, Node 20.19+, Docker Desktop (for PostgreSQL + Redis).

```powershell
# backend
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
copy .env.example .env                       # set JWT_SECRET_KEY
docker compose up -d postgres redis
alembic upgrade head
uvicorn app.main:app --reload                # API on http://127.0.0.1:8000 (Swagger: /docs)
celery -A app.tasks.celery_app worker --pool=solo -l info   # 2nd terminal (Windows needs --pool=solo)
celery -A app.tasks.celery_app beat -l info                 # optional 3rd terminal (sweeper)

# frontend (4th terminal)
cd frontend; npm install; npm run dev        # http://127.0.0.1:5173
```

Create an admin: `python -m scripts.create_admin --email you@example.com`.

## Testing

```powershell
pytest                          # 307 tests against real PostgreSQL/pgvector (~2 min)
pytest -m "not model"           # skip tests that load the real embedding model (~1 min)
pytest --cov=app                # coverage (97 % of application code)
python -m scripts.e2e_frontend  # browser end-to-end test (Playwright; full stack running)
python -m scripts.benchmark --email … --password …   # latency percentiles
```

CI (GitHub Actions) runs migrations, the full suite with a 90 % coverage gate, and the frontend build on every push.

## Tech stack

| Layer | Technology |
|---|---|
| API | FastAPI, Pydantic v2, Uvicorn |
| Database | PostgreSQL 16, pgvector 0.8 (HNSW, cosine), SQLAlchemy 2.0, Alembic |
| PDF | PyMuPDF (optional Tesseract OCR) |
| AI | Sentence Transformers `all-MiniLM-L6-v2` (384-d, local CPU), OpenAI-compatible LLM (optional) |
| Background jobs | Celery 5, Redis 7 (broker, cache, rate limiting) |
| Security | JWT (PyJWT), Argon2id, RBAC, rate limiting, security headers |
| Frontend | React 18, TypeScript, Vite 8, Tailwind CSS, React Router 7 |
| Testing | Pytest, FastAPI TestClient, fakeredis, Playwright, pytest-cov |
| Ops | Docker, Docker Compose, nginx, GitHub Actions |

## Configuration

All settings come from environment variables / `.env` — see [.env.example](.env.example).
Most important: `JWT_SECRET_KEY` (required), `POSTGRES_PASSWORD`, `OPENAI_API_KEY`
(optional), `ENVIRONMENT=production` (enables startup safety checks).

## Performance (local laptop, CPU)

| Request | p50 | p95 |
|---|---|---|
| `GET /documents` | ~21 ms | ~31 ms |
| `POST /chat/ask` (full RAG, extractive) | ~310 ms | ~430 ms |
| `POST /chat/ask` (answer cache hit) | ~40 ms | ~45 ms |

## Known limitations

- Extractive mode (no LLM key) quotes sentences rather than writing a fluent answer and may include a closely related extra sentence.
- Scanned PDFs need OCR (`OCR_ENABLED=true` + Tesseract installed); without it they are marked *failed* with a clear message.
- JWTs live in `localStorage` and remain valid until expiry after logout (see [docs/security.md](docs/security.md) for the cookie/refresh-token upgrade path).
- One embedding model process handles queries serially; scale with more API replicas or a GPU.
- Uploads use local/volume storage; production should use object storage (S3).

## Future improvements

Streaming answers (SSE) · hybrid search (BM25 + vectors) and re-ranking · OCR by default ·
S3 storage with presigned URLs · refresh tokens + httpOnly cookies · email verification ·
RAG evaluation set (faithfulness / recall@k) in CI · multi-tenant teams and sharing ·
Prometheus/Grafana metrics · Kubernetes manifests.

## Project status

- [x] **Phase 1** – FastAPI foundation, config, logging, error handling, health checks
- [x] **Phase 2** – PostgreSQL, SQLAlchemy models, pgvector, Alembic, ER diagram
- [x] **Phase 3** – Authentication (JWT, Argon2, RBAC), admin CLI
- [x] **Phase 4** – PDF upload, validation, secure storage, PyMuPDF extraction
- [x] **Phase 5** – Page-aware chunking with overlap, idempotent pipeline
- [x] **Phase 6** – Local embeddings in pgvector, batching, reuse, semantic search
- [x] **Phase 7** – Celery + Redis processing, retries, crash recovery, sweeper, admin monitoring
- [x] **Phase 8** – RAG engine: calibrated retrieval, LLM + extractive fallback, exact page citations
- [x] **Phase 9** – Chat & conversation API with follow-ups and document selection
- [x] **Phase 10** – Redis caching, rate limiting, timing middleware, metrics, benchmark
- [x] **Phase 11** – React + TypeScript frontend, browser E2E test
- [x] **Phase 12** – 307 tests, 97 % coverage, CI
- [x] **Phase 13** – Security audit and hardening (0 known vulnerabilities)
- [x] **Phase 14** – Docker images, full Docker Compose deployment
- [x] **Phase 15** – Documentation and interview preparation
