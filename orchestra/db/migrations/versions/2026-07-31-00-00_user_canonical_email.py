"""Add user.canonical_email and backfill from existing addresses.

Signup uniqueness runs against the canonical form (dots and plus-suffix
stripped for Gmail-style providers) so one inbox cannot mint unlimited
aliased accounts. Pre-existing alias duplicates are tolerated (index is
non-unique); the application layer rejects new collisions.

Revision ID: user_canonical_email
Revises: merge_multiplayer_conn_uq
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "user_canonical_email"
down_revision = "merge_multiplayer_conn_uq"
branch_labels = None
depends_on = None


def _canonicalize(email: str) -> str:
    gmail_domains = {"gmail.com", "googlemail.com"}
    local, _, domain = email.lower().strip().rpartition("@")
    local = local.split("+", 1)[0]
    if domain in gmail_domains:
        local = local.replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def upgrade() -> None:
    op.add_column("user", sa.Column("canonical_email", sa.String(), nullable=True))
    op.create_index(
        "ix_user_canonical_email",
        "user",
        ["canonical_email"],
        unique=False,
    )

    conn = op.get_bind()
    rows = conn.execute(sa.text('SELECT id, email FROM "user"')).fetchall()
    for user_id, email in rows:
        conn.execute(
            sa.text('UPDATE "user" SET canonical_email = :c WHERE id = :i'),
            {"c": _canonicalize(email), "i": user_id},
        )


def downgrade() -> None:
    op.drop_index("ix_user_canonical_email", table_name="user")
    op.drop_column("user", "canonical_email")
