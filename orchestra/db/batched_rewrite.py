"""Keyset-batched rewrites over ``log_event`` rows.

``log_event.data`` is a single JSONB document per row, so anything that adds,
strips, renames or shifts a *field* is not a catalog operation — it rewrites
every matching row, with MVCC writing a new tuple version and the GIN index on
``data`` maintained for each one. Issued as one statement that cost is a single
long transaction, and past roughly a million rows it exceeds the API gateway's
~181s request ceiling: the connection is closed, the transaction rolls back,
and the whole rewrite is wasted.

``batched_rewrite`` runs the same work as a keyset walk instead — select a page
of ids, apply the statement to that page, commit, advance — so each transaction
is small, progress is durable, and total runtime is unbounded by any single
request deadline.

Two properties matter to callers:

* **Keyset, not predicate, paging.** Pages advance on ``id > :last_id`` and no
  row is visited twice, so a non-idempotent rewrite (``shift_numeric_field``
  offsets a value) stays correct. A predicate-driven loop would re-shift rows
  it had already shifted.
* **Progress is committed per page.** An interrupted run leaves earlier pages
  applied rather than rolling everything back. For self-consuming rewrites
  (strip/rename/backfill, whose predicates stop matching once applied) simply
  re-running finishes the job. For rewrites that are not self-consuming the
  caller must decide how to resume; see ``shift_numeric_field``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from sqlalchemy import text

logger = logging.getLogger(__name__)

# Large enough that per-batch round-trip overhead stays negligible, small enough
# that one batch is far inside any statement timeout even with GIN maintenance.
DEFAULT_BATCH_SIZE = 5000


@dataclass
class BatchedRewriteResult:
    """Rows touched and pages walked by a :func:`batched_rewrite` call."""

    rows: int = 0
    batches: int = 0


def batched_rewrite(
    session: Any,
    *,
    id_query: str,
    apply_query: str,
    params: Mapping[str, Any],
    batch_size: int = DEFAULT_BATCH_SIZE,
    commit: bool = True,
    on_batch: Callable[[Sequence[int]], None] | None = None,
) -> BatchedRewriteResult:
    """Apply ``apply_query`` to every row ``id_query`` selects, one page at a time.

    ``id_query`` must select a single ``id`` column, accept ``:last_id`` and
    ``:batch_size``, and order by id ascending — the keyset contract. It is
    re-executed per page, so its predicate should exclude rows the rewrite has
    already handled where that is possible.

    ``apply_query`` must accept ``:ids`` and restrict itself to them.

    ``on_batch`` runs after the page's ids are known and before they are
    rewritten, for side effects that must accompany the row change (deleting
    backing GCS media, say).

    Set ``commit=False`` when the caller owns the transaction; the walk then
    still bounds statement size but the whole run remains one transaction, and
    the gateway ceiling still applies.
    """
    result = BatchedRewriteResult()
    last_id = 0
    base = dict(params)

    while True:
        page = session.execute(
            text(id_query),
            {**base, "last_id": last_id, "batch_size": batch_size},
        ).fetchall()
        if not page:
            break

        ids = [row[0] for row in page]
        if on_batch is not None:
            on_batch(ids)

        applied = session.execute(text(apply_query), {**base, "ids": ids})
        result.rows += int(applied.rowcount or 0)
        result.batches += 1
        last_id = ids[-1]

        if commit:
            session.commit()

        if len(ids) < batch_size:
            break

    if result.batches > 1:
        logger.info(
            "batched rewrite touched %s rows over %s batches",
            result.rows,
            result.batches,
        )
    return result


__all__ = ["DEFAULT_BATCH_SIZE", "BatchedRewriteResult", "batched_rewrite"]
