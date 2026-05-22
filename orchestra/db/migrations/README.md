# Platform alembic chain (orchestra-platform)

The platform retains its full 259-revision history at the moment. It runs as
an **independent chain** from orchestra-core's: each is consumed by its own
`alembic upgrade head` against its own (fresh) database.

## Why two independent chains today

The kernel tables (project / context / log_event / embedding / ...) were
historically created by the platform's existing migrations. After the
phase-1 split they also exist in `orchestra-core`'s squashed initial
migration. Reconciling both into a single deps-on chain
(`platform_initial.depends_on=core_initial`) is a destructive operation
that requires prod-DB cutover planning (stamp the new merged head on every
deployed instance) and is therefore deferred to phase 3.

## Validation gates that hold today

- **Standalone orchestra-core**: empty Postgres → `alembic upgrade head`
  on `orchestra_core/db/migrations` creates the 13 kernel tables.
- **Standalone orchestra-platform**: empty Postgres →
  `alembic upgrade head` on this directory creates kernel + platform
  tables (the existing chain).

Running both chains on the same database is **not** a validation gate
in phase 1. Phase 3 will introduce a single squashed `_platform_initial`
revision with `down_revision="0001_core_initial"` plus the cutover script
that stamps existing deployed DBs.
