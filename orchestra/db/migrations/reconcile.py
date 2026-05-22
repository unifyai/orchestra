"""One-shot, idempotent reconcile of `alembic_version` for the platform-chain squash.

Background
----------
Phase-3 of the orchestra-core split squashed the platform's 259-revision
alembic chain into a single `_platform_initial` revision whose
`down_revision` is orchestra-core's `0001_core_initial`. Existing
production databases are stamped at one of the *old* revisions
(e.g. `phase3_core_bridge`); the new chain doesn't know about those
names, so a naive `alembic upgrade head` fails with
`Can't locate revision identified by '...'`.

This function detects that situation and stamps the database forward to
the new chain's heads — but ONLY after verifying that the schema is
already at the expected post-upgrade state (i.e. the kernel + platform
tables exist). It is a NO-OP on:

- Fresh databases (no `alembic_version` table yet — alembic will create
  it normally as it applies the new chain).
- Databases already stamped at the new heads.
- Databases at a pre-squash revision but missing expected tables (raises;
  refuses to silently corrupt state).

The platform's `env.py` invokes this before every alembic run, so the
reconcile is automatic, idempotent, and self-applying. A non-deploy
context (e.g. a developer's local DB) gets the same treatment for free.
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.engine import Connection

logger = logging.getLogger("alembic.reconcile")

# Every revision that exists in the new (post-squash) chain. The
# reconcile is a no-op if alembic_version contents are a subset of these.
# `_platform_initial.down_revision = "0001_core_initial"`, so alembic
# treats them as a linear chain — `alembic_version` typically only stores
# the leaf, but a fresh DB stamped at `0001_core_initial` mid-upgrade is
# also a valid state and should not be force-stamped forward.
NEW_CHAIN_HEAD = "_platform_initial"
NEW_CHAIN_REVISIONS = frozenset({"0001_core_initial", NEW_CHAIN_HEAD})

# A handful of tables we expect every post-upgrade DB to have. If any of
# these are missing we abort rather than blindly stamping forward.
SENTINEL_TABLES = (
    # Kernel
    "project",
    "context",
    "log_event",
    "context_counter",
    "field_type",
    # Platform
    "user",
    "billing_account",
    "organization",
    "api_key",
)


def reconcile_to_new_chain(connection: Connection) -> None:
    """Stamp `alembic_version` to the new chain's heads if needed."""
    av_exists = connection.execute(
        text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'alembic_version'"
        ),
    ).first()
    if not av_exists:
        # Fresh DB; alembic will create alembic_version itself when it
        # applies the new chain. Nothing to reconcile.
        return

    rows = connection.execute(
        text("SELECT version_num FROM alembic_version")
    ).fetchall()
    versions = {r[0] for r in rows}

    if not versions:
        # alembic_version exists but is empty — nothing to reconcile,
        # alembic will INSERT the new heads as it applies the chain.
        return

    if versions <= NEW_CHAIN_REVISIONS:
        # Already on (or partway up) the new chain — let alembic handle
        # any remaining upgrades the normal way.
        return

    # Anything else means we're at a pre-squash revision. Verify schema
    # is in the expected post-upgrade shape before stamping.
    missing = []
    for table in SENTINEL_TABLES:
        present = connection.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = :t"
            ),
            {"t": table},
        ).first()
        if not present:
            missing.append(table)
    if missing:
        raise RuntimeError(
            f"Cannot reconcile alembic_version: required tables are missing: {missing}. "
            f"Current alembic_version contents: {sorted(versions)}. "
            "This DB is in an unexpected pre-squash state and needs manual triage."
        )

    logger.warning(
        "alembic_version reconcile: stamping forward from %s to %s",
        sorted(versions),
        [NEW_CHAIN_HEAD],
    )
    connection.execute(text("DELETE FROM alembic_version"))
    connection.execute(
        text("INSERT INTO alembic_version (version_num) VALUES (:v)"),
        {"v": NEW_CHAIN_HEAD},
    )
