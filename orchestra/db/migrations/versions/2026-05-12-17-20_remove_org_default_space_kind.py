"""Remove org_default space semantics from the spaces contract.

Revision ID: remove_org_default_space_kind
Revises: drop_credit_balance_after
Create Date: 2026-05-12 17:20:00.000000
"""

from alembic import op

revision = "remove_org_default_space_kind"
down_revision = "drop_credit_balance_after"
branch_labels = None
depends_on = None

ORG_DEFAULT_INDEX_NAME = "ux_spaces_one_org_default_per_org"
SPACES_KIND_CONSTRAINT = "ck_spaces_kind"


def upgrade() -> None:
    op.execute("UPDATE spaces SET kind = 'team' WHERE kind = 'org_default'")
    op.execute(
        f"DROP INDEX IF EXISTS {ORG_DEFAULT_INDEX_NAME}",
    )
    op.execute(
        f"ALTER TABLE spaces DROP CONSTRAINT IF EXISTS {SPACES_KIND_CONSTRAINT}",
    )
    op.execute(
        "ALTER TABLE spaces "
        f"ADD CONSTRAINT {SPACES_KIND_CONSTRAINT} CHECK (kind = 'team')",
    )


def downgrade() -> None:
    op.execute(
        f"ALTER TABLE spaces DROP CONSTRAINT IF EXISTS {SPACES_KIND_CONSTRAINT}",
    )
    op.execute(
        "ALTER TABLE spaces "
        f"ADD CONSTRAINT {SPACES_KIND_CONSTRAINT} CHECK (kind IN ('team', 'org_default'))",
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        f"{ORG_DEFAULT_INDEX_NAME} "
        "ON spaces (organization_id) "
        "WHERE kind = 'org_default'",
    )
