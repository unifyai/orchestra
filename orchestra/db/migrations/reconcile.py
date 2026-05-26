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

from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import Connection

logger = logging.getLogger("alembic.reconcile")

# Where the reconcile stamps a pre-squash DB. This is the revision whose
# schema matches what production was running just before tonight's
# convergence work — i.e. the schema produced by the historical 259
# platform migrations. Stamping at this point lets alembic naturally
# apply the kernel + platform drift-fix migrations on top, doing the
# actual schema convergence as proper migrations rather than as a
# silent stamp-only operation.
RECONCILE_STAMP_TARGET = "_platform_initial"

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


def reconcile_to_new_chain(
    connection: Connection,
    script_dir: ScriptDirectory,
) -> None:
    """Stamp `alembic_version` to the new chain's heads if needed.

    The set of "known post-squash revisions" is derived from the active
    `ScriptDirectory` so the reconcile auto-tracks every migration added
    after the squash — no manual list to keep in sync.
    """
    av_exists = connection.execute(
        text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'alembic_version'",
        ),
    ).first()
    if not av_exists:
        # Fresh DB; alembic will create alembic_version itself when it
        # applies the new chain. Nothing to reconcile.
        return

    rows = connection.execute(
        text("SELECT version_num FROM alembic_version"),
    ).fetchall()
    versions = {r[0] for r in rows}

    if not versions:
        # alembic_version exists but is empty — nothing to reconcile,
        # alembic will INSERT the new heads as it applies the chain.
        return

    new_chain_revisions = {rev.revision for rev in script_dir.walk_revisions()}
    if versions <= new_chain_revisions:
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
                "WHERE table_schema = 'public' AND table_name = :t",
            ),
            {"t": table},
        ).first()
        if not present:
            missing.append(table)
    if missing:
        raise RuntimeError(
            f"Cannot reconcile alembic_version: required tables are missing: {missing}. "
            f"Current alembic_version contents: {sorted(versions)}. "
            "This DB is in an unexpected pre-squash state and needs manual triage.",
        )

    logger.warning(
        "alembic_version reconcile: stamping forward from %s to %s; "
        "alembic will then apply post-squash drift-fix migrations on top",
        sorted(versions),
        [RECONCILE_STAMP_TARGET],
    )
    connection.execute(text("DELETE FROM alembic_version"))
    connection.execute(
        text("INSERT INTO alembic_version (version_num) VALUES (:v)"),
        {"v": RECONCILE_STAMP_TARGET},
    )
