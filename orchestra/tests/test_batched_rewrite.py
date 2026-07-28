"""Keyset batching contract for log_event field rewrites."""

from __future__ import annotations

from orchestra.db.batched_rewrite import DEFAULT_BATCH_SIZE, batched_rewrite


class FakeResult:
    def __init__(self, rows=None, rowcount=0):
        self._rows = rows or []
        self.rowcount = rowcount

    def fetchall(self):
        return self._rows


class FakeSession:
    """Session stub that serves ids from a table and records what ran."""

    def __init__(self, ids, batch_size=DEFAULT_BATCH_SIZE):
        self.remaining = list(ids)
        self.batch_size = batch_size
        self.applied: list[list[int]] = []
        self.commits = 0
        self.last_ids_seen: list[int] = []

    def execute(self, stmt, params=None):
        sql = str(stmt)
        params = params or {}
        if "SELECT" in sql.upper():
            last_id = params["last_id"]
            self.last_ids_seen.append(last_id)
            size = params["batch_size"]
            page = [i for i in self.remaining if i > last_id][:size]
            return FakeResult(rows=[(i,) for i in page])
        ids = params["ids"]
        self.applied.append(list(ids))
        # Applied rows stop matching, mirroring a self-consuming predicate.
        self.remaining = [i for i in self.remaining if i not in set(ids)]
        return FakeResult(rowcount=len(ids))

    def commit(self):
        self.commits += 1


ID_Q = "SELECT id FROM log_event WHERE id > :last_id ORDER BY id LIMIT :batch_size"
APPLY_Q = "UPDATE log_event SET data = data - :f WHERE id = ANY(:ids)"


def run(ids, **kw):
    session = FakeSession(ids, batch_size=kw.get("batch_size", DEFAULT_BATCH_SIZE))
    result = batched_rewrite(
        session,
        id_query=ID_Q,
        apply_query=APPLY_Q,
        params={"f": "x"},
        **kw,
    )
    return session, result


def test_empty_table_does_no_work():
    session, result = run([])
    assert result.rows == 0 and result.batches == 0
    assert session.applied == [] and session.commits == 0


def test_single_short_page_stops_without_a_second_probe():
    session, result = run([1, 2, 3], batch_size=10)
    assert session.applied == [[1, 2, 3]]
    assert result.rows == 3 and result.batches == 1


def test_walks_every_row_across_multiple_pages():
    ids = list(range(1, 26))
    session, result = run(ids, batch_size=10)
    assert [len(b) for b in session.applied] == [10, 10, 5]
    assert sorted(i for b in session.applied for i in b) == ids
    assert result.rows == 25 and result.batches == 3


def test_each_page_commits_so_progress_is_durable():
    session, _ = run(list(range(1, 26)), batch_size=10)
    assert session.commits == 3


def test_commit_false_leaves_the_caller_transaction_intact():
    session, _ = run(list(range(1, 26)), batch_size=10, commit=False)
    assert session.commits == 0
    assert sum(len(b) for b in session.applied) == 25


def test_paging_is_keyset_and_never_revisits_a_row():
    """Non-idempotent rewrites depend on each row being touched exactly once."""
    session, _ = run(list(range(1, 26)), batch_size=10)
    seen = [i for b in session.applied for i in b]
    assert len(seen) == len(set(seen))
    # last_id strictly advances: 0, then the tail of each applied page
    assert session.last_ids_seen == [0, 10, 20]


def test_exact_multiple_of_batch_size_terminates():
    session, result = run(list(range(1, 21)), batch_size=10)
    assert result.rows == 20
    assert [len(b) for b in session.applied] == [10, 10]


def test_on_batch_runs_per_page_with_that_pages_ids():
    session = FakeSession(list(range(1, 26)), batch_size=10)
    seen: list[list[int]] = []
    batched_rewrite(
        session,
        id_query=ID_Q,
        apply_query=APPLY_Q,
        params={"f": "x"},
        batch_size=10,
        on_batch=lambda ids: seen.append(list(ids)),
    )
    assert [len(s) for s in seen] == [10, 10, 5]
    assert seen == session.applied


def test_rows_reports_applied_rowcount_not_page_count():
    _, result = run(list(range(1, 26)), batch_size=10)
    assert result.rows == 25
    assert result.batches == 3
