"""Drop user_id from assistant_secrets primary key.

Revision ID: assistant_secret_pk_drop_user_id
Revises: readd_assistant_secrets
Create Date: 2026-04-15 12:00:00.000000

The PK was (user_id, agent_id, secret_name), which caused duplicate rows
when different org members wrote to the same secret.  The new PK is
(agent_id, secret_name); user_id stays as a regular NOT NULL column for
audit purposes.
"""

from alembic import op

revision = "assistant_secret_pk_drop_user_id"
down_revision = "readd_assistant_secrets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM assistant_secrets a
        USING assistant_secrets b
        WHERE a.user_id > b.user_id
          AND a.agent_id = b.agent_id
          AND a.secret_name = b.secret_name
        """,
    )
    op.drop_constraint(
        "assistant_secrets_pkey",
        "assistant_secrets",
        type_="primary",
    )
    op.create_primary_key(
        "assistant_secrets_pkey",
        "assistant_secrets",
        ["agent_id", "secret_name"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "assistant_secrets_pkey",
        "assistant_secrets",
        type_="primary",
    )
    op.create_primary_key(
        "assistant_secrets_pkey",
        "assistant_secrets",
        ["user_id", "agent_id", "secret_name"],
    )
