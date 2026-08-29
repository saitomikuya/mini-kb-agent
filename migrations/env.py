"""Alembic migration environment."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool

from app.config import get_settings
from app.db import Base, build_engine
import app.models  # noqa: F401  # Register models with Base.metadata.


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Alembic uses ConfigParser interpolation, so literal percent signs must be escaped.
config.set_main_option(
    "sqlalchemy.url",
    get_settings().database_url.replace("%", "%%"),
)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations without creating an Engine."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations using a short-lived migration connection."""
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    connectable = build_engine(settings, poolclass=pool.NullPool)

    try:
        with connectable.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
            )

            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
