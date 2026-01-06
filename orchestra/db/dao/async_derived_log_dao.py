"""Async version of derived_log_dao for use with AsyncSession."""

import json
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Union

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from orchestra.db.models.orchestra_models import (
    ActiveDerivedLog,
    DerivedLog,
    LogEvent,
    LogEventDerivedLog,
)
from orchestra.web.api.log.python2SQL import (
    _compute_expression,
    _extract_placeholders,
    _substitute_placeholders,
    str_filter_exp_to_dict,
)

logger = logging.getLogger(__name__)


class OverwriteError(Exception):
    pass


async def _transform_referenced_logs(
    equation: str,
    referenced_logs: Dict,
) -> List[Dict]:
    """
    Transform referenced_logs to use log placeholders (log0, log1, etc.) as keys.

    Args:
        equation: String containing placeholders like '{log0:a}+1 + {log1:b}'
        referenced_logs: Dict with original keys, e.g. {'a': 1, 'b': 2}

    Returns:
        Dict with transformed keys, e.g. {'log0': 1, 'log1': 2}
    """
    # Extract placeholders and their original keys
    placeholders = _extract_placeholders(equation)  # ['log0:a', 'log1:a']

    # Create transformed dictionary
    transformed = {}
    for p in placeholders:
        log_key, original_key = p.split(":")  # 'log0:a' -> ('log0', 'a')
        transformed[log_key] = referenced_logs[original_key]

    return transformed


async def _extract_field_names_from_equation(equation: str) -> List[str]:
    """
    Extract base field names from derived log equation for dependency tracking.

    This function is used during template creation/update to populate the
    `referenced_keys` JSONB column in ActiveDerivedLog. The stored keys enable
    fast indexed lookups for the "Ripple Effect" system.

    **Usage Pattern:**
    - Called when creating/updating ActiveDerivedLog templates to populate `referenced_keys`
    - NOT used during ripple effect queries (those use indexed `referenced_keys @>` lookups)

    Args:
        equation: String containing placeholders like '{log0:field_a} + {log1:field_b}'

    Returns:
        List of unique field names extracted from placeholders.

    Example:
        equation = "{log0:score} + {log1:accuracy}"
        result = _extract_field_names_from_equation(equation)
        # Returns: ['score', 'accuracy']

    Note:
        This function is intentionally kept simple (string parsing) since it only runs
        during template creation/update (rare operations). The performance-critical
        ripple effect queries use the pre-computed `referenced_keys` column with GIN indexing.
    """
    if not equation:
        return []

    try:
        placeholders = _extract_placeholders(
            equation,
        )  # ['log0:field_a', 'log1:field_b']
        field_names = set()
        for p in placeholders:
            if ":" in p:
                # 'log0:field_a' -> 'field_a'
                field_name = p.split(":", 1)[1]
                field_names.add(field_name)
            else:
                logger.warning(f"Malformed placeholder '{p}' in equation: {equation}")
        return list(field_names)
    except Exception as e:
        logger.warning(f"Failed to extract field names from equation '{equation}': {e}")
        return []


# noinspection PyBroadException
class AsyncDerivedLogDAO:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(
        self,
        log_event_id: int,
        key: str,
        equation: str,
        referenced_logs: Dict[str, int],
        value: int,
        inferred_type: str,
    ) -> int:

        ts = datetime.now(timezone.utc)

        new_derived_log = DerivedLog(
            key=key,
            equation=equation,
            referenced_logs=referenced_logs,
            value=value,
            inferred_type=inferred_type,
            created_at=ts,
            updated_at=ts,
        )

        self.session.add(new_derived_log)
        await self.session.flush()  # Get the ID without committing

        # Create the association
        log_event_derived_log = LogEventDerivedLog(
            log_event_id=log_event_id,
            derived_log_id=new_derived_log.id,
        )
        self.session.add(log_event_derived_log)

        await self.session.commit()
        return new_derived_log.id

    async def filter(
        self,
        id: Optional[Union[int, List[int]]] = None,
        log_event_id: Optional[Union[int, List[int]]] = None,
        key: Optional[Union[str, List[str]]] = None,
        value: Optional[Union[str, List[str]]] = None,
        equation: Optional[Union[str, List[str]]] = None,
        project_id: Optional[int] = None,
        defer: bool = False,
    ) -> List[DerivedLog]:
        async def normalize_input(value):
            if value is None or isinstance(value, list):
                return value
            return [value]

        id = normalize_input(id)
        log_event_id = normalize_input(log_event_id)
        key = normalize_input(key)
        value = normalize_input(value)
        equation = normalize_input(equation)

        if id == [] or log_event_id == [] or key == [] or value == [] or equation == []:
            return []

        query = (
            select(DerivedLog)
            .join(
                LogEventDerivedLog,
                LogEventDerivedLog.derived_log_id == DerivedLog.id,
            )
            .join(
                LogEvent,
                LogEvent.id == LogEventDerivedLog.log_event_id,
            )
        )
        if id:
            query = query.where(DerivedLog.id.in_(id))
        if log_event_id:
            query = query.where(LogEventDerivedLog.log_event_id.in_(log_event_id))
        if key:
            query = query.where(DerivedLog.key.in_(key))
        if value:
            query = query.where(DerivedLog.value.in_(value))
        if equation:
            query = query.where(DerivedLog.equation.in_(equation))
        if project_id:
            query = query.where(LogEvent.project_id == project_id)

        query = query.order_by(DerivedLog.created_at)
        rows = await self.session.execute(query)
        if defer:
            return rows
        return rows.fetchall()

    async def recompute_derived_logs(
        self,
        logs_to_recompute: List[DerivedLog],
        json_encoder: json.JSONEncoder,
        session: Session,
    ) -> None:
        """
        Recompute the 'value' (and optionally type) for each derived log in logs_to_recompute,
        based on the log's 'equation' and 'referenced_logs'. Then commit the changes.
        """
        try:
            for dlog in logs_to_recompute:
                # Get the associated log_event_id
                log_event_derived_log = (
                    session.query(LogEventDerivedLog)
                    .filter_by(derived_log_id=dlog.id)
                    .first()
                )
                if not log_event_derived_log:
                    continue

                log_event_id = log_event_derived_log.log_event_id

                reference_log = {k: log_event_id for k in dlog.referenced_logs.keys()}
                transformed_logs = _transform_referenced_logs(
                    dlog.equation,
                    reference_log,
                )
                filter_expr, alias_to_key_map = _substitute_placeholders(
                    dlog.equation,
                    transformed_logs,
                )
                filter_dict = str_filter_exp_to_dict(filter_expr)
                new_val = _compute_expression(
                    filter_dict,
                    LogEvent,
                    session,
                    [log_event_id],
                )[0][1]
                dlog.value = json.loads(json.dumps(new_val, cls=json_encoder))
                dlog.updated_at = datetime.now(timezone.utc)

            session.commit()
        except Exception as e:
            raise e

    async def recompute_derived_logs_jsonb(
        self,
        template: ActiveDerivedLog,
        log_ids: List[int],
        json_encoder: json.JSONEncoder,
        field_type_dao=None,
    ) -> int:
        """
        Recompute derived log values by materializing them directly into LogEvent.data.

        This method stores computed values in the LogEvent.data JSONB column rather than
        creating separate DerivedLog rows.

        Args:
            template: ActiveDerivedLog template containing equation and metadata
            log_ids: List of log_event_ids to recompute
            json_encoder: JSON encoder class for serializing values
            field_type_dao: Optional FieldTypeDAO for creating field type if absent

        Returns:
            Number of logs successfully updated
        """
        if not log_ids:
            return 0

        try:
            # Build resolved_ids dict for _substitute_placeholders
            # For JSONB mode, all placeholders reference the same log IDs
            placeholders = _extract_placeholders(template.equation)
            resolved_ids = {}
            for p in placeholders:
                log_key = p.split(":")[0]  # 'log0:field_a' -> 'log0'
                resolved_ids[log_key] = log_ids

            # Substitute placeholders to get filter expression
            filter_expr, alias_to_key_map = _substitute_placeholders(
                template.equation,
                resolved_ids,
            )
            filter_dict = str_filter_exp_to_dict(filter_expr)

            # Compute expression for all log_ids
            computed_values = _compute_expression(
                filter_dict,
                LogEvent,
                self.session,
                log_ids,
            )

            if not computed_values:
                return 0

            # Track non-null value for field type inference
            non_null_val = None
            updates_count = 0

            # Update each log's data JSONB column
            for log_event_id, value in computed_values:
                try:
                    # Serialize value using provided encoder
                    val = json.loads(json.dumps(value, cls=json_encoder))
                    if val is not None:
                        non_null_val = val

                    # Update LogEvent.data by merging the new derived field
                    # Using JSONB concatenation: data || jsonb_build_object('key', value)
                    stmt = (
                        update(LogEvent)
                        .where(LogEvent.id == log_event_id)
                        .values(
                            data=LogEvent.data.concat(
                                func.jsonb_build_object(template.key, val),
                            ),
                            updated_at=datetime.now(timezone.utc),
                        )
                    )
                    await self.session.execute(stmt)
                    updates_count += 1
                except Exception as e:
                    logger.warning(
                        f"Failed to recompute derived log for log_event_id={log_event_id}: {e}",
                    )
                    continue

            await self.session.commit()

            # Create field type if DAO provided
            if field_type_dao and non_null_val is not None:
                try:
                    field_type_dao.create_field_type_if_absent(
                        project_id=template.project_id,
                        field_name=template.key,
                        value=non_null_val,
                        context_id=template.context_id,
                        field_category="derived_entry",
                        infer_type=True,
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to create field type for '{template.key}': {e}",
                    )

            return updates_count

        except Exception as e:
            await self.session.rollback()
            logger.error(f"Error in recompute_derived_logs_jsonb: {e}")
            raise e

    async def update(
        self,
        id: int,
        key: str = None,
        equation: str = None,
    ) -> DerivedLog:
        """Update a derived log entry by ID"""
        try:
            derived_log = await self.session.get(DerivedLog, id)
            if not derived_log:
                raise ValueError(f"No derived log found with id {id}")

            # Check for key conflicts
            if key and key != derived_log.key:
                # Get the associated log_event_id
                log_event_derived_log = (
                    self.session.query(LogEventDerivedLog)
                    .filter_by(derived_log_id=derived_log.id)
                    .first()
                )

                if log_event_derived_log:
                    # Check if another derived log with this key exists for the same log event
                    exists = (
                        self.session.query(DerivedLog)
                        .join(
                            LogEventDerivedLog,
                            LogEventDerivedLog.derived_log_id == DerivedLog.id,
                        )
                        .filter(
                            LogEventDerivedLog.log_event_id
                            == log_event_derived_log.log_event_id,
                            DerivedLog.key == key,
                            DerivedLog.id
                            != derived_log.id,  # Exclude current derived log
                        )
                        .first()
                    )
                    if exists:
                        raise ValueError(
                            f"Key '{key}' already exists for this log event",
                        )

            # Apply updates
            if key:
                derived_log.key = key
            if equation:
                derived_log.equation = equation
            derived_log.updated_at = datetime.now(timezone.utc)

            await self.session.commit()
            return derived_log
        except Exception as e:
            await self.session.rollback()
            raise e

    async def delete(self, id: int) -> None:
        """Delete a derived log entry by ID"""
        try:
            derived_log = await self.session.get(DerivedLog, id)
            if not derived_log:
                raise ValueError(f"No derived log found with id {id}")

            await self.session.delete(derived_log)
            await self.session.commit()
        except Exception as e:
            await self.session.rollback()
            raise e
