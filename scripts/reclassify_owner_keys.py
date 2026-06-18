#!/usr/bin/env python3
"""Out-of-band corrective backfill for heavy-table ``owner_key``.

The ``heavy_owner_key`` migration added ``owner_key`` with ``DEFAULT 'sys'``, so
every pre-existing ``log_event`` / ``log_event_context`` / ``embedding`` row was
stamped ``'sys'`` and the original NULL-gated backfill never reclassified them.
Owner-scoped deletion (``purge_owner`` / ``drop_owner``) therefore leaves an
assistant/team's historical rows orphaned. This job recomputes ``owner_key``
from each log's owning (assistant/team) context for rows still labelled
``'sys'`` (see :func:`orchestra.db.scope.reclassify_heavy_owner_keys`).

It is deliberately a **standalone, out-of-band** job rather than a deploy
migration: the correction touches real data volume and must not block deploys or
race the migrator timeout. It is driven from the (small) set of assistant/team
contexts, batched by context id, runs under AUTOCOMMIT (each batch commits), and
is idempotent + resumable -- re-run it freely; it only touches rows still
mislabelled ``'sys'``.

Examples
--------
    # Against prod via the Cloud SQL Auth Proxy on localhost:5433
    python -m scripts.reclassify_owner_keys \
        --db-url 'postgresql+psycopg2://orchestra:PASS@127.0.0.1:5433/orchestra'

    # Resume (safe to re-run), then reclaim bloat from the UPDATEs
    python -m scripts.reclassify_owner_keys --db-url '...' --vacuum
"""

from __future__ import annotations

import argparse
import logging
import time

from sqlalchemy import create_engine, text

from orchestra.db.scope import reclassify_heavy_owner_keys
from orchestra.settings import settings

_HEAVY_TABLES = ("log_event", "log_event_context", "embedding")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-url", default=None, help="Override settings.db_url.")
    parser.add_argument(
        "--batch",
        type=int,
        default=1000,
        help="Owner contexts processed per committed batch (default 1000).",
    )
    parser.add_argument(
        "--vacuum",
        action="store_true",
        help="VACUUM (ANALYZE) the heavy tables after reclassifying to reclaim "
        "dead tuples left by the owner_key UPDATEs.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger("reclassify_owner_keys")

    db_url = args.db_url or str(settings.db_url)
    log.info("connecting to db=%s", db_url.split("@")[-1])
    # AUTOCOMMIT so each batched UPDATE commits independently (resumable, bounded
    # locks); VACUUM also requires running outside a transaction.
    engine = create_engine(db_url, isolation_level="AUTOCOMMIT", pool_pre_ping=True)

    start = time.monotonic()
    with engine.connect() as conn:
        reclassify_heavy_owner_keys(conn, batch=args.batch)
    log.info("reclassification complete in %.1fs", time.monotonic() - start)

    if args.vacuum:
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
