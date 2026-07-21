"""Apply external field hydrate after log formatting."""

from __future__ import annotations

from typing import Any, Optional

from orchestra.db.dao.external_field_binding_dao import ExternalFieldBindingDAO
from orchestra.db.dao.log_event_dao import LogEventDAO
from orchestra.external_bindings.planner import HydrateMode, hydrate_logs


def apply_external_hydrate(
    *,
    session,
    project_id: int,
    context_id: int,
    logs_out: list[dict[str, Any]],
    hydrate: str = "stale_ok",
    hydrate_fields: Optional[list[str]] = None,
    materialize: bool = True,
    rows: Optional[list] = None,
) -> list[dict[str, Any]]:
    """Hydrate external_entry columns on ``logs_out``; optionally materialize.

    ``rows`` should be the raw ``(id, data, …)`` tuples from ``_get_logs_query``
    when available so sidecars and input columns are read from JSONB.
    """
    if hydrate in (None, "", "none") or not logs_out:
        for log in logs_out:
            log.setdefault("external_entries", {})
        return logs_out

    if context_id is None:
        for log in logs_out:
            log.setdefault("external_entries", {})
        return logs_out

    binding_dao = ExternalFieldBindingDAO(session)
    bindings = binding_dao.list_active(
        project_id=project_id,
        context_id=context_id,
        field_names=hydrate_fields,
    )
    if not bindings:
        for log in logs_out:
            log.setdefault("external_entries", {})
        return logs_out

    raw_data_by_id: dict[int, dict[str, Any]] = {}
    if rows:
        for row in rows:
            event_id = int(row[0])
            data = row[1] if len(row) > 1 else None
            if isinstance(data, dict):
                raw_data_by_id[event_id] = data

    try:
        mode = HydrateMode(hydrate)
    except ValueError as e:
        raise ValueError(
            f"Invalid hydrate mode '{hydrate}'. Use none|stale_ok|force.",
        ) from e

    hydrated, ops = hydrate_logs(
        logs_out,
        bindings=bindings,
        mode=mode,
        hydrate_fields=hydrate_fields,
        materialize=materialize,
        raw_data_by_id=raw_data_by_id or None,
        session=session,
        project_id=project_id,
        context_id=context_id,
    )
    if materialize and ops:
        LogEventDAO(session).bulk_merge_data(ops, project_id=project_id)
        session.commit()
    return hydrated
