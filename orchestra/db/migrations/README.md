# Platform alembic chain (orchestra-platform)

The platform retains its full migration history. It runs as an
**independent chain** from orchestra-core's: each is consumed by its own
`alembic upgrade head` against its own (fresh) database.

The chain's current head is `phase3_core_bridge` — an inert marker
revision that documents the package-level dependency on orchestra-core
without forcing alembic-level convergence.

## Why two independent chains today

The kernel tables (project / context / log_event / embedding / ...)
are owned by orchestra-core's alembic chain (`0001_core_initial`). They
were also created by the platform chain back in revision
`2024-10-09-08-54_0fc27545b83d`, and every existing production database
has them in its history accordingly. Declaring an alembic-level
`depends_on = "0001_core_initial"` would make `upgrade head` fail with
`DuplicateTable` on every fresh database, because the second chain to
run would try to recreate kernel tables already created by the first.

True convergence requires squashing the existing 259-revision chain
into a single `_platform_initial` whose `down_revision =
"0001_core_initial"` and which creates **only** platform tables. That
is a destructive history rewrite plus a one-shot prod-DB cutover and is
deliberately scheduled as a follow-up PR.

## Validation gates that hold today

- **Standalone orchestra-core**: empty Postgres → `alembic upgrade head`
  on `orchestra_core/db/migrations` creates the 13 kernel tables.
- **Standalone orchestra-platform**: empty Postgres →
  `alembic upgrade head` on this directory creates kernel + platform
  tables (the existing chain) and reaches `phase3_core_bridge`.

## Future squash (separate PR)

The squash converges the two chains formally:

1. Author a new `2026-XX-XX_platform_initial.py` whose
   `down_revision = "0001_core_initial"`. Its `upgrade()` creates the
   ~56 platform-only tables (everything except the 13 kernel tables)
   plus the FK constraints from `project.user_id` → `user.id` and
   `project.organization_id` → `organization.id` that orchestra-core
   deliberately does not declare.
2. Delete the existing 259 revisions from `versions/` (they're
   replayable from `_platform_initial`).
3. Add `version_locations` in `alembic.ini` so the platform alembic env
   sees orchestra-core's installed migrations directory, allowing it to
   resolve `0001_core_initial` by name.
4. Ship a one-shot reconcile script that, for each existing production
   database, replaces its `alembic_version` row with
   `('0001_core_initial', '<new_platform_initial>')` once a sanity check
   confirms all 13 kernel tables exist with the expected schema.
5. After the squash + reconcile, `alembic upgrade head` runs cleanly
   on both fresh and existing databases against the merged chain.
