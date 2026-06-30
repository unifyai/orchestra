"""Partition-pruning helper for the log filter/expression compiler.

``log_event`` is ``LIST (project_id)``-partitioned. The compiler builds many
internal subqueries that scan ``log_event`` filtered by id (the referenced
log-event ids). Without a ``project_id`` predicate those scans fan out across
every tenant's partition. ``project_id`` is threaded through every handler, so
each scan can (and must) additionally constrain it.

Use :func:`project_scope` as an extra ``WHERE`` term on any ``log_event`` scan;
it is a no-op (``TRUE``) when ``project_id`` is unknown, so callers may apply it
unconditionally.
"""

from __future__ import annotations

# Single source of truth lives in orchestra.db.log_queries; re-exported here so
# the compiler modules import their scoping helpers from one local place.
from orchestra.db.log_queries import embedding_scope, project_scope  # noqa: F401
