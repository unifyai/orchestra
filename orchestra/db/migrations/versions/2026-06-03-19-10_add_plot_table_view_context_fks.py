"""Add context cascade FKs for plot and table views.

Revision ID: 2026_plot_table_context_fks
Revises: seed_personal_cm
Create Date: 2026-06-03 19:10:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "2026_plot_table_context_fks"
down_revision = "seed_personal_cm"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("plot", sa.Column("context_id", sa.Integer(), nullable=True))
    op.add_column("table_view", sa.Column("context_id", sa.Integer(), nullable=True))

    op.execute(
        """
        UPDATE plot
        SET context_id = context.id
        FROM context
        WHERE plot.project_id = context.project_id
          AND plot.project_config ->> 'context' = context.name
        """,
    )
    op.execute(
        """
        UPDATE table_view
        SET context_id = context.id
        FROM context
        WHERE table_view.project_id = context.project_id
          AND table_view.project_config ->> 'context' = context.name
        """,
    )

    op.create_foreign_key(
        "fk_plot_context_id_context",
        "plot",
        "context",
        ["context_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_table_view_context_id_context",
        "table_view",
        "context",
        ["context_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("idx_plot_context_id", "plot", ["context_id"])
    op.create_index("idx_table_view_context_id", "table_view", ["context_id"])


def downgrade() -> None:
    op.drop_index("idx_table_view_context_id", table_name="table_view")
    op.drop_index("idx_plot_context_id", table_name="plot")
    op.drop_constraint(
        "fk_table_view_context_id_context",
        "table_view",
        type_="foreignkey",
    )
    op.drop_constraint("fk_plot_context_id_context", "plot", type_="foreignkey")
    op.drop_column("table_view", "context_id")
    op.drop_column("plot", "context_id")
