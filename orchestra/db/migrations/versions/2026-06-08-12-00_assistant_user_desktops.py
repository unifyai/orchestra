"""Replace the single assistant->user_desktop pointer with a per-user join.

Each user links their own machine to the assistants they work with, so the
relationship becomes many-to-many with a per-(assistant, user) uniqueness rule.
The old single ``assistants.user_desktop_id`` column (and the assistant-level
``user_desktop_filesys_sync`` flag) are migrated into the new join table.

Revision ID: 2026_assistant_user_desktops
Revises: 2026_retire_spaces
Create Date: 2026-06-08 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "2026_assistant_user_desktops"
down_revision = "2026_retire_spaces"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assistant_user_desktops",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "assistant_id",
            sa.Integer(),
            sa.ForeignKey("assistants.agent_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_desktop_id",
            sa.Integer(),
            sa.ForeignKey("user_desktops.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_user_id",
            sa.String(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "filesys_sync",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "assistant_id",
            "owner_user_id",
            name="uq_assistant_user_desktop_owner",
        ),
        sa.UniqueConstraint(
            "assistant_id",
            "user_desktop_id",
            name="uq_assistant_user_desktop_pair",
        ),
    )
    op.create_index(
        "ix_assistant_user_desktops_assistant_id",
        "assistant_user_desktops",
        ["assistant_id"],
    )
    op.create_index(
        "ix_assistant_user_desktops_user_desktop_id",
        "assistant_user_desktops",
        ["user_desktop_id"],
    )
    op.create_index(
        "ix_assistant_user_desktops_owner_user_id",
        "assistant_user_desktops",
        ["owner_user_id"],
    )

    # Carry existing single links into the join table, deriving the owning user
    # from the desktop's registered owner.
    op.execute(
        """
        INSERT INTO assistant_user_desktops
            (assistant_id, user_desktop_id, owner_user_id, filesys_sync, created_at)
        SELECT a.agent_id, a.user_desktop_id, ud.user_id,
               COALESCE(a.user_desktop_filesys_sync, false), now()
        FROM assistants a
        JOIN user_desktops ud ON ud.id = a.user_desktop_id
        WHERE a.user_desktop_id IS NOT NULL
        """,
    )

    # Constraint names drift across environments: the platform baseline names the
    # unique constraint ``uq_assistant_user_desktop_id``, while DBs built from the
    # ORM's ``unique=True`` column get Postgres' default
    # ``assistants_user_desktop_id_key``. Drop whichever exists (the column drop
    # would cascade them anyway, but being explicit keeps the intent clear).
    op.execute(
        "ALTER TABLE assistants DROP CONSTRAINT IF EXISTS assistants_user_desktop_id_fkey",
    )
    op.execute(
        "ALTER TABLE assistants DROP CONSTRAINT IF EXISTS uq_assistant_user_desktop_id",
    )
    op.execute(
        "ALTER TABLE assistants DROP CONSTRAINT IF EXISTS assistants_user_desktop_id_key",
    )
    op.drop_column("assistants", "user_desktop_id")
    op.drop_column("assistants", "user_desktop_filesys_sync")


def downgrade() -> None:
    op.add_column(
        "assistants",
        sa.Column("user_desktop_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "assistants",
        sa.Column(
            "user_desktop_filesys_sync",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_foreign_key(
        "assistants_user_desktop_id_fkey",
        "assistants",
        "user_desktops",
        ["user_desktop_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "uq_assistant_user_desktop_id",
        "assistants",
        ["user_desktop_id"],
    )

    # Best-effort restore: collapse the join back to a single link per assistant
    # (the earliest link wins, matching the prior one-desktop-per-assistant rule).
    op.execute(
        """
        UPDATE assistants a
        SET user_desktop_id = j.user_desktop_id,
            user_desktop_filesys_sync = j.filesys_sync
        FROM (
            SELECT DISTINCT ON (assistant_id)
                assistant_id, user_desktop_id, filesys_sync
            FROM assistant_user_desktops
            ORDER BY assistant_id, created_at ASC, id ASC
        ) j
        WHERE a.agent_id = j.assistant_id
        """,
    )

    op.drop_index(
        "ix_assistant_user_desktops_owner_user_id",
        table_name="assistant_user_desktops",
    )
    op.drop_index(
        "ix_assistant_user_desktops_user_desktop_id",
        table_name="assistant_user_desktops",
    )
    op.drop_index(
        "ix_assistant_user_desktops_assistant_id",
        table_name="assistant_user_desktops",
    )
    op.drop_table("assistant_user_desktops")
