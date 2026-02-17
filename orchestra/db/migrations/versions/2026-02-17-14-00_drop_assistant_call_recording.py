"""Drop assistant_call_recording table

Recording URLs are now stored directly on exchange metadata in Unity's
transcript system, making the relational recording table redundant.

Revision ID: a1b2c3d4e5f6
Revises: 9b2c3d4e5f6a
Create Date: 2026-02-17 14:00:00.000000

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "a1b2c3d4e5f6"
down_revision = "9b2c3d4e5f6a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(
        op.f("ix_assistant_call_recording_agent_id"),
        table_name="assistant_call_recording",
    )
    op.drop_table("assistant_call_recording")


def downgrade() -> None:
    import sqlalchemy as sa

    op.create_table(
        "assistant_call_recording",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("agent_id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["assistants.agent_id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_assistant_call_recording_agent_id"),
        "assistant_call_recording",
        ["agent_id"],
        unique=False,
    )
