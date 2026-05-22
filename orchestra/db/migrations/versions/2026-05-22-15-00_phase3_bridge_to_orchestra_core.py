"""Phase 3 bridge: declare orchestra-platform's intent to converge on orchestra-core.

This is an inert marker revision: it documents in code that the platform
alembic chain is logically downstream of orchestra-core's kernel chain
(`0001_core_initial`), without actually triggering convergence at upgrade
time.

Why no `depends_on`
-------------------
The existing 259-revision platform chain creates the kernel tables
(`project`, `context`, `log_event`, ...) itself, in revisions dating
back to 2024. orchestra-core's `0001_core_initial` also creates those
tables. If this revision declared `depends_on = "0001_core_initial"`,
alembic would force both chains to run on every fresh database, which
fails with `DuplicateTable` errors as soon as the second chain reaches
its first kernel-table-creating revision.

True convergence requires squashing the platform chain into a single
`_platform_initial` whose `down_revision = "0001_core_initial"` and
which creates ONLY platform tables. That squash is intentionally
deferred to a follow-up PR because:

  - It is a destructive change to the migration history.
  - Existing production databases must be reconciled with a one-shot
    `INSERT INTO alembic_version VALUES ('0001_core_initial')` cutover
    to acknowledge that their kernel tables logically belong to the
    kernel chain.

Until that follow-up lands:

  - Fresh databases: `alembic upgrade head` reaches this revision via
    the platform chain alone, exactly as before. The kernel tables it
    creates are not formally tracked by orchestra-core's chain.
  - Production databases: `alembic upgrade head` advances them to this
    revision unchanged.
  - This file's existence is the architectural commitment that the
    platform package depends on orchestra-core; new platform schema
    work that needs kernel tables can rely on those tables existing.

Revision ID: phase3_core_bridge
Revises: workspace_scoped_coordinators
Create Date: 2026-05-22 15:00:00.000000

"""

import logging

revision = "phase3_core_bridge"
down_revision = "workspace_scoped_coordinators"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    logger.info(
        "phase3_core_bridge: orchestra-platform tracks orchestra-core (v0.1.x) "
        "for kernel tables. True alembic-level convergence is a follow-up PR.",
    )


def downgrade() -> None:
    logger.info("phase3_core_bridge: removing orchestra-core convergence marker.")
