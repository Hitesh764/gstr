"""
Alembic migration environment.

The database URL is NOT stored in alembic.ini. It is read from the
application settings (backend/.env or real environment variables) so
there is exactly one source of truth and no credentials in version
control.

`render_as_batch` is enabled because SQLite cannot ALTER columns in
place; batch mode rebuilds the table instead. This matters for the
upcoming multi-tenancy migration, which adds firm_id to existing tables.
"""
import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from alembic import context

# Make the `app` package importable when alembic runs from backend/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings  # noqa: E402
from app.database import Base  # noqa: E402
from app import models  # noqa: F401,E402  (registers all tables on Base.metadata)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Inject the real URL from settings, overriding whatever alembic.ini holds.
config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata

IS_SQLITE = settings.database_url.startswith("sqlite")


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting to a database."""
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=IS_SQLITE,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=IS_SQLITE,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
