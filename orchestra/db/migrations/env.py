from logging.config import fileConfig
from pathlib import Path

import orchestra_core.db.migrations as _core_migrations_pkg
from alembic import context
from alembic.script.revision import RevisionMap
from orchestra_core.db.meta import meta
from sqlalchemy import Connection, create_engine

from orchestra.db.migrations.reconcile import reconcile_to_new_chain
from orchestra.db.models import load_all_models
from orchestra.settings import settings

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Make orchestra-core's `0001_core_initial` revision discoverable so the
# platform's `_platform_initial` (down_revision="0001_core_initial") can
# resolve its parent. orchestra-core ships its migrations alongside its
# package, so we resolve the path at import time regardless of whether
# the kernel was installed from a git URL or as a local path dep.
#
# Alembic builds its `ScriptDirectory` from `alembic.ini`'s
# `version_locations` BEFORE env.py runs, so `set_main_option` from here
# is too late. We mutate the already-constructed `ScriptDirectory` on
# the active context directly and invalidate its memoized `revision_map`
# so the kernel directory is discovered before alembic walks revisions.
# orchestra_core.db.migrations is a namespace package (no __init__.py),
# so __file__ is None — resolve the directory via __path__ instead.
_core_versions = str(Path(next(iter(_core_migrations_pkg.__path__))) / "versions")
_platform_versions = str(Path(__file__).parent / "versions")
_active_script = context.script
_existing = [str(p) for p in _active_script.version_locations or []]
_active_script.version_locations = [_platform_versions, _core_versions] + [
    p for p in _existing if p not in (_platform_versions, _core_versions)
]
# revision_map is set in ScriptDirectory.__init__ as a regular attribute
# (it captures `self._load_revisions` as a closure that reads
# `self.version_locations` lazily). Replacing the map instance here
# forces alembic to re-walk the freshly-extended version locations.
_active_script.revision_map = RevisionMap(_active_script._load_revisions)

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
        # One-shot stamp-forward for DBs upgraded under the pre-squash
        # 259-revision platform chain. Idempotent + verifies the
        # post-upgrade schema is present before mutating alembic_version.
        reconcile_to_new_chain(connection, _active_script)
        connection.commit()
        do_run_migrations(connection)


if context.is_offline_mode():
    task = run_migrations_offline
else:
    task = run_migrations_online
task()
