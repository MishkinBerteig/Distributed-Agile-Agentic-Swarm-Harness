"""Create database tables directly from SQLAlchemy models (no migrations).

Used by the container entrypoint while the project has zero Alembic
revisions. Once `alembic revision --autogenerate` produces an initial
migration, this module becomes redundant and can be removed.
"""

from sqlalchemy import create_engine, inspect

from app.config import Settings
from app.orm import Base  # noqa: F401  (imports all model classes onto Base.metadata)


def create_tables() -> None:
    settings = Settings()
    engine = create_engine(settings.DATABASE_URL)

    # pgvector type in the models requires the extension to exist first.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    tables_to_create = [t for t in Base.metadata.tables if t not in existing_tables]

    if tables_to_create:
        print(f"Creating tables: {tables_to_create}")
        Base.metadata.create_all(engine, checkfirst=True)
        print("Tables created from ORM models.")
    else:
        print("All tables already exist.")

    engine.dispose()


if __name__ == "__main__":
    create_tables()
