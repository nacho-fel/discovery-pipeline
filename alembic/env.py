# Alembic configuration and environment
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# Import all models to ensure they are registered
from discovery.config import get_settings
from discovery.db.models import *  # noqa: F403

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers defaults to True, which silently sets
    # `.disabled = True` on every logger already instantiated at this point
    # (e.g. any `discovery.*` module logger created at import time) that
    # isn't explicitly listed in alembic.ini's [loggers] section -- a
    # well-known fileConfig() gotcha. That would otherwise permanently
    # black-hole this application's own logging (silently: Logger.info()
    # returns early with no error) for the rest of the process the first
    # time any migration runs, well before this repo's own logging even
    # gets configured.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata  # noqa: F405


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    # The same settings source the application itself uses -- not a
    # separately-maintained DATABASE_URL fallback that can drift from it.
    url = get_settings().database_url
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    url = get_settings().database_url
    configuration = config.get_section(config.config_ini_section)
    configuration["sqlalchemy.url"] = url

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
