from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, create_engine

from orchestra.db.meta import meta
from orchestra.db.models import load_all_models
from orchestra.settings import settings

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config


load_all_models()
# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
target_metadata = meta

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    context.configure(
        url=str(settings.db_url),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """
    Run actual sync migrations.

    :param connection: connection to the database.
    """
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


def _wait_for_cloudsql_proxy(timeout: int = 30) -> None:
    """
    Block until the Cloud SQL Auth Proxy socket is ready.

    Cloud Run jobs start the command and proxy sidecar concurrently,
    so the socket may not exist yet when Alembic begins.
    """
    import os
    import time

    socket_dir = f"/cloudsql/{settings.cloud_sql_instance}"
    if not os.path.isdir("/cloudsql"):
        return
    for i in range(timeout):
        if os.path.isdir(socket_dir):
            return
        time.sleep(1)
    raise RuntimeError(
        f"Cloud SQL Auth Proxy socket not ready at {socket_dir} "
        f"after {timeout}s. Check that ORCHESTRA_CLOUD_SQL_INSTANCE "
        f"matches the instance configured on the Cloud Run job.",
    )


def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.
    """
    _wait_for_cloudsql_proxy()
    connectable = create_engine(str(settings.db_url))

    with connectable.connect() as connection:
        do_run_migrations(connection)


if context.is_offline_mode():
    task = run_migrations_offline
else:
    task = run_migrations_online
task()
