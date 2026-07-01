"""Tests for the production partition-prune monitor routine."""

from __future__ import annotations

from orchestra.routines.partition_prune_monitor import (
    _query_prunes,
    run_partition_prune_monitor,
)


def test_query_prunes_detects_partition_predicate() -> None:
    # Pruned: a literal project_id predicate on the partitioned table.
    assert _query_prunes("SELECT le.id FROM log_event le WHERE le.project_id = 5")
    assert _query_prunes(
        "SELECT id FROM log_event WHERE project_id = 5 AND id = ANY(ARRAY[1,2])",
    )
    assert _query_prunes(
        "SELECT log_event.created_at FROM log_event JOIN log_event_context "
        "ON log_event_context.log_event_id = log_event.id "
        "WHERE log_event.project_id = 5 AND log_event_context.project_id = 5",
    )
    assert _query_prunes(
        "SELECT ref_id FROM embedding WHERE embedding.project_id = 5 "
        "AND ref_id = ANY(ARRAY[1])",
    )


def test_query_prunes_flags_unpruned_shapes() -> None:
    # Only context.project_id (joined, non-partitioned table) -> log_event fans out.
    assert not _query_prunes(
        "SELECT log_event.id FROM log_event "
        "JOIN log_event_context ON log_event_context.log_event_id = log_event.id "
        "JOIN context ON context.id = log_event_context.context_id "
        "WHERE context.project_id = 5 AND context.name IN ('x')",
    )
    # context_id only, no project_id anywhere.
    assert not _query_prunes(
        "SELECT log_event.created_at FROM log_event "
        "JOIN log_event_context ON log_event_context.log_event_id = log_event.id "
        "WHERE log_event_context.context_id = 5",
    )
    # embedding scanned by ref_id only.
    assert not _query_prunes(
        "SELECT vector FROM embedding WHERE ref_id = ANY(ARRAY[1,2]) AND key = 'x'",
    )


def test_monitor_runs_read_only(dbsession) -> None:
    """The routine returns a structured summary and never raises, whether or not
    pg_stat_statements is installed in the test database."""
    result = run_partition_prune_monitor(session=dbsession)
    assert "available" in result
    assert "offenders" in result
    assert isinstance(result["offenders"], list)
