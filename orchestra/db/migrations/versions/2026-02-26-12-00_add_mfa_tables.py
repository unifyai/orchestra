"""Add mfa_credential and mfa_recovery tables for Phase 2 TOTP 2FA

Create the mfa_credential table (encrypted TOTP secrets, one per user
per method type) and the mfa_recovery table (hashed single-use recovery
codes).

Revision ID: add_mfa_tables
Revises: add_email_auth
Create Date: 2026-02-26 12:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "add_mfa_tables"
down_revision = "add_email_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # mfa_credential — encrypted TOTP (or future WebAuthn) secrets
    # ------------------------------------------------------------------
    op.create_table(
        "mfa_credential",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("method_type", sa.String(), nullable=False),
        sa.Column("credential_data", sa.LargeBinary(), nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "confirmed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "last_used_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_mfa_credential_user_type",
        "mfa_credential",
        ["user_id", "method_type"],
    )

    # ------------------------------------------------------------------
    # mfa_recovery — hashed single-use recovery codes
    # ------------------------------------------------------------------
    op.create_table(
        "mfa_recovery",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("code_hash", sa.String(), nullable=False),
        sa.Column(
            "used",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
        sa.Column(
            "used_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("mfa_recovery")
    op.drop_index("ix_mfa_credential_user_type", table_name="mfa_credential")
    op.drop_table("mfa_credential")
