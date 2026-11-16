"""DAASH 0.4.1: real embeddings for search (docs/DESIGN-0.4.1.md section 4)

* tasks, learnings, memories:
  * `embedding` loses its fixed dimension (vector(768) -> vector), so any model's vectors fit;
  * `embedding_model`: the model that made the vector (search compares a query only with rows of its
    own model);
  * `embedding_text`: the text the row is found by, so the embedder can re-embed it with another
    model.
  Until now the images had no model, so every stored vector was a word hash with no record of its
  text. They are cleared, the text is backfilled, and the embedder service re-embeds every row with
  the chosen model.
* embedding_status: one row with the embedder's state, shown in DAASHboard Settings.

The HNSW indexes are per model (partial expression indexes); the embedder service creates and
drops them as the model changes (section 4).

Revision ID: f0a3c0d0e007
Revises: f0a3c0d0e006
"""
from typing import Sequence, Union

from alembic import op

revision: str = "f0a3c0d0e007"
down_revision: Union[str, None] = "f0a3c0d0e006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ("tasks", "learnings", "memories")


def upgrade() -> None:
    for t in TABLES:
        op.execute(f"ALTER TABLE {t} ALTER COLUMN embedding TYPE vector USING NULL::vector")
        op.execute(f"ALTER TABLE {t} ADD COLUMN embedding_model text, ADD COLUMN embedding_text text")
    # app.embeddings.task_text: name, a newline, the description; surrounding whitespace trimmed
    op.execute("UPDATE tasks SET embedding_text = "
               "btrim(name || E'\\n' || coalesce(description, ''), E' \\n\\r\\t')")
    op.execute("UPDATE learnings SET embedding_text = content")
    op.execute("UPDATE memories SET embedding_text = content")
    op.execute("""CREATE TABLE embedding_status (
        id integer PRIMARY KEY CHECK (id = 1),
        chosen_model text,
        active_model text,
        dim integer,
        state text NOT NULL DEFAULT 'starting'
            CHECK (state IN ('starting', 'loading', 'indexing', 'ready', 'error')),
        indexed integer NOT NULL DEFAULT 0,
        total integer NOT NULL DEFAULT 0,
        error text,
        updated_at timestamptz NOT NULL DEFAULT now()
    )""")
    op.execute("INSERT INTO embedding_status (id) VALUES (1)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS embedding_status")
    for t in TABLES:
        op.execute(f"""DO $$ DECLARE r record; BEGIN
            FOR r IN SELECT indexname FROM pg_indexes
                     WHERE tablename = '{t}' AND indexname LIKE '{t}\\_emb\\_%' LOOP
                EXECUTE format('DROP INDEX IF EXISTS %I', r.indexname);
            END LOOP; END $$""")
        op.execute(f"ALTER TABLE {t} ALTER COLUMN embedding TYPE vector(768) USING NULL::vector(768)")
        op.execute(f"ALTER TABLE {t} DROP COLUMN IF EXISTS embedding_text, "
                   f"DROP COLUMN IF EXISTS embedding_model")
