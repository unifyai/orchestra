"""Pydantic schemas for dashboard token endpoints."""

from typing import Any, Optional, Union

from pydantic import BaseModel, Field, model_validator


class RegisterTokenRequest(BaseModel):
    """Request body for POST /dashboards/tokens."""

    token: str = Field(..., min_length=1, max_length=12)
    entity_type: str = Field(..., pattern="^(tile|dashboard)$")
    context_name: str = Field(..., min_length=1, max_length=500)
    project_name: str = Field(..., min_length=1, max_length=200)


class RegisterTokenResponse(BaseModel):
    """Response for successful token registration."""

    token: str
    entity_type: str
    context_name: str


class TokenResolutionResponse(BaseModel):
    """Response for admin token resolution."""

    entity_type: str
    context_name: str
    user_id: str
    organization_id: Optional[int] = None
    project_id: int
    project_name: str


class FilterBridgeRequest(BaseModel):
    """Request body for the filter bridge proxy.

    Accepts both Orchestra-native field names (filter_expr, from_fields,
    exclude_fields) and Console proxy names (filter, columns,
    exclude_columns).  Console aliases take precedence when both are sent.
    """

    context: str
    filter_expr: Optional[str] = None
    from_fields: Optional[str] = None
    exclude_fields: Optional[str] = None
    sorting: Optional[str] = None
    limit: Optional[int] = Field(None, ge=1, le=1000)
    offset: Optional[int] = Field(None, ge=0)
    column_context: Optional[str] = None
    group_by: Optional[list[str]] = None
    randomize: Optional[bool] = None

    @model_validator(mode="before")
    @classmethod
    def _accept_console_aliases(cls, values: Any) -> Any:
        """Map Console proxy field names to Orchestra names."""
        if not isinstance(values, dict):
            return values
        alias_map = {
            "filter": "filter_expr",
            "columns": "from_fields",
            "exclude_columns": "exclude_fields",
        }
        for alias, canonical in alias_map.items():
            if alias in values and canonical not in values:
                values[canonical] = values.pop(alias)
            elif alias in values:
                values.pop(alias)
        return values


class FilterBridgeResponse(BaseModel):
    """Response from the filter bridge.

    Rows are flattened log entries (entries + derived_entries merged)
    for ergonomic JS consumption.
    """

    rows: list[dict]
    total_count: int


# ---------------------------------------------------------------------------
# Reduce bridge
# ---------------------------------------------------------------------------


class ReduceBridgeRequest(BaseModel):
    """Request body for the reduce (aggregation) bridge endpoint.

    Maps to ``compute_metric_for_key`` /
    ``_compute_metric_for_key_grouped`` using the tile creator's identity.
    """

    context: str
    metric: str = Field(
        ...,
        description="Aggregation function: count, sum, mean, var, std, min, max, median, mode.",
    )
    columns: Union[str, list[str]] = Field(
        ...,
        description="Column(s) to aggregate (maps to 'key' internally).",
    )
    filter_expr: Optional[str] = Field(
        None,
        description="Filter expression applied before aggregation.",
    )
    group_by: Optional[Union[str, list[str]]] = Field(
        None,
        description="Field(s) to group by before aggregation.",
    )

    @model_validator(mode="before")
    @classmethod
    def _accept_console_aliases(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        if "filter" in values and "filter_expr" not in values:
            values["filter_expr"] = values.pop("filter")
        elif "filter" in values:
            values.pop("filter")
        return values


class ReduceBridgeResponse(BaseModel):
    """Response from the reduce bridge.

    ``result`` is a scalar for ungrouped single-key, a dict for
    multi-key, or a nested dict when group_by is used.
    """

    result: Any


# ---------------------------------------------------------------------------
# Join bridge (row mode)
# ---------------------------------------------------------------------------


class JoinBridgeRequest(BaseModel):
    """Request body for the join bridge endpoint (row mode).

    Maps to ``_join_query_internal`` with ``metric=None`` using the tile
    creator's identity.
    """

    tables: list[str] = Field(
        ...,
        min_length=2,
        max_length=2,
        description="Exactly two context paths to join.",
    )
    join_expr: str = Field(
        ...,
        min_length=1,
        description="Join condition using aliases A and B, e.g. 'A.user_id == B.user_id'.",
    )
    select: Optional[dict[str, str]] = Field(
        None,
        description="Column mapping from 'A.col' / 'B.col' to output alias.",
    )
    mode: str = Field(
        default="inner",
        pattern="^(inner|left|right|outer)$",
        description="Join type.",
    )
    left_where: Optional[str] = Field(
        None,
        description="Pre-join filter for the left table.",
    )
    right_where: Optional[str] = Field(
        None,
        description="Pre-join filter for the right table.",
    )
    result_where: Optional[str] = Field(
        None,
        description="Post-join filter on the joined rows.",
    )
    result_limit: int = Field(100, ge=1, le=1000)
    result_offset: int = Field(0, ge=0)


class JoinBridgeResponse(BaseModel):
    """Response from the join bridge (row mode)."""

    rows: list[dict]
    total_count: int


# ---------------------------------------------------------------------------
# Join-reduce bridge
# ---------------------------------------------------------------------------


class JoinReduceBridgeRequest(BaseModel):
    """Request body for the join-reduce bridge endpoint.

    Maps to ``_join_query_internal`` with ``metric`` + ``key`` using the
    tile creator's identity.
    """

    tables: list[str] = Field(
        ...,
        min_length=2,
        max_length=2,
        description="Exactly two context paths to join.",
    )
    join_expr: str = Field(
        ...,
        min_length=1,
        description="Join condition using aliases A and B.",
    )
    select: Optional[dict[str, str]] = Field(
        None,
        description="Column mapping from 'A.col' / 'B.col' to output alias.",
    )
    mode: str = Field(
        default="inner",
        pattern="^(inner|left|right|outer)$",
        description="Join type.",
    )
    left_where: Optional[str] = Field(
        None,
        description="Pre-join filter for the left table.",
    )
    right_where: Optional[str] = Field(
        None,
        description="Pre-join filter for the right table.",
    )
    metric: str = Field(
        ...,
        description="Aggregation function: count, sum, mean, var, std, min, max, median, mode.",
    )
    columns: Union[str, list[str]] = Field(
        ...,
        description="Column(s) to aggregate (maps to 'key' internally).",
    )
    group_by: Optional[Union[str, list[str]]] = Field(
        None,
        description="Field(s) to group by before aggregation.",
    )
    result_where: Optional[str] = Field(
        None,
        description="Post-join filter applied before aggregation.",
    )


class JoinReduceBridgeResponse(BaseModel):
    """Response from the join-reduce bridge.

    ``result`` is a scalar for ungrouped single-key, a dict for
    multi-key, or a nested dict when group_by is used.
    """

    result: Any


# ---------------------------------------------------------------------------
# Dashboard action metadata
# ---------------------------------------------------------------------------


class DashboardActionRecord(BaseModel):
    """Action metadata stored in the Dashboards/Actions context."""

    tile_token: str
    action_name: str
    function_id: int
    request: str = ""
    label: str = ""
    icon: Optional[str] = None
    scope: str = "dashboard"


class UpsertDashboardActionsRequest(BaseModel):
    """Batch upsert of action metadata for a tile."""

    tile_token: str
    actions: list[DashboardActionRecord]
