# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project

DocuMind AI — RAG platform for asking questions about PDFs with exact page citations.
FastAPI + PostgreSQL/pgvector + Celery/Redis + local Sentence Transformers embeddings + React/TypeScript frontend.
All 15 build phases are complete (see README "Project status").

## Environment (Windows)

- Python 3.13 venv in `.venv` (do not use 3.14 — ML wheels). Activate: `.\.venv\Scripts\Activate.ps1`
- Node 20.19+ for `frontend/`
- Docker Desktop provides PostgreSQL (pgvector/pg16) and Redis 7. **Docker Desktop must be running** before anything touching the DB/Redis.
- Config comes from `.env` (template: `.env.example`). `JWT_SECRET_KEY` is required. Never commit `.env`.

## Common commands

```powershell
# infra only (for local dev / tests)
docker compose up -d postgres redis
alembic upgrade head

# backend (separate terminals)
uvicorn app.main:app --reload                                # http://127.0.0.1:8000/docs
celery -A app.tasks.celery_app worker --pool=solo -l info    # --pool=solo is required on Windows
celery -A app.tasks.celery_app beat -l info                  # optional sweeper

# frontend
cd frontend; npm run dev                                     # http://127.0.0.1:5173

# full stack in Docker (alternative to all of the above)
docker compose up -d --build                                 # http://localhost:8080

# tests (need postgres + redis running; uses documind_test DB)
pytest                     # full suite
pytest -m "not model"      # skip tests loading the real embedding model
pytest --cov=app           # CI gate is 90 %

# admin user
python -m scripts.create_admin --email you@example.com
```

## Architecture

Layered backend under `app/`:

- `routers/` — HTTP endpoints only (auth, documents, chat, conversations, admin, health)
- `services/` — business logic (pdf, chunking, embedding, retrieval, rag, llm, cache, storage, task_queue)
- `repositories/` — DB access (SQLAlchemy 2.0)
- `models/` — ORM models; `schemas/` — Pydantic v2 request/response models
- `core/` — config (`pydantic-settings`), security (JWT, Argon2id), middleware, rate limiting, exceptions
- `tasks/` — Celery app and document processing tasks (extract → chunk → embed)
- `tests/` — pytest suite against real PostgreSQL; Redis faked with fakeredis
- `alembic/versions/` — migrations; `scripts/` — CLI helpers (admin, benchmark, e2e)

Detailed docs live in `docs/` (architecture, database-design, rag-pipeline, api-documentation, deployment, security, testing, interview-prep).

## Rules

- Every query on user data must be filtered by the owning user (strict per-user isolation).
- `EMBEDDING_DIMENSION` must match both the model and the DB migration (384 for all-MiniLM-L6-v2).
- Schema changes go through a new Alembic migration, never by editing existing ones.
- Without `OPENAI_API_KEY`, the app answers in local extractive mode — that is expected, not a bug.
- Add/update tests with every behaviour change; keep coverage above the 90 % CI gate.

## Working with the user

The user is learning backend engineering for interview prep: explain in simple Hinglish, build and verify changes incrementally.
