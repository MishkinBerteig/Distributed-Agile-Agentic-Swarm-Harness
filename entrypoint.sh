#!/usr/bin/env bash
set -e

# Usage: entrypoint.sh [api|engine]   (default: api)
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
  *)
    echo "Unknown mode '$MODE' (expected: api | engine)" >&2
    exit 2
    ;;
esac
