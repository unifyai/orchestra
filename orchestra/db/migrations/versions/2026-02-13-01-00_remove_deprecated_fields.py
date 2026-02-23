"""Remove deprecated fields from auth_user and organization.

Removes fields that are no longer needed:
1. auth_user.assistant_hiring_approval - replaced by rate limits + credits
2. organization.billing_user_id - orgs now use direct billing via stripe_customer_id
3. auth_user business fields - business info now tracked on Organization only

Revision ID: remove_deprecated_fields
Revises: add_onboarding_status
Create Date: 2026-02-13 01:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "remove_deprecated_fields"
down_revision = "add_onboarding_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # =========================================================================
    # 1. Remove assistant_hiring_approval from auth_user
    # =========================================================================
    op.drop_index(
        "ix_auth_user_assistant_hiring_approval",
        table_name="auth_user",
    )
    op.drop_column("auth_user", "assistant_hiring_approval")

    # =========================================================================
    # 2. Remove billing_user_id from organization
    # =========================================================================
    op.drop_constraint(
        "organization_billing_user_id_fkey",
        "organization",
        type_="foreignkey",
    )
    op.drop_column("organization", "billing_user_id")

    # =========================================================================
    # 3. Remove business fields from auth_user
    # =========================================================================
    # Drop indexes first
    op.drop_index("idx_auth_user_account_type", table_name="auth_user")
    op.drop_index("idx_auth_user_tax_id", table_name="auth_user")
    op.drop_index("idx_auth_user_business_verified", table_name="auth_user")
    op.drop_index("idx_auth_user_business_country", table_name="auth_user")
    op.drop_index("idx_auth_user_account_type_verified", table_name="auth_user")
    op.drop_index("idx_auth_user_tax_jurisdiction", table_name="auth_user")

    # Drop check constraint
    op.drop_constraint("ck_auth_user_account_type", "auth_user", type_="check")

    # Drop columns
    op.drop_column("auth_user", "account_type")
    op.drop_column("auth_user", "business_name")
    op.drop_column("auth_user", "tax_id")
    op.drop_column("auth_user", "business_type")
    op.drop_column("auth_user", "business_address_line1")
    op.drop_column("auth_user", "business_address_line2")
    op.drop_column("auth_user", "business_city")
    op.drop_column("auth_user", "business_state")
    op.drop_column("auth_user", "business_country")
    op.drop_column("auth_user", "business_postal_code")
    op.drop_column("auth_user", "tax_exempt")
    op.drop_column("auth_user", "business_verified")
    op.drop_column("auth_user", "tax_jurisdiction")


def downgrade() -> None:
    # =========================================================================
    # 3. Re-add business fields to auth_user
    # =========================================================================
    op.add_column(
        "auth_user",
        sa.Column(
            "account_type",
            sa.String(20),
            nullable=False,
            server_default="individual",
        ),
    )
    op.add_column(
        "auth_user",
        sa.Column("business_name", sa.String(255), nullable=True),
    )
    op.add_column(
        "auth_user",
        sa.Column("tax_id", sa.String(100), nullable=True),
    )
    op.add_column(
        "auth_user",
        sa.Column("business_type", sa.String(50), nullable=True),
    )
    op.add_column(
        "auth_user",
        sa.Column("business_address_line1", sa.String(255), nullable=True),
    )
    op.add_column(
        "auth_user",
        sa.Column("business_address_line2", sa.String(255), nullable=True),
    )
    op.add_column(
        "auth_user",
        sa.Column("business_city", sa.String(100), nullable=True),
    )
    op.add_column(
        "auth_user",
        sa.Column("business_state", sa.String(100), nullable=True),
    )
    op.add_column(
        "auth_user",
        sa.Column("business_country", sa.String(100), nullable=True),
    )
    op.add_column(
        "auth_user",
        sa.Column("business_postal_code", sa.String(20), nullable=True),
    )
    op.add_column(
        "auth_user",
        sa.Column("tax_exempt", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "auth_user",
        sa.Column(
            "business_verified",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )
    op.add_column(
        "auth_user",
        sa.Column("tax_jurisdiction", sa.String(100), nullable=True),
    )

    # Re-add check constraint
    op.create_check_constraint(
        "ck_auth_user_account_type",
        "auth_user",
        "account_type IN ('individual', 'business')",
    )

    # Re-add indexes
    op.create_index("idx_auth_user_account_type", "auth_user", ["account_type"])
    op.create_index(
        "idx_auth_user_tax_id",
        "auth_user",
        ["tax_id"],
        unique=True,
        postgresql_where=sa.text("tax_id IS NOT NULL"),
    )
    op.create_index(
        "idx_auth_user_business_verified",
        "auth_user",
        ["business_verified"],
    )
    op.create_index("idx_auth_user_business_country", "auth_user", ["business_country"])
    op.create_index(
        "idx_auth_user_account_type_verified",
        "auth_user",
        ["account_type", "business_verified"],
    )
    op.create_index("idx_auth_user_tax_jurisdiction", "auth_user", ["tax_jurisdiction"])

    # =========================================================================
    # 2. Re-add billing_user_id to organization
    # =========================================================================
    op.add_column(
        "organization",
        sa.Column(
            "billing_user_id",
            sa.String(),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "organization_billing_user_id_fkey",
        "organization",
        "users",
        ["billing_user_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # =========================================================================
    # 1. Re-add assistant_hiring_approval to auth_user
    # =========================================================================
    op.add_column(
        "auth_user",
        sa.Column(
            "assistant_hiring_approval",
            sa.String(),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_auth_user_assistant_hiring_approval",
        "auth_user",
        ["assistant_hiring_approval"],
        unique=False,
    )
