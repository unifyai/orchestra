# Platform alembic chain (orchestra-platform)

The platform's migration chain is now a **single squashed revision**:

- `_platform_initial` (`down_revision = "0001_core_initial"`) — creates
  every platform table outside the 13 kernel tables that orchestra-core's
  `0001_core_initial` already creates, plus the two `project` foreign keys
  back to `user` / `organization` that orchestra-core deliberately leaves
  off.

The two chains are now **formally converged**: a fresh database runs
core then platform sequentially with no `DuplicateTable` conflicts.
`alembic upgrade head` from the platform's `alembic.ini` discovers both
version directories (the platform's `versions/` plus orchestra-core's
installed `versions/` — `env.py` resolves the kernel path at import time
regardless of whether orchestra-core was installed via git URL or as a
local path dep).

## Production cutover

Existing production databases were stamped at one of the pre-squash
revisions (e.g. `phase3_core_bridge`). Those revision IDs no longer
exist in the chain, so a naive `alembic upgrade head` would fail with
`Can't locate revision identified by '...'`.

The migration job handles this transparently:
`orchestra/db/migrations/reconcile.py` runs **before every alembic
upgrade**, detects DBs stamped at any pre-squash revision, verifies the
expected schema is already in place (sentinel kernel + platform
tables), and stamps `alembic_version` forward to
`(0001_core_initial, _platform_initial)`. The reconcile is idempotent
and a no-op on fresh DBs and already-reconciled DBs.

## Schema source of truth

`_platform_initial.upgrade()` reads
[`_platform_initial_schema.sql`](versions/_platform_initial_schema.sql)
and executes it as a single multi-statement block. That file was
generated verbatim from a `pg_dump --schema-only` of a fresh database
that ran the historical 259-revision chain to its head, so every
constraint name, index, function (`safe_cast_to_*`), and partial-unique
definition matches production exactly.

If a future change needs to alter the platform schema, author a new
revision whose `down_revision = "_platform_initial"`. Do **not** edit
the squash file directly — it's a frozen recreation of the historical
schema.

## Re-generating the squash from scratch

If you ever need to regenerate `_platform_initial_schema.sql` (e.g. to
fold subsequent revisions into the squash), the procedure is:

1. Spin up a fresh Postgres + pgvector.
2. Apply the full migration chain on a copy of the platform repo from
   the relevant commit.
3. `pg_dump --schema-only --no-owner --no-comments` excluding kernel
   tables (`project`, `context`, `log_event`, ...), `_backup_orphan_*`
   tables, and `alembic_version`.
4. Strip pg_dump's `\restrict`, `SET ...`, and `Dumped by ...` preamble.
5. Replace `_platform_initial_schema.sql`.
