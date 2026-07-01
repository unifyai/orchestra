#!/usr/bin/env python3
"""Out-of-band cleanup of the redundant base ``Events`` mirror rows.

Unity's EventBus used to write every event twice: once to the base context
``{user}/{assistant}/Events`` (as a ``payload_json`` blob) and once to the
per-type context ``{user}/{assistant}/Events/{Type}`` (payload fields spread
into columns). The runtime and Console now read only the per-type copy, so the
base copies are pure duplication -- roughly half of a user's event rows, and
they inflate the per-owner counts that drive partition promotion. This job
deletes them.

It is deliberately a **standalone, out-of-band** job rather than a deploy
migration: the deletion touches real data volume and must not block deploys or
race the migrator timeout. It is driven from the (small) set of base ``Events``
contexts, batched by ``context_id`` + log id, runs under AUTOCOMMIT (each batch
commits), and is idempotent + resumable -- re-run it freely; it only ever finds
fewer remaining rows. The base ``context`` row itself is retained (EventBus
recreates it idempotently on next init).

Examples
--------
    # Count what would be deleted (no mutations), against prod via the
    # Cloud SQL Auth Proxy on localhost:5433
    python -m scripts.delete_events_mirror \
        --db-url 'postgresql+psycopg2://orchestra:PASS@127.0.0.1:5433/orchestra' \
        --dry-run

    # Perform the deletion, then reclaim the bloat left by the DELETEs
    python -m scripts.delete_events_mirror --db-url '...' --vacuum
"""

from __future__ import annotations

import argparse
import logging
import time

from sqlalchemy import create_engine, text

from orchestra.db.scope import delete_events_mirror_logs
from orchestra.settings import settings

_HEAVY_TABLES = ("log_event", "log_event_context")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-url", default=None, help="Override settings.db_url.")
    parser.add_argument(
        "--batch",
        type=int,
        default=1000,
        help="Log rows deleted per committed batch (default 1000).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only count the base Events mirror rows that would be deleted; "
        "make no changes.",
    )
    parser.add_argument(
        "--vacuum",
        action="store_true",
        help="VACUUM (ANALYZE) the heavy tables after deleting to reclaim the "
        "dead tuples left by the DELETEs.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger("delete_events_mirror")

    db_url = args.db_url or str(settings.db_url)
    log.info("connecting to db=%s", db_url.split("@")[-1])
    # AUTOCOMMIT so each batched DELETE commits independently (resumable, bounded
    # locks); VACUUM also requires running outside a transaction.
    engine = create_engine(db_url, isolation_level="AUTOCOMMIT", pool_pre_ping=True)

    start = time.monotonic()
    with engine.connect() as conn:
        removed = delete_events_mirror_logs(
            conn,
            batch=args.batch,
            dry_run=args.dry_run,
        )
    verb = "would remove" if args.dry_run else "removed"
    log.info(
        "%s %s base Events mirror log rows in %.1fs",
        verb,
        removed,
        time.monotonic() - start,
    )

    if args.vacuum and not args.dry_run:
        with engine.connect() as conn:
            for table in _HEAVY_TABLES:
                log.info("VACUUM (ANALYZE) %s ...", table)
                t0 = time.monotonic()
                conn.execute(text(f'VACUUM (ANALYZE) "{table}"'))
                log.info("  done in %.1fs", time.monotonic() - t0)

    engine.dispose()
    log.info("all done")


if __name__ == "__main__":
    main()
