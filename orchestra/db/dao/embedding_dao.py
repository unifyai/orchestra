import logging
from typing import List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

ACTIVE_QUEUE_STATUSES = "('pending', 'generating', 'vector_ready', 'inserting')"


class EmbeddingDAO:
    def __init__(self, session: Session):
        self.session = session

    @staticmethod
    def _validate_scope(
        log_event_ids: Optional[List[int]],
        project_id: Optional[int],
    ) -> None:
        if log_event_ids is not None and project_id is not None:
            raise ValueError("Provide exactly one of log_event_ids or project_id")
        if log_event_ids is None and project_id is None:
            raise ValueError("Provide exactly one of log_event_ids or project_id")

    def cancel_queue(
        self,
        *,
        log_event_ids: Optional[List[int]] = None,
        project_id: Optional[int] = None,
        reason: str = "Deleted",
    ) -> int:
        """Cancel active embedding queue items for the given scope.

        Prevents embedding workers from processing items for log events that
        are about to be deleted, avoiding race conditions and FK violations.
        """
        self._validate_scope(log_event_ids, project_id)

        if log_event_ids is not None:
            if not log_event_ids:
                return 0
            result = self.session.execute(
                text(f"""
                    UPDATE embedding_queue
                    SET status = 'cancelled',
                        error_message = :reason
                    WHERE ref_id = ANY(:ids)
                      AND status IN {ACTIVE_QUEUE_STATUSES}
                """),
                {"ids": log_event_ids, "reason": reason},
            )
        else:
            result = self.session.execute(
                text(f"""
                    UPDATE embedding_queue eq
                    SET status = 'cancelled',
                        error_message = :reason
                    FROM log_event le
                    WHERE eq.ref_id = le.id
                      AND le.project_id = :project_id
                      AND eq.status IN {ACTIVE_QUEUE_STATUSES}
                """),
                {"project_id": project_id, "reason": reason},
            )

        return result.rowcount

    def soft_delete(
        self,
        *,
        log_event_ids: Optional[List[int]] = None,
        project_id: Optional[int] = None,
    ) -> int:
        """Soft-delete embeddings (is_deleted=true) for the given scope.

        Marks embeddings as deleted so they are excluded from HNSW similarity
        searches immediately, without expensive index surgery.
        """
        self._validate_scope(log_event_ids, project_id)

        if log_event_ids is not None:
            if not log_event_ids:
                return 0
            result = self.session.execute(
                text("""
                    UPDATE embedding
                    SET is_deleted = true
                    WHERE ref_id = ANY(:ids)
                      AND is_deleted = false
                """),
                {"ids": log_event_ids},
            )
        else:
            result = self.session.execute(
                text("""
                    UPDATE embedding e
                    SET is_deleted = true
                    FROM log_event le
                    WHERE e.ref_id = le.id
                      AND le.project_id = :project_id
                      AND e.is_deleted = false
                """),
                {"project_id": project_id},
            )

        return result.rowcount

    def null_ref_ids(
        self,
        *,
        log_event_ids: Optional[List[int]] = None,
        project_id: Optional[int] = None,
    ) -> int:
        """Null out embedding ref_ids in bulk for the given scope.

        Must be called BEFORE hard-deleting log events. This prevents the
        per-row FK SET NULL trigger from firing during deletion, which would
        cause massive overhead (index updates on the embedding table per row).
        """
        self._validate_scope(log_event_ids, project_id)

        if log_event_ids is not None:
            if not log_event_ids:
                return 0
            result = self.session.execute(
                text("""
                    UPDATE embedding
                    SET ref_id = NULL
                    WHERE ref_id = ANY(:ids)
                """),
                {"ids": log_event_ids},
            )
        else:
            result = self.session.execute(
                text("""
                    UPDATE embedding e
                    SET ref_id = NULL
                    FROM log_event le
                    WHERE e.ref_id = le.id
                      AND le.project_id = :project_id
                """),
                {"project_id": project_id},
            )

        return result.rowcount
