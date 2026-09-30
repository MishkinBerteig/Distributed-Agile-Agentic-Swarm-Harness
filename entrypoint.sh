#!/usr/bin/env bash
set -e

# Wait for PostgreSQL to be ready
echo "Waiting for PostgreSQL to be ready..."
DA_HOST="${DAASH_DATABASE_HOST:-db}"
until pg_isready -h "$DA_HOST" -p 5432 -U daash; do
  sleep 1
done
echo "PostgreSQL is ready."

# Run schema migrations (env.py reads DAASH_ALEMBIC_DATABASE_URL / DAASH_DATABASE_URL)
echo "Running Alembic migrations..."
alembic -c /opt/daash/alembic.ini upgrade head
echo "Migrations complete."

# Start uvicorn
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
