"""Federated (multi-context) log reads.

One request reads several contexts — an assistant's personal root plus its
``Teams/{id}/…`` roots, the builtins catalogue, etc. — and returns a single
globally-ordered window, exactly as though every row lived in one context.

Why per-branch execution + a server-side merge, rather than one SQL UNION:
field types are **per-context** in this data model, so the same filter or
sort expression legitimately compiles to different SQL per context. Each
branch therefore runs through the ordinary single-context pipeline
(:func:`_get_logs_query` — per-context field types, owner-key partition
pruning, ANN fast-paths) and the exact merge happens here. The window is
exact for the same reason the client-side equivalent was: with every branch
fetched to ``offset + limit`` rows under backend ordering (NULLs last), a
row outside its branch window cannot enter the global window.

This endpoint replaces the fan-out that clients (the unify runtime's
``federated_search`` helper, Console's read-across-roots fetches) previously
did over N× paged HTTP calls.
"""

from __future__ import annotations

from functools import cmp_to_key
from typing import Any, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.exc import DataError, SQLAlchemyError

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.field_type_dao import FieldTypeDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dependencies import get_db_session
from orchestra.web.api.log.utils.logging_utils import _format_logs, _get_logs_query
from orchestra.web.api.log.views import _sanitize_sql_error

router = APIRouter()

SOURCE_FIELD = "_federated_source"
CONTEXT_FIELD = "_federated_context"

_MAX_CONTEXTS = 32
_MAX_LIMIT = 1000


class FederatedContextSpec(BaseModel):
    """One context participating in a federated read."""

    context: str = Field(description="Context name to read from.")
    source: Optional[str] = Field(
        default=None,
        description="Label stamped on rows from this context "
        "(defaults to the context name).",
    )
    filter: Optional[str] = Field(
        default=None,
        description="Per-context filter expression, ANDed with the shared filter.",
    )
    from_fields: Optional[List[str]] = Field(
        default=None,
        description="Restrict returned fields for this context.",
    )
    exclude_fields: Optional[List[str]] = Field(
        default=None,
        description="Fields withheld from this context's rows "
        "(mutually exclusive with from_fields).",
    )
    project_name: Optional[str] = Field(
        default=None,
        description="Project owning this context (defaults to the request's "
        "project_name); enables cross-project reads such as the public "
        "builtins catalogue.",
    )


class FederatedSortSpec(BaseModel):
    """One global sort key.

    ``missing`` controls where rows lacking the field are placed. The
    backend orders NULLs last, so ``missing='last'`` permits exact windowed
    branch fetches while ``missing='first'`` forces full branch fetches.
    """

    field: str
    direction: Literal["ascending", "descending"] = "ascending"
    missing: Literal["first", "last"] = "last"


class FederatedLogsRequest(BaseModel):
    """Request body for one federated read."""

    project_name: str = Field(description="Default project for context specs.")
    contexts: List[FederatedContextSpec] = Field(min_length=1, max_length=_MAX_CONTEXTS)
    filter: Optional[str] = Field(
        default=None,
        description="Shared filter expression applied to every context.",
    )
    sorting: List[FederatedSortSpec] = Field(default_factory=list)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=0, le=_MAX_LIMIT)
    unique_id_field: Optional[str] = Field(
        default=None,
        description="Deduplicate merged rows on this field, keeping the "
        "first instance in merge order.",
    )
    annotate: bool = Field(
        default=True,
        description=f"Stamp rows with {SOURCE_FIELD!r}/{CONTEXT_FIELD!r}.",
    )
    value_limit: Optional[int] = Field(
        default=None,
        ge=1,
        description="Maximum characters returned for string values.",
    )


def _combine_filters(left: Optional[str], right: Optional[str]) -> Optional[str]:
    parts = [part for part in (left, right) if part]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return " and ".join(f"({part})" for part in parts)


def _compare_present_values(left: object, right: object) -> int:
    try:
        if left < right:  # type: ignore[operator]
            return -1
        if left > right:  # type: ignore[operator]
            return 1
        return 0
    except TypeError:
        left_repr = repr(left)
        right_repr = repr(right)
        if left_repr < right_repr:
            return -1
        if left_repr > right_repr:
            return 1
        return 0


def _compare_by_sorting(
    left: dict,
    right: dict,
    sorting: List[FederatedSortSpec],
) -> int:
    for spec in sorting:
        left_entries = left.get("entries") or {}
        right_entries = right.get("entries") or {}
        left_missing = spec.field not in left_entries or (
            left_entries.get(spec.field) is None
        )
        right_missing = spec.field not in right_entries or (
            right_entries.get(spec.field) is None
        )
        if left_missing or right_missing:
            if left_missing and right_missing:
                continue
            left_first = spec.missing == "first"
            return -1 if (left_missing == left_first) else 1
        cmp = _compare_present_values(
            left_entries.get(spec.field),
            right_entries.get(spec.field),
        )
        if cmp != 0:
            return -cmp if spec.direction == "descending" else cmp
    return 0


def _id_key(value: Any) -> Any:
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _dedup_logs(logs: list[dict], unique_id_field: Optional[str]) -> list[dict]:
    if not unique_id_field:
        return logs
    seen: set = set()
    deduped: list[dict] = []
    for log in logs:
        value = (log.get("entries") or {}).get(unique_id_field)
        if value is None:
            deduped.append(log)
            continue
        key = _id_key(value)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(log)
    return deduped


@router.post("/logs/federated")
def get_federated_logs(
    request: FederatedLogsRequest,
    request_fastapi: Request,
    session=Depends(get_db_session),
):
    """Read several contexts as one globally-ordered, windowed log list.

    Response shape::

        {
          "logs":   [...],                 # merged window, ordered
          "count":  <int>,                 # sum of per-context match counts
          "counts": {"<source>": <int>},   # per-context match counts
        }

    Missing contexts contribute nothing (tolerates roots where a table has
    not been provisioned yet). Counts are pre-deduplication.
    """
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    field_type_dao = FieldTypeDAO(session)

    user_id = request_fastapi.state.user_id
    organization_id = getattr(request_fastapi.state, "organization_id", None)

    # Resolve every referenced project once, enforcing readability per
    # project (a spec naming an unreadable project fails the whole request).
    project_ids: dict[str, int] = {}
    for spec in request.contexts:
        name = spec.project_name or request.project_name
        if name in project_ids:
            continue
        try:
            project_ids[name] = project_dao.get_readable_by_user_and_name(
                name=name,
                user_id=user_id,
                organization_id=organization_id,
            ).id
        except Exception:
            raise HTTPException(
                status_code=404,
                detail=f"Project {name} not found.",
            )

    # `missing='first'` breaks the windowed-fetch exactness argument (the
    # backend orders NULLs last), so those requests fetch full branches.
    # `limit=0` is a count-only read: fetch a single row per branch (the
    # total count is computed pre-pagination regardless).
    exact_window = all(spec.missing == "last" for spec in request.sorting)
    window = request.offset + request.limit
    count_only = request.limit == 0
    # Cap branch fetches even for missing='first' so large contexts cannot
    # unbounded-materialize. Over-fetch a window multiple for approximate merge.
    _FEDERATED_BRANCH_HARD_CAP = 10_000
    if count_only:
        branch_limit: Optional[int] = 1
    elif exact_window:
        branch_limit = window
    else:
        branch_limit = min(max(window * 5, window), _FEDERATED_BRANCH_HARD_CAP)

    backend_sorting = None
    if request.sorting:
        import json as _json

        backend_sorting = _json.dumps(
            {spec.field: spec.direction for spec in request.sorting},
        )

    merged: list[tuple[int, int, dict]] = []
    counts: dict[str, int] = {}
    total_count = 0

    for source_order, spec in enumerate(request.contexts):
        project_name = spec.project_name or request.project_name
        project_id = project_ids[project_name]
        source_label = spec.source or spec.context

        context_obj = context_dao.filter(name=spec.context, project_id=project_id)
        if not context_obj:
            counts[source_label] = counts.get(source_label, 0)
            continue
        context_id = context_obj[0][0].id

        combined_filter = _combine_filters(request.filter, spec.filter)
        from_fields_param = "&".join(spec.from_fields) if spec.from_fields else None
        exclude_fields_param = (
            "&".join(spec.exclude_fields) if spec.exclude_fields else None
        )

        try:
            rows, branch_count = _get_logs_query(
                request_fastapi,
                project_name=project_name,
                context=spec.context,
                filter=combined_filter,
                sorting=backend_sorting,
                from_ids=None,
                exclude_ids=None,
                from_fields=from_fields_param,
                exclude_fields=exclude_fields_param,
                limit=branch_limit,
                offset=0,
                project_dao=project_dao,
                field_type_dao=field_type_dao,
                context_dao=context_dao,
                session=session,
            )
        except DataError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid data format in filter: {_sanitize_sql_error(exc)}",
            )
        except SQLAlchemyError as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Database error: {_sanitize_sql_error(exc)}",
            )

        counts[source_label] = counts.get(source_label, 0) + int(branch_count)
        total_count += int(branch_count)
        if count_only:
            continue

        field_types = field_type_dao.get_field_types(
            project_id,
            context_id=context_id,
            return_mutable=True,
        )
        field_order_map = field_type_dao.get_ordered_field_names(
            project_id,
            context_id=context_id,
        )
        logs_out, _ = _format_logs(
            rows=rows,
            field_types=field_types,
            value_limit=request.value_limit,
            column_context=None,
            field_order_map=field_order_map,
            from_fields=from_fields_param,
            exclude_fields=exclude_fields_param,
        )
        for local_order, log in enumerate(logs_out):
            if request.annotate:
                entries = log.get("entries")
                if isinstance(entries, dict):
                    entries[SOURCE_FIELD] = source_label
                    entries[CONTEXT_FIELD] = spec.context
            merged.append((source_order, local_order, log))

    # Each branch arrives in backend order; the global merge re-sorts with the
    # requested NULLs placement, stable on (source order, local order) so
    # unsorted reads preserve source-then-fetch order exactly like a
    # same-order UNION ALL would.
    logs = [log for *_order, log in merged]
    if request.sorting:
        sorting_specs = list(request.sorting)
        logs.sort(
            key=cmp_to_key(
                lambda left, right: _compare_by_sorting(left, right, sorting_specs),
            ),
        )
    logs = _dedup_logs(logs, request.unique_id_field)
    logs = logs[request.offset : request.offset + request.limit]

    return {"logs": logs, "count": total_count, "counts": counts}


__all__ = ["router"]
