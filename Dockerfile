# DocuMind AI backend image - used for the API, the Celery worker, Celery beat
# and the one-off migration job (different `command` in docker-compose.yml).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/models

WORKDIR /app

# 1) Dependencies first (cached layer: only rebuilt when requirements change).
#    torch from the CPU index: the default Linux wheel bundles ~2 GB of CUDA.
COPY requirements.txt .
RUN pip install torch==2.15.0 --index-url https://download.pytorch.org/whl/cpu \
 && pip install -r requirements.txt

# 2) Bake the embedding model into the image: no download at container start,
#    works without internet, and every container uses the identical model.
#    Same for the cross-encoder used for re-ranking (RAG_RERANK_ENABLED).
ARG EMBEDDING_MODEL_NAME=sentence-transformers/all-MiniLM-L6-v2
ARG RERANK_MODEL_NAME=cross-encoder/ms-marco-MiniLM-L-6-v2
RUN python -c "from sentence_transformers import SentenceTransformer as S, CrossEncoder as C; S('${EMBEDDING_MODEL_NAME}'); C('${RERANK_MODEL_NAME}')"

# 3) Application code.
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY scripts ./scripts

# 4) Run as an unprivileged user; it can only write to the uploads directory.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin documind \
 && mkdir -p /app/storage/uploads \
 && chown -R documind:documind /app/storage
USER documind

# The model is already in the image: never try to reach the Hugging Face Hub.
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3).status == 200 else 1)"

# --proxy-headers: trust X-Forwarded-For from nginx so rate limits see the real
# client IP. The API port is only reachable inside the compose network.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--no-access-log"]
