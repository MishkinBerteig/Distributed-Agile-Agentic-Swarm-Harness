#!/usr/bin/env bash
set -e

# Usage: entrypoint.sh [api|engine|agent-api|embedder]   (default: api)
MODE="${1:-api}"

# Wait for PostgreSQL to be ready
echo "Waiting for PostgreSQL to be ready..."
DA_HOST="${DAASH_DATABASE_HOST:-db}"
until pg_isready -h "$DA_HOST" -p 5432 -U daash; do
  sleep 1
done
echo "PostgreSQL is ready."

case "$MODE" in
  api)
    # Run schema migrations (env.py reads DAASH_ALEMBIC_DATABASE_URL / DAASH_DATABASE_URL)
    echo "Running Alembic migrations..."
    alembic -c /opt/daash/alembic.ini upgrade head
    echo "Migrations complete."
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000
    ;;
  engine)
    # The coordinator owns migrations; the engine only starts once it is healthy.
    exec python -m app.engine
    ;;
  agent-api)
    # Agent-facing port (docs/DESIGN-0.4.md section 6): /health, /mcp and /runner/* only.
    # The coordinator owns migrations; compose starts this once the coordinator is healthy.
    exec uvicorn app.agent_api:app --host 0.0.0.0 --port 8001
    ;;
  embedder)
    # The embedder service (docs/DESIGN-0.4.1.md section 2): the embedding model, POST /embed and
    # the indexer. Internal port 8002 only. Compose starts it once the coordinator (migrations) is healthy.
    # No access log: every stored or searched text is a POST /embed.
    exec uvicorn app.embedder_api:app --host 0.0.0.0 --port 8002 --no-access-log
    ;;
  *)
    echo "Unknown mode '$MODE' (expected: api | engine | agent-api | embedder)" >&2
    exit 2
    ;;
esac
