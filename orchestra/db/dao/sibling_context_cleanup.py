"""
Shared sibling context cleanup logic for Assistants/UnityTests projects.

Handles the 3-tier context hierarchy used in Assistants projects:
- Tier 1: All/<SubContext> (global aggregate) - PROTECTED ARCHIVE
- Tier 2: <User>/All/<SubContext> (user aggregate)
- Tier 3: <User>/<Assistant>/<SubContext> (user + assistant specific)

When deleting logs/contexts from one tier, the same logs should be
removed from sibling tiers to maintain consistency.

ARCHIVE PROTECTION:
- Topmost archive contexts (All/*) are protected from cascading deletions
  originating from lower-tier contexts (Tier 2 or Tier 3).
- This preserves historical data for billing and reporting.
- Deleting from All/* itself still cascades to lower tiers normally.
- Intermediate contexts (*/All/*) are NOT protected.
"""

from typing import TYPE_CHECKING, Dict, List, Optional

from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import Context, LogEvent, LogEventContext

if TYPE_CHECKING:
    from orchestra.db.dao.context_dao import ContextDAO


def get_assistants_sibling_context_info(
    session: Session,
    project_id: int,
    context_id: int,
    context_name: str,
    log_event_ids: List[int],
    context_dao: "ContextDAO",
) -> Dict[int, List[int]]:
    """
    For Assistants/UnityTests project, find sibling context IDs for each log event.

    Uses a 3-tier context hierarchy:
    - Tier 1: "<prefix>/All/<SubContext>" (global aggregate)
    - Tier 2: "<prefix>/<User>/All/<SubContext>" (user aggregate)
    - Tier 3: "<prefix>/<User>/<Assistant>/<SubContext>" (user + assistant specific)

    Both <prefix> and <SubContext> can have arbitrary depth. We determine them
    dynamically by finding each log's Tier 1 context and locating "All" within it.
    The Tier 1 context is identified as the shortest context containing "All"
    that each log belongs to.

    Deletion cascade rules:
    - From any tier: delete from the other two tiers

    Uses "_user" and "_assistant" fields from logs to construct sibling paths.

    Args:
        session: Database session
        project_id: Project ID
        context_id: Current context ID being deleted from
        context_name: Name of the current context
        log_event_ids: List of log event IDs being deleted
        context_dao: Context DAO instance

    Returns:
        Dict mapping log_event_id to list of sibling context_ids.
        Empty dict if no sibling contexts found.
    """
    if not log_event_ids or not context_name:
        return {}

    sibling_map: Dict[int, List[int]] = {}

    def _add_sibling(log_id: int, ctx_id: int):
        """Helper to add a sibling context ID to the map."""
        if log_id not in sibling_map:
            sibling_map[log_id] = []
        if ctx_id not in sibling_map[log_id]:
            sibling_map[log_id].append(ctx_id)

    def _is_topmost_archive(name: str) -> bool:
        """Check if context is a topmost archive (All/* only, NOT */All/*).

        Topmost archives are protected from cascading deletions originating
        from lower-tier contexts.
        """
        return name.startswith("All/")

    def _get_log_field_values(field_name: str) -> Dict[int, str]:
        """Get field values for all log events from LogEvent.data JSONB column.

        Returns:
            Dict mapping log_event_id to field value string.
        """
        values = (
            session.query(
                LogEvent.id,
                LogEvent.data[field_name].astext,
            )
            .filter(
                LogEvent.id.in_(log_event_ids),
                LogEvent.data.has_key(field_name),
            )
            .all()
        )

        result = {}
        for log_event_id, value in values:
            if value:
                if isinstance(value, str):
                    value = value.strip('"')
                result[log_event_id] = value
        return result

    def _find_context_id(ctx_name: str) -> Optional[int]:
        """Find context by name and return its ID if it exists."""
        ctx = context_dao.filter(project_id=project_id, name=ctx_name)
        if ctx:
            return ctx[0][0].id
        return None

    def _verify_logs_in_context(ctx_id: int, logs: List[int]) -> List[int]:
        """Verify which logs exist in the given context."""
        existing = (
            session.query(LogEventContext.log_event_id)
            .filter(
                LogEventContext.log_event_id.in_(logs),
                LogEventContext.context_id == ctx_id,
            )
            .all()
        )
        return [log_id for (log_id,) in existing]

    def _get_tier1_context_for_logs() -> Dict[int, str]:
        """Find the Tier 1 context name for each log.

        Tier 1 is identified as the SHORTEST context containing "All" that
        each log belongs to. This works because Tier 2 adds a User component,
        making it longer than Tier 1 for the same prefix/SubContext.

        Returns:
            Dict mapping log_event_id to its Tier 1 context name.
        """
        # Query all contexts that contain these logs
        log_contexts = (
            session.query(LogEventContext.log_event_id, Context.name)
            .join(Context, Context.id == LogEventContext.context_id)
            .filter(
                LogEventContext.log_event_id.in_(log_event_ids),
                Context.project_id == project_id,
            )
            .all()
        )

        # For each log, find its Tier 1 context (shortest one containing "All")
        result: Dict[int, str] = {}
        for log_id, ctx_name in log_contexts:
            if "/All/" not in ctx_name and not ctx_name.startswith("All/"):
                # No "All" in this context - not an aggregation context
                continue

            if log_id not in result or len(ctx_name) < len(result[log_id]):
                # First match or shorter match - this is more likely Tier 1
                result[log_id] = ctx_name

        return result

    def _parse_tier1_context(tier1_ctx: str) -> tuple:
        """Parse a Tier 1 context into (prefix, sub_context).

        Args:
            tier1_ctx: Context name like "<prefix>/All/<SubContext>"

        Returns:
            Tuple of (prefix, sub_context) where prefix may be empty string.
        """
        parts = tier1_ctx.split("/")
        try:
            all_idx = parts.index("All")
            prefix = "/".join(parts[:all_idx]) if all_idx > 0 else ""
            sub_context = (
                "/".join(parts[all_idx + 1 :]) if all_idx < len(parts) - 1 else ""
            )
            return (prefix, sub_context)
        except ValueError:
            return ("", "")

    # Step 1: Find Tier 1 context for each log to determine prefix/SubContext
    tier1_contexts = _get_tier1_context_for_logs()

    if not tier1_contexts:
        return {}

    # Step 2: Get _user and _assistant fields for all logs
    user_values = _get_log_field_values("_user")
    assistant_values = _get_log_field_values("_assistant")

    # Step 3: For each log, construct and find sibling contexts
    for log_id in log_event_ids:
        tier1_ctx = tier1_contexts.get(log_id)
        if not tier1_ctx:
            continue

        prefix, sub_context = _parse_tier1_context(tier1_ctx)
        if not sub_context:
            continue

        user_ctx = user_values.get(log_id)
        assistant_ctx = assistant_values.get(log_id)

        # Construct all three tier context names
        if prefix:
            tier1_name = f"{prefix}/All/{sub_context}"
            tier2_name = f"{prefix}/{user_ctx}/All/{sub_context}" if user_ctx else None
            tier3_name = (
                f"{prefix}/{user_ctx}/{assistant_ctx}/{sub_context}"
                if user_ctx and assistant_ctx
                else None
            )
        else:
            tier1_name = f"All/{sub_context}"
            tier2_name = f"{user_ctx}/All/{sub_context}" if user_ctx else None
            tier3_name = (
                f"{user_ctx}/{assistant_ctx}/{sub_context}"
                if user_ctx and assistant_ctx
                else None
            )

        # Determine if current context is a topmost archive
        current_is_archive = _is_topmost_archive(context_name)

        # Find sibling contexts (excluding the current context)
        for sibling_name in [tier1_name, tier2_name, tier3_name]:
            if sibling_name and sibling_name != context_name:
                # ARCHIVE PROTECTION: When deleting from a non-archive context,
                # skip cascade to topmost archive (All/*) contexts.
                # This preserves historical data in the archive.
                sibling_is_archive = _is_topmost_archive(sibling_name)
                if not current_is_archive and sibling_is_archive:
                    continue

                ctx_id = _find_context_id(sibling_name)
                if ctx_id and ctx_id != context_id:
                    # Verify log actually exists in this context before adding
                    if _verify_logs_in_context(ctx_id, [log_id]):
                        _add_sibling(log_id, ctx_id)

    return sibling_map


def remove_logs_from_sibling_contexts(
    session: Session,
    sibling_context_map: Dict[int, List[int]],
) -> int:
    """
    Remove log associations from sibling contexts.

    Args:
        session: Database session
        sibling_context_map: Dict mapping log_event_id to list of sibling context_ids

    Returns:
        Number of associations removed.
    """
    removed = 0
    for log_id, sibling_ctx_ids in sibling_context_map.items():
        for sibling_ctx_id in sibling_ctx_ids:
            deleted = (
                session.query(LogEventContext)
                .filter(
                    LogEventContext.log_event_id == log_id,
                    LogEventContext.context_id == sibling_ctx_id,
                )
                .delete(synchronize_session=False)
            )
            removed += deleted
    return removed
