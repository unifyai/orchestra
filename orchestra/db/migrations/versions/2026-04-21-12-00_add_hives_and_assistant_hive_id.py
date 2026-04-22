"""Add hives table and assistants.hive_id column.

Introduces the Hive entity as a first-class Orchestra table. A Hive groups
assistants owned by the same organization so they can share Contacts,
Transcripts, Knowledge, Guidance, Tasks, and other team memory while each
body keeps its own runtime. Assistants gain a nullable ``hive_id`` FK that
marks membership; ``NULL`` still means solo. No reads or writes consume the
new column yet, so this migration is invisible at runtime and N-1 safe.

V0 restricts the system to a single Hive per organization; the
``ux_hives_one_per_org`` unique index enforces that atomically and trips as
HTTP 409 on concurrent creates. When multi-Hive ships that index drops and
``ux_hives_org_name`` becomes the operative uniqueness constraint.

``organization_id`` uses ``ON DELETE RESTRICT`` on purpose: the Hive cascade
is application-driven (shared contexts, embeddings, GCS objects, per-body
overlays) and must run before the org row disappears; a raw DB cascade would
silently orphan that state.

Revision ID: add_hives_and_assistant_hive_id
Revises: ms365_business_premium_pricing
Create Date: 2026-04-21 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "add_hives_and_assistant_hive_id"
down_revision = "add_assistant_job_title"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hives",
        sa.Column(
            "hive_id",
            sa.BigInteger,
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "organization_id",
            sa.Integer,
            sa.ForeignKey("organization.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ux_hives_one_per_org",
        "hives",
        ["organization_id"],
        unique=True,
    )
    op.create_index(
        "ux_hives_org_name",
        "hives",
        ["organization_id", "name"],
        unique=True,
    )

    op.add_column(
        "assistants",
        sa.Column(
            "hive_id",
            sa.Integer,
            sa.ForeignKey("hives.hive_id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("assistants", "hive_id")
    op.drop_index("ux_hives_org_name", table_name="hives")
    op.drop_index("ux_hives_one_per_org", table_name="hives")
    op.drop_table("hives")
