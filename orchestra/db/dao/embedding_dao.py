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
        if log_event_ids is None and project_id is None:
            raise ValueError("Provide log_event_ids and/or project_id")

    @staticmethod
    def _prune_clause(project_id: Optional[int], column: str = "project_id") -> str:
        """Optional partition-pruning predicate for the log_event_ids paths.

        The heavy tables are LIST(project_id)-partitioned; a ``ref_id``/``id``
        predicate alone cannot prune partitions. When the owning ``project_id``
        is known it is added purely as a pruning filter -- the targeted rows all
        belong to that project, so the matched set is unchanged.
        """
        return f"AND {column} = :pid " if project_id is not None else ""

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
            params = {"ids": log_event_ids, "reason": reason}
            if project_id is not None:
                params["pid"] = project_id
            result = self.session.execute(
                text(
                    f"""
                    UPDATE embedding_queue
                    SET status = 'cancelled',
                        error_message = :reason
                    WHERE ref_id = ANY(:ids)
                      {self._prune_clause(project_id)}AND status IN {ACTIVE_QUEUE_STATUSES}
                """,
                ),
                params,
            )
        else:
            result = self.session.execute(
                text(
                    f"""
                    UPDATE embedding_queue eq
                    SET status = 'cancelled',
                        error_message = :reason
                    FROM log_event le
                    WHERE eq.ref_id = le.id
                      AND eq.project_id = :project_id
                      AND le.project_id = :project_id
                      AND eq.status IN {ACTIVE_QUEUE_STATUSES}
                """,
                ),
                {"project_id": project_id, "reason": reason},
            )

        return result.rowcount

    SOFT_DELETE_BATCH_SIZE = 5000

    def soft_delete(
        self,
        *,
        log_event_ids: Optional[List[int]] = None,
        project_id: Optional[int] = None,
        batch_size: int = SOFT_DELETE_BATCH_SIZE,
    ) -> int:
        """Soft-delete embeddings (is_deleted=true) for the given scope.

        Marks embeddings as deleted so they are excluded from HNSW similarity
        searches immediately. Processes in batches to limit HNSW index churn
        per transaction — a single unbatched UPDATE on 100K+ rows causes
        superlinear graph maintenance overhead.

        Commits after each batch so WAL can flush and locks are released.
        """
        self._validate_scope(log_event_ids, project_id)
        total = 0

        if log_event_ids is not None:
            if not log_event_ids:
                return 0
            for i in range(0, len(log_event_ids), batch_size):
                chunk = log_event_ids[i : i + batch_size]
                params = {"ids": chunk}
                if project_id is not None:
                    params["pid"] = project_id
                result = self.session.execute(
                    text(
                        f"""
                        UPDATE embedding
                        SET is_deleted = true
                        WHERE ref_id = ANY(:ids)
                          {self._prune_clause(project_id)}AND is_deleted = false
                    """,
                    ),
                    params,
                )
                total += result.rowcount
                self.session.commit()
        else:
            # ``project_id`` is denormalized onto embedding, so soft-delete by it
            # directly: this prunes to the project's partition and uses the
            # composite PK to page through batches. (A ctid-based batch is unsafe
            # here because ctid is ambiguous across partitions.)
            while True:
                result = self.session.execute(
                    text(
                        """
                        WITH batch AS (
                            SELECT project_id, id FROM embedding
                            WHERE project_id = :project_id
                              AND is_deleted = false
                            LIMIT :batch_size
                        )
                        UPDATE embedding e
                        SET is_deleted = true
                        FROM batch b
                        WHERE e.project_id = :project_id
                          AND e.project_id = b.project_id AND e.id = b.id
                    """,
                    ),
                    {"project_id": project_id, "batch_size": batch_size},
                )
                updated = result.rowcount
                total += updated
                self.session.commit()
                if updated < batch_size:
                    break

        return total

    def null_ref_ids(
        self,
        *,
        log_event_ids: Optional[List[int]] = None,
        project_id: Optional[int] = None,
    ) -> int:
        """Null out embedding ref_ids in bulk for the given scope.

        Called when the referenced log events are being hard-deleted. The
        embedding.ref_id -> log_event FK was removed when the tables were
        partitioned, so deleting a log event no longer touches the embedding;
        nulling ref_id explicitly records that the reference is gone (callers
        also soft-delete these embeddings so the index-maintenance worker
        reclaims them).
        """
        self._validate_scope(log_event_ids, project_id)

        if log_event_ids is not None:
            if not log_event_ids:
                return 0
            params = {"ids": log_event_ids}
            if project_id is not None:
                params["pid"] = project_id
            result = self.session.execute(
                text(
                    f"""
                    UPDATE embedding
                    SET ref_id = NULL
                    WHERE ref_id = ANY(:ids)
                      {self._prune_clause(project_id)}
                """,
                ),
                params,
            )
        else:
            result = self.session.execute(
                text(
                    """
                    UPDATE embedding e
                    SET ref_id = NULL
                    FROM log_event le
                    WHERE e.ref_id = le.id
                      AND le.project_id = :project_id
                """,
                ),
                {"project_id": project_id},
            )

        return result.rowcount
