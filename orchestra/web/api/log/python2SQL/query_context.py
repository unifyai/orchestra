"""
Query Context for CTE-based aggregation optimization.

This module provides infrastructure to track aggregation expressions during query building
and generate CTEs for pre-computation, avoiding correlated scalar subqueries in WHERE clauses.

The optimization transforms queries from:
    WHERE CAST(coalesce((SELECT avg(...) FROM jsonb_array_elements(...)), 0) AS INTEGER) = 2

To:
    WITH agg_mean_list_0 AS MATERIALIZED (
        SELECT id, avg(...) AS agg_value FROM log_event, jsonb_array_elements(...) GROUP BY id
    )
    SELECT * FROM log_event
    JOIN agg_mean_list_0 ON log_event.id = agg_mean_list_0.id
    WHERE agg_mean_list_0.agg_value = 2
"""

from typing import Any, Dict, List, Optional

from sqlalchemy import Float, and_, cast, func, literal, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import CTE

__all__ = ["QueryContext", "CTEColumnReference"]


class CTEColumnReference(ColumnElement):
    """
    Placeholder for a CTE column reference.

    This is used during query building to mark where a CTE column reference
    should be inserted. After CTEs are built, these placeholders are replaced
    with actual column references.

    Attributes:
        cte_name: Name of the CTE this references
        column_name: Name of the column within the CTE
        _cte_info: Internal storage for CTE building information
    """

    inherit_cache = True  # Enable SQLAlchemy caching

    def __init__(
        self,
        cte_name: str,
        column_name: str = "agg_value",
        cte_info: Optional[Dict[str, Any]] = None,
    ):
        super().__init__()
        self.cte_name = cte_name
        self.column_name = column_name
        self._cte_info = cte_info or {}

    def __repr__(self):
        return f"CTEColumnReference({self.cte_name!r}, {self.column_name!r})"


class QueryContext:
    """
    Tracks CTEs and aggregation expressions during query building.

    This class accumulates aggregation expressions that should be pre-computed
    in CTEs rather than executed as correlated subqueries. After query building
    is complete, CTEs can be generated and applied to the main query.

    Usage:
        context = QueryContext()

        # During query building, register aggregations
        ref = context.register_aggregation(
            log_event_alias=LogEvent,
            jsonb_field_expr=LogEvent.data.op("->")("test_list"),
            field_key="test_list",
            metric_name="mean",
            inferred_type="list",
        )

        # After building, check if CTEs are needed
        if context.has_aggregations():
            ctes = context.build_ctes(LogEvent, base_filters)
            where_clause = context.replace_cte_refs(where_clause, ctes)
    """

    def __init__(self):
        self.aggregation_ctes: List[Dict[str, Any]] = []
        self.aggregation_counter: int = 0
        self._cte_objects: Dict[str, CTE] = {}

    def register_aggregation(
        self,
        log_event_alias,
        jsonb_field_expr,
        field_key: str,
        metric_name: str,
        inferred_type: str,
    ) -> CTEColumnReference:
        """
        Register an aggregation for CTE pre-computation.

        Instead of building a correlated scalar subquery, this stores the
        aggregation info and returns a CTEColumnReference placeholder that
        will be replaced with the actual CTE column reference after CTEs are built.

        Args:
            log_event_alias: The LogEvent model/alias for correlation
            jsonb_field_expr: The JSONB field expression (e.g., LogEvent.data -> 'field')
            field_key: The field name being aggregated
            metric_name: The aggregation metric (mean, sum, var, etc.)
            inferred_type: The inferred type of the field (list, dict, Any, float, etc.)

        Returns:
            CTEColumnReference: A placeholder that will be replaced with actual CTE column
        """
        # Sanitize field_key for use in CTE name (replace non-alphanumeric chars)
        safe_field_key = "".join(c if c.isalnum() else "_" for c in field_key)

        # Generate unique CTE name
        cte_name = f"agg_{metric_name}_{safe_field_key}_{self.aggregation_counter}"
        self.aggregation_counter += 1

        # Store aggregation info for later CTE building
        cte_info = {
            "cte_name": cte_name,
            "log_event_alias": log_event_alias,
            "jsonb_field_expr": jsonb_field_expr,
            "field_key": field_key,
            "metric_name": metric_name,
            "inferred_type": inferred_type,
        }
        self.aggregation_ctes.append(cte_info)

        # Return placeholder with CTE info attached
        return CTEColumnReference(cte_name, "agg_value", cte_info)

    def has_aggregations(self) -> bool:
        """Check if any aggregations were registered."""
        return len(self.aggregation_ctes) > 0

    def build_ctes(
        self,
        log_event_alias,
        base_filters: Optional[List] = None,
    ) -> Dict[str, CTE]:
        """
        Generate SQLAlchemy CTE objects from registered aggregations.

        Each CTE computes the aggregation once per log_event row, then the main
        query joins on log_event.id to filter on the pre-computed values.

        Args:
            log_event_alias: The LogEvent model/alias
            base_filters: Base WHERE filters to apply in CTEs (project_id, context_id, etc.)

        Returns:
            Dict mapping CTE names to CTE objects
        """
        from ..utils.metric_utils import AggregationMetric

        ctes = {}

        for cte_info in self.aggregation_ctes:
            cte_name = cte_info["cte_name"]
            jsonb_field_expr = cte_info["jsonb_field_expr"]
            metric_name = cte_info["metric_name"]
            inferred_type = cte_info["inferred_type"]

            # Map metric name to AggregationMetric enum
            metric_map = {
                "mean": AggregationMetric.MEAN,
                "sum": AggregationMetric.SUM,
                "var": AggregationMetric.VAR,
                "std": AggregationMetric.STD,
                "min": AggregationMetric.MIN,
                "max": AggregationMetric.MAX,
                "median": AggregationMetric.MEDIAN,
                "mode": AggregationMetric.MODE,
                "count": AggregationMetric.COUNT,
            }
            metric = metric_map.get(metric_name)

            # Build aggregation expression for CTE
            agg_expr = self._build_cte_aggregation_expr(
                jsonb_field_expr,
                metric,
                inferred_type,
            )

            # Build CTE query:
            # SELECT log_event.id, <aggregation_expr> AS agg_value
            # FROM log_event, LATERAL jsonb_array_elements(...)
            # WHERE <base_filters>
            # GROUP BY log_event.id
            cte_query = self._build_cte_query(
                log_event_alias,
                jsonb_field_expr,
                agg_expr,
                inferred_type,
                base_filters,
            )

            # Create materialized CTE
            cte = cte_query.cte(cte_name).prefix_with("MATERIALIZED")
            ctes[cte_name] = cte

        self._cte_objects = ctes
        return ctes

    def _build_cte_aggregation_expr(
        self,
        jsonb_field_expr,
        metric,
        inferred_type: str,
    ):
        """
        Build the aggregation expression for a CTE.

        This replicates the logic from _get_reduction_expr but returns
        just the aggregation function call, not wrapped in a scalar subquery.
        """
        from ..utils.metric_utils import AggregationMetric

        if inferred_type in ["list", "dict", "Any"]:
            # For list/dict types, we aggregate over the elements
            # The actual jsonb_array_elements/jsonb_each is handled in _build_cte_query
            # Here we just build the aggregation function
            numeric_col = cast(func.jsonb_elem.c.value, Float)

            if metric == AggregationMetric.COUNT:
                return func.count(numeric_col)
            elif metric == AggregationMetric.SUM:
                return func.sum(numeric_col)
            elif metric == AggregationMetric.MEAN:
                return func.avg(numeric_col)
            elif metric == AggregationMetric.VAR:
                return func.var_pop(numeric_col)
            elif metric == AggregationMetric.STD:
                return func.stddev_pop(numeric_col)
            elif metric == AggregationMetric.MIN:
                return func.min(numeric_col)
            elif metric == AggregationMetric.MAX:
                return func.max(numeric_col)
            elif metric == AggregationMetric.MEDIAN:
                return func.percentile_cont(0.5).within_group(numeric_col.asc())
            elif metric == AggregationMetric.MODE:
                return func.mode().within_group(numeric_col.asc())

        # For scalar types, the aggregation is simpler
        return None  # Scalar types don't benefit from CTE optimization

    def _build_cte_query(
        self,
        log_event_alias,
        jsonb_field_expr,
        agg_expr,
        inferred_type: str,
        base_filters: Optional[List] = None,
    ):
        """
        Build the full CTE query for an aggregation.

        For list types:
            SELECT log_event.id, avg(CAST(elem.value AS FLOAT)) AS agg_value
            FROM log_event, LATERAL jsonb_array_elements(log_event.data -> 'field') AS elem
            WHERE <base_filters>
            GROUP BY log_event.id

        For dict types:
            SELECT log_event.id, avg(CAST(kv.value AS FLOAT)) AS agg_value
            FROM log_event, LATERAL jsonb_each(log_event.data -> 'field') AS kv
            WHERE <base_filters>
            GROUP BY log_event.id
        """
        if inferred_type in ["list", "Any"]:
            # Use jsonb_array_elements for list types
            elements = (
                func.jsonb_array_elements(
                    cast(jsonb_field_expr, JSONB),
                )
                .table_valued("value")
                .alias("jsonb_elem")
            )

            numeric_col = cast(elements.c.value, Float)

            # Build aggregation based on metric
            # We need to rebuild agg_expr with the correct column reference
            agg_expr_with_col = self._rebuild_agg_expr_with_column(
                agg_expr,
                numeric_col,
            )

            cte_query = (
                select(
                    log_event_alias.id.label("id"),
                    func.coalesce(agg_expr_with_col, 0).label("agg_value"),
                )
                .select_from(log_event_alias)
                .join(elements, literal(True))
            )

        elif inferred_type == "dict":
            # Use jsonb_each for dict types
            key_values = (
                func.jsonb_each(
                    cast(jsonb_field_expr, JSONB),
                )
                .table_valued("key", "value")
                .alias("jsonb_elem")
            )

            numeric_col = cast(key_values.c.value, Float)

            agg_expr_with_col = self._rebuild_agg_expr_with_column(
                agg_expr,
                numeric_col,
            )

            cte_query = (
                select(
                    log_event_alias.id.label("id"),
                    func.coalesce(agg_expr_with_col, 0).label("agg_value"),
                )
                .select_from(log_event_alias)
                .join(key_values, literal(True))
            )
        else:
            # For scalar types, no LATERAL join needed
            # This path shouldn't be used for CTE optimization (scalar types don't need it)
            cte_query = select(
                log_event_alias.id.label("id"),
                literal(0).label("agg_value"),
            ).select_from(log_event_alias)

        # Apply base filters
        if base_filters:
            cte_query = cte_query.where(and_(*base_filters))

        # Group by log_event.id
        cte_query = cte_query.group_by(log_event_alias.id)

        return cte_query

    def _rebuild_agg_expr_with_column(self, agg_expr, numeric_col):
        """
        Rebuild aggregation expression with the correct column reference.

        The agg_expr was built with a placeholder column, this rebuilds it
        with the actual column from the LATERAL join.
        """

        # Determine metric from agg_expr type and rebuild
        # This is a workaround since we can't easily modify the column in the expression
        if agg_expr is None:
            return func.avg(numeric_col)

        # Check what function was used and rebuild with correct column
        expr_str = str(agg_expr)
        if "avg" in expr_str.lower():
            return func.avg(numeric_col)
        elif "sum" in expr_str.lower():
            return func.sum(numeric_col)
        elif "count" in expr_str.lower():
            return func.count(numeric_col)
        elif "var_pop" in expr_str.lower():
            return func.var_pop(numeric_col)
        elif "stddev_pop" in expr_str.lower():
            return func.stddev_pop(numeric_col)
        elif "min" in expr_str.lower() and "percentile" not in expr_str.lower():
            return func.min(numeric_col)
        elif "max" in expr_str.lower():
            return func.max(numeric_col)
        elif "percentile_cont" in expr_str.lower():
            return func.percentile_cont(0.5).within_group(numeric_col.asc())
        elif "mode" in expr_str.lower():
            return func.mode().within_group(numeric_col.asc())

        # Default to avg
        return func.avg(numeric_col)

    def replace_cte_refs(
        self,
        where_clause,
        cte_objects: Dict[str, CTE],
    ):
        """
        Replace CTEColumnReference placeholders in WHERE clause with actual CTE column refs.

        This walks the SQLAlchemy expression tree and replaces CTEColumnReference
        instances with references to the corresponding CTE columns.

        Args:
            where_clause: The WHERE clause expression containing CTEColumnReference placeholders
            cte_objects: Dict mapping CTE names to CTE objects

        Returns:
            Modified WHERE clause with CTE column references
        """
        from sqlalchemy.sql import visitors

        def replace_visitor(element):
            if isinstance(element, CTEColumnReference):
                cte = cte_objects.get(element.cte_name)
                if cte is not None:
                    return cte.c.agg_value
            return None

        # Use SQLAlchemy's replacement visitor
        return visitors.replacement_traverse(where_clause, {}, replace_visitor)

    def get_cte_column_ref(self, cte_name: str, cte_objects: Dict[str, CTE]):
        """
        Get the column reference for a specific CTE.

        Args:
            cte_name: Name of the CTE
            cte_objects: Dict mapping CTE names to CTE objects

        Returns:
            Column reference to the CTE's agg_value column
        """
        cte = cte_objects.get(cte_name)
        if cte is not None:
            return cte.c.agg_value
        return None
