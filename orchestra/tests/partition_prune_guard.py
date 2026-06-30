"""Test-time guard that catches log queries which fail to prune by ``project_id``.

``log_event`` / ``log_event_context`` / ``embedding`` / ``embedding_queue`` are
``LIST (project_id)``-partitioned. A query that does not constrain ``project_id``
to a literal cannot be pruned and fans out across every tenant's partition --
the root cause of the June 2026 production slowdown. Grep can only find one
query shape; this finds *all* of them (ORM, Core, raw ``text()``) by asking the
planner directly.

How it works -- a sentinel "tracer" partition:

* We create a dedicated partition for :data:`SENTINEL_PROJECT_ID`, a project id
  that no test data or query ever references.
* The planner prunes that partition out of any query that constrains
  ``project_id`` (to a literal or an ``IN`` list -- the sentinel id is never in
  one). So a correctly-scoped query's plan *never* mentions it.
* Therefore, if a statement's ``EXPLAIN`` plan references the sentinel
  partition, the statement provably failed to prune by ``project_id``. No false
  positives from legitimate multi-project / admin queries (they still exclude
  the sentinel project), and it is agnostic to how the SQL was built.

Enable with ``PARTITION_PRUNE_GUARD=warn`` (collect + report) or ``=strict``
(also fail the session). Disabled by default so normal runs pay nothing.
"""

from __future__ import annotations

import json
import os
import re
import threading
import traceback
from collections import OrderedDict

from sqlalchemy import event
from sqlalchemy.engine import Engine

from orchestra.db.partitioning import PARTITIONED_TABLES

# A project_id far outside any real/test range; gets a dedicated "tracer"
# partition per table that only an unpruned query can reach.
SENTINEL_PROJECT_ID = 2_147_483_646
_SENTINEL_TOKEN = f"p{SENTINEL_PROJECT_ID}"

# Match the partitioned PARENT tables as whole words. ``\bembedding\b`` does not
# match ``embedding_queue`` / ``artifact_embedding`` (``_`` is a word char), so
# the queue is listed explicitly; direct-leaf references (``log_event_p123``)
# likewise don't match the parent word and are correctly ignored.
_TABLE_RE = re.compile(
    r"\b(log_event_context|log_event|embedding_queue|embedding)\b",
    re.IGNORECASE,
)
_DML_RE = re.compile(r"^\s*(?:select|with|update|delete|insert)\b", re.IGNORECASE)

# Call sites that are *intentionally* partition-spanning: project_id is genuinely
# NOT known (a bare log_event id is being mapped to its project/owner), a
# cross-partition move, or a deliberate global/admin scan. Each entry is matched
# against a normalized "<relpath>::<func>" frame (line number stripped), so it
# pins a specific function. Every entry is justified inline.
_ALLOWLIST: tuple[str, ...] = (
    # id -> metadata / project mappers (the id is all we have).
    "db/dao/log_event_dao.py::get_ts",
    "db/dao/log_event_dao.py::get_user_id",
    "db/dao/log_event_dao.py::get_user_and_project_id",
    "db/dao/log_event_dao.py::get_user_and_project_ids_batch",
    # Resolves project_id FROM ids before scoped work; and bare-id point lookups.
    "db/dao/log_event_dao.py::delete",
    "db/dao/log_event_dao.py::update",
    "db/dao/log_event_dao.py::filter",
    # Cross-partition move of rows by id (old project spans partitions by design).
    "db/dao/log_event_dao.py::reproject_logs",
    # id -> owner_key, and a context-batched maintenance backfill.
    "db/scope.py::owner_key_for_log",
    "db/scope.py::reclassify_heavy_owner_keys",
    # Auth gate: bare id -> project_id, then access check.
    "web/api/log/views.py::_atomic_field_update_impl",
    # Deliberate admin global contact search across all projects.
    "web/api/assistant/views.py::admin_list_contacts",
)

_lock = threading.Lock()
_seen: set[str] = set()
violations: "OrderedDict[str, dict]" = OrderedDict()
_stats = {"analyzed": 0, "explain_errors": 0}
_active = threading.local()


def enabled() -> bool:
    return os.environ.get("PARTITION_PRUNE_GUARD", "").lower() in {
        "1",
        "true",
        "warn",
        "strict",
    }


def strict() -> bool:
    return os.environ.get("PARTITION_PRUNE_GUARD", "").lower() == "strict"


def ensure_sentinel_partitions(conn) -> None:
    """Create the sentinel partition for every partitioned table (idempotent)."""
    from orchestra.db.partitioning import create_dedicated_partition

    for table in PARTITIONED_TABLES:
        create_dedicated_partition(conn, table, SENTINEL_PROJECT_ID)


def _callsite() -> list[str]:
    frames: list[str] = []
    for frame in reversed(traceback.extract_stack()[:-2]):
        path = frame.filename
        if (
            "/orchestra/orchestra/" not in path
            or "/.venv/" in path
            or "/site-packages/" in path
            or "/tests/" in path
            or "partition_prune_guard" in path
        ):
            continue
        rel = "orchestra/" + path.split("/orchestra/orchestra/", 1)[-1]
        frames.append(f"{rel}:{frame.lineno} {frame.name}")
        if len(frames) >= 5:
            break
    return frames


def _allowlisted(callsite: list[str]) -> bool:
    # Normalize each frame "<relpath>:<lineno> <func>" -> "<relpath>::<func>" so
    # allowlist entries pin a function without depending on line numbers.
    normalized = " | ".join(re.sub(r":\d+ ", "::", frame) for frame in callsite)
    return any(entry in normalized for entry in _ALLOWLIST)


def _explain(engine, statement: str, parameters) -> str | None:
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        try:
            cur.execute("SET statement_timeout = '5s'")
            cur.execute("EXPLAIN " + statement, parameters or None)
            return "\n".join(row[0] for row in cur.fetchall())
        finally:
            cur.close()
    finally:
        # EXPLAIN (no ANALYZE) writes nothing; roll back to release any locks and
        # return the connection to the pool.
        try:
            raw.rollback()
        finally:
            raw.close()


@event.listens_for(Engine, "before_cursor_execute")
def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
    if not enabled() or executemany or getattr(_active, "busy", False):
        return
    if not isinstance(statement, str):
        return
    if _SENTINEL_TOKEN in statement:
        return  # our own sentinel DDL / a query already targeting the sentinel
    if not _DML_RE.match(statement) or not _TABLE_RE.search(statement):
        return
    norm = re.sub(r"\s+", " ", statement).strip()
    with _lock:
        if norm in _seen:
            return
        _seen.add(norm)
    _active.busy = True
    try:
        plan = _explain(conn.engine, statement, parameters)
        with _lock:
            _stats["analyzed"] += 1
    except Exception:
        plan = None  # uncommitted/test-only relation, lock, or unparsable -> skip
        with _lock:
            _stats["explain_errors"] += 1
    finally:
        _active.busy = False
    if not plan or _SENTINEL_TOKEN not in plan:
        return
    callsite = _callsite()
    if _allowlisted(callsite):
        return
    with _lock:
        violations[norm] = {"sql": norm[:20000], "callsite": callsite}


def report() -> int:
    """Write this worker's violations to disk and print a summary. Returns count."""
    if not enabled():
        return 0
    out_dir = os.environ.get("PARTITION_PRUNE_GUARD_DIR", "/tmp/partition_prune_guard")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"violations_{os.getpid()}.jsonl")
    with open(path, "w") as fh:
        for entry in violations.values():
            fh.write(json.dumps(entry) + "\n")
    print(
        f"\n[partition-prune-guard] analyzed={_stats['analyzed']} "
        f"explain_errors={_stats['explain_errors']} "
        f"violations={len(violations)} -> {path}",
        flush=True,
    )
    for entry in violations.values():
        print(f"  UNPRUNED: {entry['sql'][:160]}", flush=True)
        for frame in entry["callsite"][:3]:
            print(f"      at {frame}", flush=True)
    return len(violations)


def aggregate() -> int:
    """Controller-side union of every worker's violation file.

    Under ``pytest-xdist`` each worker executes the queries and writes its own
    ``violations_<pid>.jsonl`` from :func:`report`; the controller process never
    runs them, so its own ``violations`` dict is empty. This reads all the files
    back, dedupes by SQL, prints a summary, and returns the distinct count so the
    controller's ``pytest_sessionfinish`` can fail the session in strict mode.
    """
    if not enabled():
        return 0
    out_dir = os.environ.get("PARTITION_PRUNE_GUARD_DIR", "/tmp/partition_prune_guard")
    seen: dict[str, dict] = {}
    try:
        names = os.listdir(out_dir)
    except FileNotFoundError:
        names = []
    for name in names:
        if not (name.startswith("violations_") and name.endswith(".jsonl")):
            continue
        with open(os.path.join(out_dir, name)) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    entry = json.loads(line)
                    seen[entry["sql"]] = entry
    if seen:
        print(
            "\n[partition-prune-guard] STRICT FAILURE: "
            f"{len(seen)} distinct unpruned log queries across all workers. "
            "Add a literal project_id predicate (see orchestra/db/log_queries.py) "
            "or, if genuinely cross-partition, add the call site to _ALLOWLIST.",
            flush=True,
        )
        for entry in list(seen.values())[:40]:
            print(f"  UNPRUNED: {entry['sql'][:160]}", flush=True)
            for frame in entry["callsite"][:3]:
                print(f"      at {frame}", flush=True)
    return len(seen)
