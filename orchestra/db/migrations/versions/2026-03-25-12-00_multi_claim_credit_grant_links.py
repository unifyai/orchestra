"""Multi-claim credit grant links.

Introduces a separate ``credit_grant_link_claim`` table so that a single
credit grant link can be redeemed by multiple users (up to ``max_claims``).

Steps:
1. Create ``credit_grant_link_claim`` table.
2. Add ``max_claims`` column to ``one_time_credit_grant_link``.
3. Migrate existing claim data from link rows into ``credit_grant_link_claim``.
4. Drop the now-redundant ``user_id``, ``organization_id``, ``claimed_at``
   columns from ``one_time_credit_grant_link``.

Revision ID: multi_claim_credit_grant_links
Revises: add_assistant_deploy_env
Create Date: 2026-03-25 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "multi_claim_credit_grant_links"
down_revision = "add_assistant_deploy_env"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Create claims table
    op.create_table(
        "credit_grant_link_claim",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "link_id",
            sa.String(),
            sa.ForeignKey(
                "one_time_credit_grant_link.id",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("user.id"),
            nullable=False,
        ),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organization.id"),
            nullable=True,
            comment="Organization that received the credits (NULL = personal claim)",
        ),
        sa.Column(
            "claimed_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_credit_grant_link_claim_link_id",
        "credit_grant_link_claim",
        ["link_id"],
    )
    op.create_index(
        "ix_credit_grant_link_claim_user_id",
        "credit_grant_link_claim",
        ["user_id"],
    )
    op.create_index(
        "ix_credit_grant_link_claim_organization_id",
        "credit_grant_link_claim",
        ["organization_id"],
    )
    op.create_unique_constraint(
        "uq_claim_link_user",
        "credit_grant_link_claim",
        ["link_id", "user_id"],
    )

    # 2. Add max_claims and name columns to link table
    op.add_column(
        "one_time_credit_grant_link",
        sa.Column(
            "max_claims",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
            comment="Maximum number of distinct users/orgs that can redeem this link",
        ),
    )
    op.add_column(
        "one_time_credit_grant_link",
        sa.Column(
            "name",
            sa.String(),
            nullable=True,
            comment="Optional admin-facing label (e.g. outreach channel or campaign)",
        ),
    )

    # 3. Migrate existing claims into the new table
    op.execute(
        """
        INSERT INTO credit_grant_link_claim (id, link_id, user_id, organization_id, claimed_at)
        SELECT
            gen_random_uuid()::text,
            id,
            user_id,
            organization_id,
            COALESCE(claimed_at, now())
        FROM one_time_credit_grant_link
        WHERE user_id IS NOT NULL
        """,
    )

    # 4. Drop old columns
    op.drop_index(
        "ix_one_time_credit_grant_link_organization_id",
        table_name="one_time_credit_grant_link",
    )
    op.drop_constraint(
        "one_time_credit_grant_link_organization_id_fkey",
        "one_time_credit_grant_link",
        type_="foreignkey",
    )
    op.drop_column("one_time_credit_grant_link", "organization_id")

    op.drop_index(
        "ix_assistant_hiring_one_time_approval_link_user_id",
        table_name="one_time_credit_grant_link",
    )
    op.drop_constraint(
        "assistant_hiring_one_time_approval_link_user_id_fkey",
        "one_time_credit_grant_link",
        type_="foreignkey",
    )
    op.drop_column("one_time_credit_grant_link", "user_id")

    op.drop_column("one_time_credit_grant_link", "claimed_at")


def downgrade() -> None:
    # Re-add old columns
    op.add_column(
        "one_time_credit_grant_link",
        sa.Column(
            "claimed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "one_time_credit_grant_link",
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("user.id"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_assistant_hiring_one_time_approval_link_user_id",
        "one_time_credit_grant_link",
        ["user_id"],
    )
    op.add_column(
        "one_time_credit_grant_link",
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organization.id"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_one_time_credit_grant_link_organization_id",
        "one_time_credit_grant_link",
        ["organization_id"],
    )

    # Migrate claims back to link table (only the first claim per link)
    op.execute(
        """
        UPDATE one_time_credit_grant_link AS l
        SET
            user_id = c.user_id,
            organization_id = c.organization_id,
            claimed_at = c.claimed_at
        FROM (
            SELECT DISTINCT ON (link_id) link_id, user_id, organization_id, claimed_at
            FROM credit_grant_link_claim
            ORDER BY link_id, claimed_at ASC
        ) AS c
        WHERE l.id = c.link_id
        """,
    )

    # Drop new columns and claims table
    op.drop_column("one_time_credit_grant_link", "name")
    op.drop_column("one_time_credit_grant_link", "max_claims")
    op.drop_table("credit_grant_link_claim")
