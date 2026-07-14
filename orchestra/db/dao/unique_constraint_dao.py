"""Data Access Object for unique field constraint validation.

This DAO provides efficient O(M×log N) unique field validation using a lookup table,
replacing the previous O(N×M) JSONB containment scan approach.

Supports:
- Single unique fields: field_name = 'row_id', value_hash = md5(value)
- Composite keys: field_name = '__composite__', value_hash = md5(json(combo))

Migration Strategy:
- Controlled by ORCHESTRA_UNIQUE_VALIDATION_MODE env var
- Default: jsonb_scan (old behavior) - for backward compatibility
- After backfill: lookup_table (new behavior) - for performance
"""

import hashlib
import json
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from orchestra.db.models.core_models import LogUniqueConstraint
from orchestra.db.scope import single_owner_key_for_context
from orchestra.settings import UniqueValidationMode, settings

logger = logging.getLogger(__name__)

# Field name used for composite key constraints
COMPOSITE_KEY_FIELD = "__composite__"


class UniqueConstraintDAO:
    """DAO for managing unique field constraints using a lookup table."""

    def __init__(self, session: Session):
        """Initialize the DAO with a database session."""
        self.session = session

    @staticmethod
    def hash_value(value: Any) -> str:
        """
        Create a deterministic hash for any value.

        Uses MD5 for speed (not security) - produces fixed 32-char hex string.

        :param value: Any JSON-serializable value.
        :return: MD5 hex digest of the JSON-serialized value.
        """
        if value is None:
            return "__null__"
        serialized = json.dumps(value, sort_keys=True, default=str)
        return hashlib.md5(serialized.encode()).hexdigest()

    @staticmethod
    def hash_composite(values: Dict[str, Any], key_order: List[str]) -> str:
        """
        Create a deterministic hash for a composite key.

        :param values: Dictionary of field values.
        :param key_order: Ordered list of field names in the composite key.
        :return: MD5 hex digest of the ordered values.
        """
        ordered = {k: values.get(k) for k in key_order}
        return UniqueConstraintDAO.hash_value(ordered)

    def _use_lookup_table(self) -> bool:
        """Check if lookup table mode is enabled."""
        return settings.unique_validation_mode == UniqueValidationMode.LOOKUP_TABLE

    def _project_id_for_context(
        self,
        context_id: int,
        project_id: Optional[int] = None,
    ) -> int:
        """Resolve the denormalized project_id for uniqueness rows."""
        if project_id is not None:
            return int(project_id)
        pid = self.session.execute(
            text("SELECT project_id FROM context WHERE id = :cid"),
            {"cid": context_id},
        ).scalar()
        if pid is None:
            raise ValueError(f"Unknown context_id={context_id}")
        return int(pid)

    @staticmethod
    def _constraint_row(
        *,
        context_id: int,
        project_id: int,
        field_name: str,
        value_hash: str,
        log_event_id: int,
    ) -> Dict[str, Any]:
        return {
            "context_id": context_id,
            "project_id": project_id,
            "field_name": field_name,
            "value_hash": value_hash,
            "log_event_id": log_event_id,
        }

    def check_unique_fields_batch(
        self,
        context_id: int,
        project_id: int,
        log_entries: List[Tuple[int, Dict[str, Any]]],
        unique_fields: Set[str],
        exclude_ids: Optional[List[int]] = None,
    ) -> Optional[Tuple[int, str, Any]]:
        """
        Check unique field constraints for a batch of log entries.

        This is the main entry point for unique field validation. It:
        1. Checks for duplicates within the batch itself
        2. Checks against existing data (using lookup table or JSONB scan)
        3. Inserts constraints for new entries (if using lookup table)

        :param context_id: Context ID for the logs.
        :param project_id: Project ID for the logs.
        :param log_entries: List of (log_event_id, log_data) tuples.
        :param unique_fields: Set of field names that must be unique.
        :param exclude_ids: Log event IDs to exclude from duplicate check.
        :return: (log_event_id, field_name, value) of first duplicate, or None.
        """
        if not log_entries or not unique_fields:
            return None

        exclude_ids = exclude_ids or []
        resolved_project_id = self._project_id_for_context(context_id, project_id)

        # Step 1: Check for duplicates within the batch
        seen: Dict[Tuple[str, str], int] = {}  # (field_name, value_hash) -> log_id
        entries_to_check: List[Dict] = []

        for log_event_id, log_data in log_entries:
            for field_name in unique_fields:
                if field_name not in log_data:
                    continue
                value = log_data[field_name]
                if value is None:
                    continue

                value_hash = self.hash_value(value)
                key = (field_name, value_hash)

                # Check within batch
                if key in seen and seen[key] != log_event_id:
                    return (log_event_id, field_name, value)
                seen[key] = log_event_id

                entries_to_check.append(
                    {
                        "context_id": context_id,
                        "project_id": resolved_project_id,
                        "field_name": field_name,
                        "value_hash": value_hash,
                        "log_event_id": log_event_id,
                        "value": value,  # Keep original for error message
                    },
                )

        if not entries_to_check:
            return None

        # Step 2: Check against existing data
        if self._use_lookup_table():
            duplicate = self._check_via_lookup_table(entries_to_check, exclude_ids)
            if duplicate:
                return duplicate
            # Step 3: Insert constraints for new entries
            self._insert_constraints(entries_to_check)
        else:
            duplicate = self._check_via_jsonb_scan(
                context_id,
                project_id,
                entries_to_check,
                exclude_ids,
            )
            if duplicate:
                return duplicate

        return None

    def _check_via_lookup_table(
        self,
        entries: List[Dict],
        exclude_ids: List[int],
    ) -> Optional[Tuple[int, str, Any]]:
        """
        Check for duplicates using the lookup table (fast path).

        Uses INSERT ... ON CONFLICT DO NOTHING with RETURNING to atomically
        check and insert in a single query.

        :param entries: List of constraint entries to check.
        :param exclude_ids: Log event IDs to exclude.
        :return: First duplicate found, or None.
        """
        if not entries:
            return None

        # Build values for batch insert
        values = [
            self._constraint_row(
                context_id=e["context_id"],
                project_id=e["project_id"],
                field_name=e["field_name"],
                value_hash=e["value_hash"],
                log_event_id=e["log_event_id"],
            )
            for e in entries
        ]

        # Use INSERT ... ON CONFLICT DO NOTHING with RETURNING
        # Rows that conflict (duplicates) won't be returned
        stmt = insert(LogUniqueConstraint).values(values)
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["context_id", "field_name", "value_hash"],
        )
        stmt = stmt.returning(
            LogUniqueConstraint.context_id,
            LogUniqueConstraint.field_name,
            LogUniqueConstraint.value_hash,
        )

        result = self.session.execute(stmt)
        inserted_keys = {
            (row.context_id, row.field_name, row.value_hash)
            for row in result.fetchall()
        }

        # Find which entries were NOT inserted (duplicates)
        for entry in entries:
            key = (entry["context_id"], entry["field_name"], entry["value_hash"])
            if key not in inserted_keys:
                # This is a duplicate - but check if it's our own entry
                existing = self.session.execute(
                    text(
                        """
                        SELECT log_event_id FROM log_unique_constraint
                        WHERE context_id = :context_id
                          AND field_name = :field_name
                          AND value_hash = :value_hash
                    """,
                    ),
                    {
                        "context_id": entry["context_id"],
                        "field_name": entry["field_name"],
                        "value_hash": entry["value_hash"],
                    },
                ).fetchone()

                if existing and existing.log_event_id not in exclude_ids:
                    if existing.log_event_id != entry["log_event_id"]:
                        return (
                            entry["log_event_id"],
                            entry["field_name"],
                            entry["value"],
                        )

        return None

    def _check_via_jsonb_scan(
        self,
        context_id: int,
        project_id: int,
        entries: List[Dict],
        exclude_ids: List[int],
    ) -> Optional[Tuple[int, str, Any]]:
        """
        Check for duplicates using JSONB containment scan (slow path).

        This is the fallback method used when lookup table is not enabled
        or not yet backfilled.

        :param context_id: Context ID.
        :param project_id: Project ID.
        :param entries: List of constraint entries to check.
        :param exclude_ids: Log event IDs to exclude.
        :return: First duplicate found, or None.
        """
        if not entries:
            return None

        # Build OR conditions for JSONB containment check
        params: Dict[str, Any] = {
            "context_id": context_id,
            "project_id": project_id,
            "exclude_ids": exclude_ids or [],
        }

        or_conditions = []
        case_parts = []

        for i, entry in enumerate(entries):
            value_json = json.dumps({entry["field_name"]: entry["value"]})
            params[f"value_{i}"] = value_json
            or_conditions.append(f"le.data @> CAST(:value_{i} AS jsonb)")
            case_parts.append(f"WHEN le.data @> CAST(:value_{i} AS jsonb) THEN {i}")

        owner_key_filter = single_owner_key_for_context(self.session, context_id)
        owner_clause = ""
        if owner_key_filter is not None:
            owner_clause = (
                " AND le.owner_key = :owner_key AND lec.owner_key = :owner_key "
            )
            params["owner_key"] = owner_key_filter

        jsonb_query = f"""
            SELECT DISTINCT CASE {' '.join(case_parts)} END AS match_idx
            FROM log_event le
            JOIN log_event_context lec ON le.id = lec.log_event_id
                AND lec.project_id = le.project_id
            WHERE le.project_id = :project_id
            {owner_clause}
            AND lec.context_id = :context_id
            AND le.id != ALL(:exclude_ids)
            AND ({' OR '.join(or_conditions)})
        """

        results = self.session.execute(text(jsonb_query), params).fetchall()

        for (match_idx,) in results:
            if match_idx is not None:
                entry = entries[match_idx]
                return (entry["log_event_id"], entry["field_name"], entry["value"])

        return None

    def _insert_constraints(self, entries: List[Dict]) -> None:
        """
        Insert constraint records for successfully validated entries.

        Uses INSERT ... ON CONFLICT DO NOTHING to handle race conditions.

        :param entries: List of constraint entries to insert.
        """
        if not entries:
            return

        values = [
            self._constraint_row(
                context_id=e["context_id"],
                project_id=e["project_id"],
                field_name=e["field_name"],
                value_hash=e["value_hash"],
                log_event_id=e["log_event_id"],
            )
            for e in entries
        ]

        stmt = insert(LogUniqueConstraint).values(values)
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["context_id", "field_name", "value_hash"],
        )
        self.session.execute(stmt)

    def check_composite_keys_batch(
        self,
        context_id: int,
        log_entries: List[Tuple[int, Dict[str, Any]]],
        key_columns: List[str],
        project_id: Optional[int] = None,
    ) -> Optional[Tuple[int, Dict[str, Any]]]:
        """
        Check composite key constraints for a batch of log entries.

        :param context_id: Context ID for the logs.
        :param log_entries: List of (log_event_id, row_data) tuples.
        :param key_columns: Ordered list of columns forming the composite key.
        :param project_id: Optional project id (resolved from context when omitted).
        :return: (log_event_id, key_values) of first duplicate, or None.
        """
        if not log_entries or not key_columns:
            return None

        resolved_project_id = self._project_id_for_context(context_id, project_id)

        # Step 1: Check for duplicates within the batch
        seen: Dict[str, int] = {}  # value_hash -> log_id
        entries_to_check: List[Dict] = []

        for log_event_id, row_data in log_entries:
            key_values = {col: row_data.get(col) for col in key_columns}

            # Validate all key columns have values
            if None in key_values.values():
                raise ValueError("Composite key columns must all have values.")

            value_hash = self.hash_composite(key_values, key_columns)

            # Check within batch
            if value_hash in seen:
                return (log_event_id, key_values)
            seen[value_hash] = log_event_id

            entries_to_check.append(
                {
                    "context_id": context_id,
                    "project_id": resolved_project_id,
                    "field_name": COMPOSITE_KEY_FIELD,
                    "value_hash": value_hash,
                    "log_event_id": log_event_id,
                    "key_values": key_values,  # Keep for error message
                },
            )

        if not entries_to_check:
            return None

        # Step 2: Check against existing data
        if self._use_lookup_table():
            duplicate = self._check_composite_via_lookup(entries_to_check)
            if duplicate:
                return duplicate
            # Step 3: Insert constraints
            self._insert_composite_constraints(entries_to_check)
        else:
            duplicate = self._check_composite_via_jsonb(
                context_id,
                entries_to_check,
                key_columns,
            )
            if duplicate:
                return duplicate

        return None

    def _check_composite_via_lookup(
        self,
        entries: List[Dict],
    ) -> Optional[Tuple[int, Dict[str, Any]]]:
        """
        Check composite key duplicates using lookup table.

        :param entries: List of composite key entries to check.
        :return: First duplicate found, or None.
        """
        if not entries:
            return None

        values = [
            self._constraint_row(
                context_id=e["context_id"],
                project_id=e["project_id"],
                field_name=e["field_name"],
                value_hash=e["value_hash"],
                log_event_id=e["log_event_id"],
            )
            for e in entries
        ]

        stmt = insert(LogUniqueConstraint).values(values)
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["context_id", "field_name", "value_hash"],
        )
        stmt = stmt.returning(
            LogUniqueConstraint.context_id,
            LogUniqueConstraint.field_name,
            LogUniqueConstraint.value_hash,
        )

        result = self.session.execute(stmt)
        inserted_keys = {
            (row.context_id, row.field_name, row.value_hash)
            for row in result.fetchall()
        }

        for entry in entries:
            key = (entry["context_id"], entry["field_name"], entry["value_hash"])
            if key not in inserted_keys:
                # Check if it's a different log event
                existing = self.session.execute(
                    text(
                        """
                        SELECT log_event_id FROM log_unique_constraint
                        WHERE context_id = :context_id
                          AND field_name = :field_name
                          AND value_hash = :value_hash
                    """,
                    ),
                    {
                        "context_id": entry["context_id"],
                        "field_name": entry["field_name"],
                        "value_hash": entry["value_hash"],
                    },
                ).fetchone()

                if existing and existing.log_event_id != entry["log_event_id"]:
                    return (entry["log_event_id"], entry["key_values"])

        return None

    def _check_composite_via_jsonb(
        self,
        context_id: int,
        entries: List[Dict],
        key_columns: List[str],
    ) -> Optional[Tuple[int, Dict[str, Any]]]:
        """
        Check composite key duplicates using JSONB containment scan.

        :param context_id: Context ID.
        :param entries: List of composite key entries to check.
        :param key_columns: Ordered list of key column names.
        :return: First duplicate found, or None.
        """
        if not entries:
            return None

        # Build OR conditions for each composite key
        params: Dict[str, Any] = {"context_id": context_id}
        or_conditions = []

        for i, entry in enumerate(entries):
            key_values = entry["key_values"]
            value_json = json.dumps(key_values)
            params[f"combo_{i}"] = value_json
            or_conditions.append(f"le.data @> CAST(:combo_{i} AS jsonb)")

        # Redundant project_id predicate (derived from the context) prunes the
        # LIST(project_id) partitions; guarded so a missing project never hides a
        # real duplicate.
        pid = self.session.execute(
            text("SELECT project_id FROM context WHERE id = :cid"),
            {"cid": context_id},
        ).scalar()
        project_filter = "AND le.project_id = :project_id\n" if pid is not None else ""
        if pid is not None:
            params["project_id"] = pid

        # Owner sub-partition pruning for single-owner (assistant/team) contexts.
        owner_key_filter = single_owner_key_for_context(self.session, context_id)
        owner_filter = ""
        if owner_key_filter is not None:
            owner_filter = (
                "AND le.owner_key = :owner_key AND lec.owner_key = :owner_key\n"
            )
            params["owner_key"] = owner_key_filter

        query = f"""
            SELECT le.id
            FROM log_event le
            JOIN log_event_context lec ON lec.log_event_id = le.id
            AND le.project_id = lec.project_id
            WHERE lec.context_id = :context_id
            {project_filter}{owner_filter}AND ({' OR '.join(or_conditions)})
            LIMIT 1
        """

        result = self.session.execute(text(query), params).fetchone()
        if result:
            # Find which entry matched
            for entry in entries:
                return (entry["log_event_id"], entry["key_values"])

        return None

    def _insert_composite_constraints(self, entries: List[Dict]) -> None:
        """
        Insert composite key constraint records.

        :param entries: List of composite key entries to insert.
        """
        if not entries:
            return

        values = [
            self._constraint_row(
                context_id=e["context_id"],
                project_id=e["project_id"],
                field_name=e["field_name"],
                value_hash=e["value_hash"],
                log_event_id=e["log_event_id"],
            )
            for e in entries
        ]

        stmt = insert(LogUniqueConstraint).values(values)
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["context_id", "field_name", "value_hash"],
        )
        self.session.execute(stmt)

    def remove_constraints_for_logs(
        self,
        log_event_ids: List[int],
        *,
        context_id: Optional[int] = None,
        project_id: Optional[int] = None,
    ) -> int:
        """
        Remove constraints for the given log event IDs.

        Prefer passing ``context_id`` and/or ``project_id`` so the DELETE hits
        the (context_id, log_event_id) / (project_id, log_event_id) indexes
        instead of scanning the global log_event_id index under concurrent
        INSERT … ON CONFLICT traffic.

        :param log_event_ids: List of log event IDs to remove constraints for.
        :param context_id: Optional context scope for the delete.
        :param project_id: Optional project scope for the delete.
        :return: Number of constraints removed.
        """
        if not log_event_ids:
            return 0

        clauses = ["log_event_id = ANY(:log_ids)"]
        params: Dict[str, Any] = {"log_ids": log_event_ids}
        if context_id is not None:
            clauses.append("context_id = :context_id")
            params["context_id"] = context_id
        if project_id is not None:
            clauses.append("project_id = :project_id")
            params["project_id"] = project_id

        result = self.session.execute(
            text(
                f"""
                DELETE FROM log_unique_constraint
                WHERE {' AND '.join(clauses)}
                """,
            ),
            params,
        )
        return result.rowcount

    def update_constraint(
        self,
        context_id: int,
        log_event_id: int,
        field_name: str,
        old_value: Any,
        new_value: Any,
        project_id: Optional[int] = None,
    ) -> Optional[str]:
        """
        Update a constraint when a unique field value changes.

        :param context_id: Context ID.
        :param log_event_id: Log event ID being updated.
        :param field_name: Name of the unique field.
        :param old_value: Previous value (to delete old constraint).
        :param new_value: New value (to insert new constraint).
        :param project_id: Optional project id (resolved from context when omitted).
        :return: Error message if duplicate found, None on success.
        """
        if not self._use_lookup_table():
            return None  # Handled by JSONB scan in caller

        old_hash = self.hash_value(old_value)
        new_hash = self.hash_value(new_value)

        if old_hash == new_hash:
            return None  # No change

        resolved_project_id = self._project_id_for_context(context_id, project_id)

        # Delete old constraint
        self.session.execute(
            text(
                """
                DELETE FROM log_unique_constraint
                WHERE context_id = :context_id
                  AND field_name = :field_name
                  AND value_hash = :value_hash
                  AND log_event_id = :log_event_id
            """,
            ),
            {
                "context_id": context_id,
                "field_name": field_name,
                "value_hash": old_hash,
                "log_event_id": log_event_id,
            },
        )

        # Try to insert new constraint
        stmt = insert(LogUniqueConstraint).values(
            context_id=context_id,
            project_id=resolved_project_id,
            field_name=field_name,
            value_hash=new_hash,
            log_event_id=log_event_id,
        )
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["context_id", "field_name", "value_hash"],
        )
        stmt = stmt.returning(LogUniqueConstraint.log_event_id)

        result = self.session.execute(stmt).fetchone()
        if result is None:
            # Conflict - duplicate exists
            return f"Duplicate entry for unique field '{field_name}'."

        return None

    def find_composite_duplicate_indices(
        self,
        context_id: int,
        key_columns: List[str],
        rows: List[Dict[str, Any]],
    ) -> Dict[int, Dict[str, Any]]:
        """Return indices that collide within ``rows`` or already exist in context.

        First occurrence of a composite key in ``rows`` wins. Does not insert
        constraint rows — check-only for ``on_duplicate=skip`` pre-filtering.

        :return: ``{index: key_values}`` for rows that should be skipped.
        """
        if not rows or not key_columns:
            return {}

        skipped: Dict[int, Dict[str, Any]] = {}
        seen_hashes: Dict[str, int] = {}
        index_by_hash: Dict[str, int] = {}
        hashes_in_order: List[str] = []

        for index, row in enumerate(rows):
            key_values = {col: row.get(col) for col in key_columns}
            if None in key_values.values():
                continue
            value_hash = self.hash_composite(key_values, key_columns)
            if value_hash in seen_hashes:
                skipped[index] = key_values
                continue
            seen_hashes[value_hash] = index
            index_by_hash[value_hash] = index
            hashes_in_order.append(value_hash)

        if not hashes_in_order:
            return skipped

        existing_hashes = self._existing_composite_hashes(
            context_id=context_id,
            key_columns=key_columns,
            rows=rows,
            candidate_hashes=hashes_in_order,
            index_by_hash=index_by_hash,
        )
        for value_hash in existing_hashes:
            index = index_by_hash[value_hash]
            if index not in skipped:
                skipped[index] = {col: rows[index].get(col) for col in key_columns}

        return skipped

    def _existing_composite_hashes(
        self,
        *,
        context_id: int,
        key_columns: List[str],
        rows: List[Dict[str, Any]],
        candidate_hashes: List[str],
        index_by_hash: Dict[str, int],
    ) -> Set[str]:
        """Return subset of ``candidate_hashes`` already present in the context."""
        if self._use_lookup_table():
            result = self.session.execute(
                text(
                    """
                    SELECT value_hash FROM log_unique_constraint
                    WHERE context_id = :context_id
                      AND field_name = :field_name
                      AND value_hash = ANY(:hashes)
                    """,
                ),
                {
                    "context_id": context_id,
                    "field_name": COMPOSITE_KEY_FIELD,
                    "hashes": candidate_hashes,
                },
            )
            return {row.value_hash for row in result.fetchall()}

        # JSONB scan: find which candidate key combos already exist.
        params: Dict[str, Any] = {"context_id": context_id}
        or_conditions = []
        hash_by_param: Dict[str, str] = {}
        for i, value_hash in enumerate(candidate_hashes):
            index = index_by_hash[value_hash]
            key_values = {col: rows[index].get(col) for col in key_columns}
            param = f"combo_{i}"
            params[param] = json.dumps(key_values)
            hash_by_param[param] = value_hash
            or_conditions.append(f"le.data @> CAST(:{param} AS jsonb)")

        pid = self.session.execute(
            text("SELECT project_id FROM context WHERE id = :cid"),
            {"cid": context_id},
        ).scalar()
        project_filter = ""
        if pid is not None:
            project_filter = "AND le.project_id = :project_id\n"
            params["project_id"] = pid

        owner_key_filter = single_owner_key_for_context(self.session, context_id)
        owner_filter = ""
        if owner_key_filter is not None:
            owner_filter = (
                "AND le.owner_key = :owner_key AND lec.owner_key = :owner_key\n"
            )
            params["owner_key"] = owner_key_filter

        query = f"""
            SELECT le.data
            FROM log_event le
            JOIN log_event_context lec ON lec.log_event_id = le.id
              AND le.project_id = lec.project_id
            WHERE lec.context_id = :context_id
            {project_filter}{owner_filter}AND ({' OR '.join(or_conditions)})
        """
        existing: Set[str] = set()
        for row in self.session.execute(text(query), params).fetchall():
            data = row.data or {}
            combo = {col: data.get(col) for col in key_columns}
            existing.add(self.hash_composite(combo, key_columns))
        return existing & set(candidate_hashes)

    def find_unique_field_conflict_log_ids(
        self,
        context_id: int,
        project_id: int,
        log_entries: List[Tuple[int, Dict[str, Any]]],
        unique_fields: Set[str],
        exclude_ids: Optional[List[int]] = None,
    ) -> Dict[int, Tuple[str, Any]]:
        """Return ``{log_event_id: (field_name, value)}`` for unique-field clashes.

        Check-only (no constraint inserts). Within-batch: first log wins.
        """
        if not log_entries or not unique_fields:
            return {}

        exclude_ids = exclude_ids or []
        conflicts: Dict[int, Tuple[str, Any]] = {}
        seen: Dict[Tuple[str, str], int] = {}
        entries_to_check: List[Dict] = []

        for log_event_id, log_data in log_entries:
            for field_name in unique_fields:
                if field_name not in log_data:
                    continue
                value = log_data[field_name]
                if value is None:
                    continue
                value_hash = self.hash_value(value)
                key = (field_name, value_hash)
                if key in seen and seen[key] != log_event_id:
                    conflicts[log_event_id] = (field_name, value)
                    continue
                seen[key] = log_event_id
                entries_to_check.append(
                    {
                        "context_id": context_id,
                        "field_name": field_name,
                        "value_hash": value_hash,
                        "log_event_id": log_event_id,
                        "value": value,
                    },
                )

        if not entries_to_check:
            return conflicts

        if self._use_lookup_table():
            for entry in entries_to_check:
                if entry["log_event_id"] in conflicts:
                    continue
                existing = self.session.execute(
                    text(
                        """
                        SELECT log_event_id FROM log_unique_constraint
                        WHERE context_id = :context_id
                          AND field_name = :field_name
                          AND value_hash = :value_hash
                          AND NOT (log_event_id = ANY(:exclude_ids))
                        LIMIT 1
                        """,
                    ),
                    {
                        "context_id": entry["context_id"],
                        "field_name": entry["field_name"],
                        "value_hash": entry["value_hash"],
                        "exclude_ids": exclude_ids or [0],
                    },
                ).fetchone()
                if existing and existing.log_event_id != entry["log_event_id"]:
                    conflicts[entry["log_event_id"]] = (
                        entry["field_name"],
                        entry["value"],
                    )
        else:
            duplicate = self._check_via_jsonb_scan(
                context_id,
                project_id,
                entries_to_check,
                exclude_ids,
            )
            # jsonb helper returns first only — walk remaining by filtering
            remaining = list(entries_to_check)
            while duplicate:
                dup_id, field_name, value = duplicate
                conflicts[dup_id] = (field_name, value)
                remaining = [e for e in remaining if e["log_event_id"] != dup_id]
                if not remaining:
                    break
                duplicate = self._check_via_jsonb_scan(
                    context_id,
                    project_id,
                    remaining,
                    exclude_ids,
                )

        return conflicts
