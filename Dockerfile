# Stages: base (system + Python deps) -> runtime (coordinator, engine, agent-api) -> test;
#         base -> embedder-deps (PyTorch, sentence-transformers, the built-in model) -> embedder.
# The heavy embedder layers come before any COPY of the app, so code changes never reinstall
# PyTorch or re-download the model (docs/DESIGN-0.4.1.md section 2).
FROM python:3.11-slim AS base

RUN apt-get update && apt-get install -y postgresql-client && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    "uvicorn[standard]>=0.30.0" \
    "fastapi>=0.115.0" \
    "sqlalchemy[asyncio]>=2.0.0" \
    "alembic>=1.13.0" \
    "asyncpg>=0.30.0" \
    "pgvector>=0.3.0" \
    "redis[hiredis]>=5.0.0" \
    "pydantic>=2.0.0" \
    "pydantic-settings>=2.0.0" \
    "psycopg[binary]>=3.2.0" \
    "httpx"

FROM base AS runtime

COPY pyproject.toml /opt/daash/pyproject.toml
COPY app /opt/daash/app
COPY alembic.ini /opt/daash/alembic.ini
COPY migrations /opt/daash/migrations
COPY entrypoint.sh /opt/daash/entrypoint.sh
# The runner, served as one file to agent hosts (docs/DESIGN-0.4.md 5.9): /runner/daash-runner.pyz
COPY daash_runner /opt/daash/daash_runner

RUN chmod +x /opt/daash/entrypoint.sh \
    && cd /opt/daash && python -m daash_runner.pyz /opt/daash/daash-runner.pyz
WORKDIR /opt/daash
ENTRYPOINT ["/opt/daash/entrypoint.sh"]

# Test image: runtime plus pytest and the test suite (compose service `test`).
FROM runtime AS test

RUN pip install --no-cache-dir "pytest>=8.0.0" "pytest-asyncio>=0.24.0"

COPY tests /opt/daash/tests
COPY daash_runner /opt/daash/daash_runner

ENTRYPOINT []
CMD ["python", "-m", "pytest", "tests/unit", "-q"]

# Embedder service (docs/DESIGN-0.4.1.md section 2): runtime deps plus PyTorch (CPU wheels),
# transformers and sentence-transformers, and the built-in embedding model saved as a plain
# folder that loads with no network. transformers stays on 4.57: 5.x breaks gte-base-en-v1.5's
# own model code (its rotary position ids come out uninitialised).
FROM base AS embedder-deps

RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu "torch==2.15.0" \
    && pip install --no-cache-dir "transformers==4.57.6" "sentence-transformers==5.7.0"

ARG DAASH_EMBEDDING_MODEL=Alibaba-NLP/gte-base-en-v1.5
COPY scripts/bake-embedding-model.py /opt/daash/scripts/bake-embedding-model.py
RUN python /opt/daash/scripts/bake-embedding-model.py "$DAASH_EMBEDDING_MODEL" /opt/daash/embedding-models

FROM embedder-deps AS embedder

COPY pyproject.toml /opt/daash/pyproject.toml
COPY app /opt/daash/app
COPY entrypoint.sh /opt/daash/entrypoint.sh
RUN chmod +x /opt/daash/entrypoint.sh && mkdir -p /var/lib/daash/models
# Downloaded models (and the code of models allowed to run their own) go to the daash-models volume.
ENV HF_HOME=/var/lib/daash/models/hf \
    DAASH_EMBEDDING_MODEL=${DAASH_EMBEDDING_MODEL}
WORKDIR /opt/daash
ENTRYPOINT ["/opt/daash/entrypoint.sh"]
CMD ["embedder"]
