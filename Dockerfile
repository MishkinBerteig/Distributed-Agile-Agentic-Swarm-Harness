FROM python:3.11-slim AS runtime

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

COPY pyproject.toml /opt/daash/pyproject.toml
COPY app /opt/daash/app
COPY alembic.ini /opt/daash/alembic.ini
COPY migrations /opt/daash/migrations
COPY entrypoint.sh /opt/daash/entrypoint.sh

RUN chmod +x /opt/daash/entrypoint.sh
WORKDIR /opt/daash
ENTRYPOINT ["/opt/daash/entrypoint.sh"]

# Test image: runtime plus pytest and the test suite (compose service `test`).
FROM runtime AS test

RUN pip install --no-cache-dir "pytest>=8.0.0" "pytest-asyncio>=0.24.0"

COPY tests /opt/daash/tests

ENTRYPOINT []
CMD ["python", "-m", "pytest"]
