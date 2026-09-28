#!/usr/bin/env bash
set -e

# Wait for PostgreSQL to be ready
echo "Waiting for PostgreSQL to be ready..."
DA_HOST="${DAASH_DATABASE_HOST:-db}"
until pg_isready -h "$DA_HOST" -p 5432 -U daash; do
  sleep 1
done
echo "PostgreSQL is ready."

# Install pgvector extension if not already present
PGPASSWORD=daash psql -h "$DA_HOST" -U daash -d daash -c "CREATE EXTENSION IF NOT EXISTS vector;" || true

# Set the correct database URL for Alembic (from env var)
if [ -n "$DAASH_ALEMBIC_DATABASE_URL" ]; then
  python3 -c "
import pathlib
p = pathlib.Path('/opt/daash/alembic.ini')
content = p.read_text()
# Replace the sqlalchemy.url line
content = content.replace('sqlalchemy.url = postgresql+psycopg://daash:daash@localhost/daash',
                          'sqlalchemy.url = ' + '$DAASH_ALEMBIC_DATABASE_URL')
p.write_text(content)
"
fi

# Create or migrate database
MIGRATION_COUNT=$(ls -1 /opt/daash/migrations/versions/*.py 2>/dev/null | wc -l | tr -d ' ')

if [ "$MIGRATION_COUNT" -gt 0 ]; then
  echo "Running Alembic migrations ($MIGRATION_COUNT revision(s))..."
  alembic -c /opt/daash/alembic.ini upgrade head
  echo "Migrations complete."
else
  echo "No migration files — creating tables from ORM models..."
  python3 -m app.db_setup
fi

# Start uvicorn
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
