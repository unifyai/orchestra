import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import JSON, Integer, and_, case, cast, func, literal, select
from sqlalchemy.sql.selectable import Subquery

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.field_type_dao import FieldTypeDAO
from orchestra.db.dao.log_dao import LogDAO
from orchestra.db.dao.log_event_dao import LogEventDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.models.orchestra_models import (
    Context,
    DerivedLog,
    JSONLog,
    JSONLogHistory,
    Log,
    LogEvent,
    LogHistory,
)
from orchestra.web.api.log.schema import CreateLogConfig

__all__ = [
    "create_logs_internal",
    "_build_unified_logs_subquery",
    "_flatten_fields",
    "_format_flat_logs",
    "_get_final_logs",
    "is_image_field",
]
#########################
# Logs Utils            #
#########################


def create_logs_internal(
    request: CreateLogConfig,
    project_id: int,
    context_id: int,
    project_dao: ProjectDAO,
    field_type_dao: FieldTypeDAO,
    log_event_dao: LogEventDAO,
    log_dao: LogDAO,
    context_dao: ContextDAO,
):
    """
    Core implementation of log creation logic, extracted from the create_logs endpoint.
    This function handles the actual creation of logs after project and context validation.

    Args:
        request: The CreateLogConfig containing entries and params to create
        project_id: The ID of the project to create logs for
        context_id: The ID of the context to associate logs with
        project_dao: Data access object for projects
        field_type_dao: Data access object for field types
        log_event_dao: Data access object for log events
        log_dao: Data access object for logs
        context_dao: Data access object for contexts

    Returns:
        List of created log event IDs

    Raises:
        HTTPException: If validation fails or duplicate logs are detected
    """
    # Convert single entries/params to list format for uniform processing
    entries_list = (
        request.entries if isinstance(request.entries, list) else [request.entries]
    )
    params_list = (
        request.params if isinstance(request.params, list) else [request.params]
    )

    # Validate and normalize params and entries
    if isinstance(request.entries, list) and isinstance(request.params, list):
        # Case 1: Both are lists - they should have equal lengths
        if len(request.entries) != len(request.params):
            raise HTTPException(
                status_code=400,
                detail=f"When both 'params' and 'entries' are provided as lists, they must have equal lengths. "
                f"Got params length: {len(request.params)}, entries length: {len(request.entries)}",
            )
    elif isinstance(request.entries, list) and (
        request.params is None or request.params == {}
    ):
        # Case 2: Entries is a list, params is None/empty - this is allowed
        params_list = [{}] * len(request.entries)
    elif isinstance(request.params, list) and (
        request.entries is None or request.entries == {}
    ):
        # Case 2: Params is a list, entries is None/empty - this is allowed
        entries_list = [{}] * len(request.params)
    elif isinstance(request.entries, list) and isinstance(request.params, dict):
        # Case 3: Entries is a list, params is a dict - convert params to a list of the same dict
        params_list = [
            {k: v for k, v in request.params.items()}
            for _ in range(len(request.entries))
        ]
    elif isinstance(request.params, list) and isinstance(request.entries, dict):
        # Case 3: Params is a list, entries is a dict - convert entries to a list of the same dict
        entries_list = [
            {k: v for k, v in request.entries.items()}
            for _ in range(len(request.params))
        ]

    # Get field types once for all operations
    field_types = field_type_dao.get_field_types(
        project_id,
        return_mutable=True,
        context_id=context_id,
    )

    def enforce_types(
        field_name,
        value,
        batch_index=None,
        explicit_types=None,
        context_id=None,
        is_param=False,
    ):
        entered_type = LogDAO.infer_type(field_name, value)
        field_info = field_types.get(field_name)
        if field_info:
            # Check field category first
            existing_category = field_info["field_category"]
            new_category = "param" if is_param else "entry"
            if existing_category != new_category:
                new_article = "an" if new_category == "entry" else "a"
                existing_article = "an" if existing_category == "entry" else "a"
                raise HTTPException(
                    status_code=400,
                    detail=f"Field '{field_name}' already exists as {existing_article} {existing_category}. Cannot create it as {new_article} {new_category}.",
                )

        # Then check data type
        expected_type = field_info["field_type"] if field_info else None
        if expected_type:
            if expected_type == "NoneType":
                if entered_type == "NoneType":
                    return
                # update the field type to the new type
                field_type_dao.upsert_field_type(
                    project_id,
                    field_name,
                    value,
                    field_category="param" if is_param else "entry",
                    context_id=context_id,
                )
            elif entered_type != expected_type and entered_type != "NoneType":
                batch_info = (
                    f" (in batch entry {batch_index})"
                    if batch_index is not None
                    else ""
                )
                raise HTTPException(
                    status_code=400,
                    detail=f"Type mismatch for field '{field_name}'{batch_info}: expected {expected_type}, got {entered_type}. Value: {str(value)[:100]}",
                )
        else:
            # Extract mutable flag from explicit_types if present
            mutable = (
                explicit_types.get(field_name, {}).get("mutable", False)
                if explicit_types
                else False
            )
            # If in a versioned context, force mutable=True
            if context_id and context_dao.is_versioned(context_id):
                mutable = True
            field_type_dao.create_field_type_if_absent(
                project_id,
                field_name,
                value,
                mutable=mutable,
                field_category="param" if is_param else "entry",
                context_id=context_id,
            )

    # Bulk create all log events at once
    entries_len = len(entries_list)
    params_len = len(params_list)
    total_logs = max(entries_len, params_len)

    # Bulk create all log events in one operation
    log_event_ids = log_event_dao.bulk_create(
        project_id=project_id,
        context_id=context_id,
        count=total_logs,
    )

    # Prepare collections for bulk operations
    new_field_types = []
    log_records_to_create = []

    # Process all logs in the batch
    for i in range(total_logs):
        log_event_id = log_event_ids[i]

        # Get current entries and params
        # If i exceeds list length, use the last item in the list
        current_entries = entries_list[min(i, entries_len - 1)]
        current_params = params_list[min(i, params_len - 1)]

        # Extract explicit types
        entries_explicit_types = (
            current_entries.pop("explicit_types", {})
            if isinstance(current_entries, dict)
            else None
        )
        params_explicit_types = (
            current_params.pop("explicit_types", {})
            if isinstance(current_params, dict)
            else None
        )

        # Process params - collect them for bulk creation
        for k, v in current_params.items():
            # Check and register new field types if needed
            if k not in field_types:
                mutable = (
                    params_explicit_types.get(k, {}).get("mutable", False)
                    if params_explicit_types
                    else False
                )
                # If in a versioned context, force mutable=True
                if context_id and context_dao.is_versioned(context_id):
                    mutable = True
                new_field_types.append(
                    {
                        "project_id": project_id,
                        "field_name": k,
                        "value": v,
                        "mutable": mutable,
                        "field_category": "param",
                        "context_id": context_id,
                    },
                )
            else:
                # Enforce types for existing fields
                enforce_types(k, v, i, params_explicit_types, context_id, is_param=True)

            # Determine version for parameter
            existing_param = log_dao.filter(
                key=k,
                value=json.dumps(v),
                project_id=project_id,
            )
            if existing_param:
                version = existing_param[0][0].version
            else:
                version = log_dao.get_next_param_version(project_id, context_id, k)

            # Add to records for bulk creation
            log_records_to_create.append(
                {
                    "project_id": project_id,
                    "log_event_id": log_event_id,
                    "key": k,
                    "value": v,
                    "version": version,
                    "explicit_types": params_explicit_types,
                    "context_id": context_id,
                },
            )

        # Process entries - collect them for bulk creation
        for k, v in current_entries.items():
            # Check and register new field types if needed
            if k not in field_types:
                mutable = (
                    entries_explicit_types.get(k, {}).get("mutable", False)
                    if entries_explicit_types
                    else False
                )
                # If in a versioned context, force mutable=True
                if context_id and context_dao.is_versioned(context_id):
                    mutable = True
                new_field_types.append(
                    {
                        "project_id": project_id,
                        "field_name": k,
                        "value": v,
                        "mutable": mutable,
                        "field_category": "entry",
                        "context_id": context_id,
                    },
                )
            else:
                # Enforce types for existing fields
                enforce_types(
                    k,
                    v,
                    i,
                    entries_explicit_types,
                    context_id,
                    is_param=False,
                )

            # Add to records for bulk creation (entries don't have version)
            log_records_to_create.append(
                {
                    "project_id": project_id,
                    "log_event_id": log_event_id,
                    "key": k,
                    "value": v,
                    "explicit_types": entries_explicit_types,
                    "context_id": context_id,
                },
            )

    # Bulk create new field types if any
    if new_field_types:
        field_type_dao.bulk_create_field_types(new_field_types)

    # Bulk create all log records
    log_dao.bulk_create(log_records_to_create)

    # Check for duplicates if context doesn't allow duplicates
    context_obj = None
    if context_id:
        context_obj = (
            context_dao.session.query(Context).filter_by(id=context_id).first()
        )
        # Check if context doesn't allow duplicates
        if context_obj and not context_obj.allow_duplicates:
            for log_event_id in log_event_ids:
                # Check for duplicates
                duplicate = context_dao.check_for_duplicates(context_id, log_event_id)
                if duplicate:
                    log_event_dao.delete(log_event_id)
                    raise HTTPException(
                        status_code=400,
                        detail=f"Duplicate log detected in context '{context_obj.name}' which doesn't allow duplicates. Log event ID: {log_event_id}",
                    )
    if context_obj and context_obj.is_versioned:
        # archive the new state
        context_dao.archive_context_state(
            context_obj,
            name="create",
            description=f"Created {total_logs} new LogEvents",
        )
        context_obj.version += 1
        context_obj.updated_at = datetime.now(timezone.utc)
        context_dao.session.commit()

    return log_event_ids


# TODO(yusha): refactor get_logs_query to make it modular
def _build_unified_logs_subquery(
    session,
    event_ids: Optional[Subquery] = None,
    relevant_log_events: Optional[Subquery] = None,
    key: str = None,
    return_versions: bool = False,
) -> Subquery:
    """
    Build a unified subquery that combines base logs and derived logs based on return_versions parameter.

    Args:
        session: The database session
        event_ids: Optional list of event IDs to filter by directly
        relevant_log_events: Optional subquery containing relevant log event IDs to join with
        return_versions: Whether to include version history in the query

    Returns:
        A unified subquery combining base and derived logs
    """
    if event_ids is None and relevant_log_events is None:
        raise ValueError("Either event_ids or relevant_log_events must be provided")

    def _apply_event_filter(query, table):
        if event_ids is not None:
            return query.filter(LogEvent.id.in_(event_ids))
        query = query.join(relevant_log_events, relevant_log_events.c.id == LogEvent.id)
        if key:
            return query.filter(table.key == key)
        return query

    if return_versions:
        # get latest version + all history logs
        base_logs_q_current = session.query(
            Log.id.label("id"),
            Log.log_event_id.label("log_event_id"),
            Log.key.label("key"),
            Log.value.label("value"),
            Log.inferred_type.label("inferred_type"),
            Log.version.label("param_version"),
            cast(None, Integer).label("context_version"),
            Log.updated_at.label("updated_at"),
            LogEvent.created_at.label("created_at"),
            literal("current").label("source_type"),
        ).join(LogEvent, LogEvent.id == Log.log_event_id)
        base_logs_q_current = _apply_event_filter(base_logs_q_current, Log)

        base_logs_q_history = session.query(
            LogHistory.id.label("id"),
            LogHistory.log_event_id.label("log_event_id"),
            LogHistory.key.label("key"),
            LogHistory.value.label("value"),
            LogHistory.inferred_type.label("inferred_type"),
            cast(None, Integer).label("param_version"),
            LogHistory.version.label("context_version"),
            LogHistory.archived_at.label("updated_at"),
            LogEvent.created_at.label("created_at"),
            literal("history").label("source_type"),
        ).join(LogEvent, LogEvent.id == LogHistory.log_event_id)
        base_logs_q_history = _apply_event_filter(base_logs_q_history, LogHistory)

        base_logs_q = base_logs_q_current.union_all(base_logs_q_history)
    else:
        # get only the latest version of the logs
        base_logs_q = session.query(
            Log.id.label("id"),
            Log.log_event_id.label("log_event_id"),
            Log.key.label("key"),
            Log.value.label("value"),
            Log.inferred_type.label("inferred_type"),
            Log.version.label("param_version"),
            cast(None, Integer).label("context_version"),
            Log.updated_at.label("updated_at"),
            LogEvent.created_at.label("created_at"),
            literal("base").label("source_type"),
        ).join(LogEvent, LogEvent.id == Log.log_event_id)
        base_logs_q = _apply_event_filter(base_logs_q, Log)

    derived_logs_q = session.query(
        DerivedLog.id.label("id"),
        DerivedLog.log_event_id.label("log_event_id"),
        DerivedLog.key.label("key"),
        DerivedLog.value.label("value"),
        DerivedLog.inferred_type.label("inferred_type"),
        # derived logs have no version => cast to None
        cast(None, Integer).label("param_version"),
        cast(None, Integer).label("context_version"),
        DerivedLog.updated_at.label("updated_at"),
        DerivedLog.created_at.label("created_at"),
        literal("derived").label("source_type"),
    ).join(LogEvent, LogEvent.id == DerivedLog.log_event_id)
    derived_logs_q = _apply_event_filter(derived_logs_q, DerivedLog)

    unified_logs_subq = base_logs_q.union_all(derived_logs_q).subquery(
        name="unified_logs",
    )
    # re-label columns to avoid anonymous column names
    return select(
        unified_logs_subq.c[unified_logs_subq.c.keys()[0]].label("id"),
        unified_logs_subq.c[unified_logs_subq.c.keys()[1]].label("log_event_id"),
        unified_logs_subq.c[unified_logs_subq.c.keys()[2]].label("key"),
        unified_logs_subq.c[unified_logs_subq.c.keys()[3]].label("value"),
        unified_logs_subq.c[unified_logs_subq.c.keys()[4]].label("inferred_type"),
        unified_logs_subq.c[unified_logs_subq.c.keys()[5]].label("param_version"),
        unified_logs_subq.c[unified_logs_subq.c.keys()[6]].label("context_version"),
        unified_logs_subq.c[unified_logs_subq.c.keys()[7]].label("updated_at"),
        unified_logs_subq.c[unified_logs_subq.c.keys()[8]].label("created_at"),
        unified_logs_subq.c[unified_logs_subq.c.keys()[9]].label("source_type"),
    ).subquery("unified_logs")


######################
# Formatting functions
######################


def _flatten_fields(
    log_fields: list,
):
    flattened = dict()
    for log_ids, fields in log_fields:
        log_ids = log_ids if isinstance(log_ids, list) else [log_ids]
        fields = fields if isinstance(fields, list) else [fields]
        for log_id in log_ids:
            if log_id not in flattened:
                flattened[log_id] = list()
            for field in fields:
                if field is not None and field not in flattened[log_id]:
                    flattened[log_id].append(field)
    return flattened


def is_image_field(field_name: str, field_types: dict) -> bool:
    """Check if a field is an image type."""
    return field_types.get(field_name) == "image"


def _format_flat_logs(rows, context_len, value_limit, field_order_map):
    """Helper function to format flat logs using raw query data"""
    formatted = {}

    for (
        row_key,
        row_value,
        row_inferred_type,
        row_param_version,
        row_context_version,
        row_source_type,
        row_created_at,
        row_event_id,
    ) in rows:

        if row_event_id not in formatted:
            formatted[row_event_id] = {
                "ts": row_created_at.isoformat() if row_created_at else None,
                "clipped_fields": [],
                "entries": {},
                "versions": {},
                "context_versions": {},
                "derived_entries": {},
            }

        is_derived = row_source_type == "derived"

        # Apply context_len slicing to the key
        key = row_key

        def _limit_value(value: any, inferred_type: str) -> tuple:
            """Limit the size of a value based on its type and the value_limit parameter.
            Returns a tuple of (limited_value, is_clipped)."""
            if value_limit is None:
                return value, False

            # Handle numeric values - return as is
            if inferred_type in ["int", "float", "bool"]:
                return value, False

            if inferred_type == "image":
                return "", True

            if inferred_type in ["list", "dict", "tuple"]:
                str_value = str(value)
                if len(str_value) > value_limit:
                    return str_value[:value_limit] + "...", True
                return str_value, False

            # Handle string values
            if inferred_type == "str":
                if len(str(value)) > value_limit:
                    return str(value)[:value_limit] + "...", True
                return value, False

            # Default case - treat as string
            str_value = str(value)
            if len(str_value) > value_limit:
                return str_value[:value_limit] + "...", True
            return str_value, False

        # Apply value limiting and get clipped status
        limited_val, is_clipped = _limit_value(row_value, row_inferred_type)
        if is_clipped:
            formatted[row_event_id]["clipped_fields"].append(key)

        if is_derived:
            formatted[row_event_id]["derived_entries"][key] = limited_val
        else:
            if row_param_version is not None:
                # param-based version
                if key not in formatted[row_event_id]["versions"]:
                    formatted[row_event_id]["versions"][key] = {}
                formatted[row_event_id]["versions"][key][
                    row_param_version
                ] = limited_val
                formatted[row_event_id]["entries"][key] = str(row_param_version)

            elif row_context_version is not None:
                # context-based version
                if key not in formatted[row_event_id]["context_versions"]:
                    formatted[row_event_id]["context_versions"][key] = {}
                formatted[row_event_id]["context_versions"][key][
                    row_context_version
                ] = limited_val
                if key not in formatted[row_event_id]["entries"]:
                    formatted[row_event_id]["entries"][key] = limited_val

            else:
                # entries
                formatted[row_event_id]["entries"][key] = limited_val

    # Now build final JSON
    logs_out = []
    params_out = {}
    for event_id, data in formatted.items():
        entries = {}
        params = {}
        for k, v in data["entries"].items():
            if k in data["versions"]:
                # It's param-based
                params[k] = v  # v is the str(ver)
                # Also store in params_out if needed
                if k not in params_out:
                    params_out[k] = {}
                # We might have multiple versions for the same param
                for ver_num, ver_val in data["versions"][k].items():
                    params_out[k][ver_num] = ver_val
            else:
                # It's a normal base entry
                entries[k] = v

        # derived_entries
        derived_entries = data["derived_entries"]

        # Sort all dictionaries according to field_type order
        sorted_entries = dict(
            sorted(
                entries.items(),
                key=lambda x: field_order_map.get(x[0], float("inf")),
            ),
        )
        sorted_params = dict(
            sorted(
                params.items(),
                key=lambda x: field_order_map.get(x[0], float("inf")),
            ),
        )
        sorted_derived = dict(
            sorted(
                derived_entries.items(),
                key=lambda x: field_order_map.get(x[0], float("inf")),
            ),
        )
        # sort keys which are strings by descending order
        sorted_context_versions = {
            field: dict(sorted(versions.items(), key=lambda x: x[0], reverse=True))
            for field, versions in data["context_versions"].items()
        }
        logs_out.append(
            {
                "id": event_id,
                "ts": data["ts"],
                "entries": sorted_entries,
                "params": sorted_params,
                "derived_entries": sorted_derived,
                "versions": sorted_context_versions,
                "clipped_fields": data.get("clipped_fields", []),
            },
        )

    return logs_out, params_out


def _get_final_logs(session, filtered_logs_subq, paginated_ids_subq):
    """
    Returns final rows with the JSONLog value (if available) restored.
    """
    # Outer join JSONLog and JSONLogHistory based on source_type
    final_logs_query = (
        session.query(
            filtered_logs_subq.c.id,
            filtered_logs_subq.c.log_event_id,
            filtered_logs_subq.c.key,
            # Use coalesce to select the appropriate JSON value based on source_type
            func.coalesce(
                case(
                    (
                        filtered_logs_subq.c.source_type == "history",
                        JSONLogHistory.value,
                    ),
                    else_=JSONLog.value,
                ),
                cast(filtered_logs_subq.c.value, JSON),
            ).label("value"),
            filtered_logs_subq.c.inferred_type,
            filtered_logs_subq.c.param_version,
            filtered_logs_subq.c.context_version,
            filtered_logs_subq.c.created_at,
            filtered_logs_subq.c.source_type,
        )
        .outerjoin(
            JSONLog,
            and_(
                JSONLog.log_event_id == filtered_logs_subq.c.log_event_id,
                JSONLog.key == filtered_logs_subq.c.key,
                filtered_logs_subq.c.source_type != "history",
            ),
        )
        .outerjoin(
            JSONLogHistory,
            and_(
                JSONLogHistory.log_event_id == filtered_logs_subq.c.log_event_id,
                JSONLogHistory.key == filtered_logs_subq.c.key,
                JSONLogHistory.version == filtered_logs_subq.c.context_version,
                filtered_logs_subq.c.source_type == "history",
            ),
        )
        .join(
            paginated_ids_subq,
            paginated_ids_subq.c.log_event_id == filtered_logs_subq.c.log_event_id,
        )
        .order_by(paginated_ids_subq.c.row_num, filtered_logs_subq.c.created_at)
    )
    return final_logs_query.all()
