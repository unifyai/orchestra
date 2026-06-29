"""Repair child rows whose denormalized partition key drifted from their parent.

``log_event_context``, ``embedding`` and ``embedding_queue`` carry a denormalized
``project_id`` that IS the ``LIST (project_id)`` partition key, copied from the
parent ``log_event``. The assistant->organization transfer historically moved
``log_event.project_id`` without moving the children (now fixed via
``LogEventDAO.reproject_logs``), which could leave children pointing at the old
project. While everything lives in the single DEFAULT partition this is latent,
but once projects are promoted to real partitions the drifted children would
route to the wrong partition and orphan from their parent.

This one-time, idempotent backfill re-derives each child's ``project_id`` from
its parent ``log_event`` and only touches rows that actually drifted (a no-op
where data is already consistent).

Revision ID: reproject_orphaned_child_partition_keys
Revises: tune_kernel_partition_autovacuum
Create Date: 2026-07-05 00:00:00.000000
"""

from alembic import op

revision = "reproject_orphaned_child_partition_keys"
down_revision = "tune_kernel_partition_autovacuum"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE log_event_context lec
        SET project_id = le.project_id
        FROM log_event le
        WHERE lec.log_event_id = le.id
          AND lec.project_id <> le.project_id
        """,
    )
    op.execute(
        """
        UPDATE embedding e
        SET project_id = le.project_id
        FROM log_event le
        WHERE e.ref_id = le.id
          AND e.ref_id IS NOT NULL
          AND e.project_id <> le.project_id
        """,
    )
    op.execute(
        """
        UPDATE embedding_queue eq
        SET project_id = le.project_id
        FROM log_event le
        WHERE eq.ref_id = le.id
          AND eq.project_id <> le.project_id
        """,
    )


def downgrade() -> None:
    # Data-repair only; the prior (inconsistent) state is not reconstructable.
    pass
