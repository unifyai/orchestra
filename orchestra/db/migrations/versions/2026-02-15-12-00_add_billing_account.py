"""Add billing_account table and migrate billing fields.

Creates a shared billing_account table that consolidates billing fields from
both user and organization tables. After migration:

- billing_account holds: credits, stripe_customer_id, autorecharge settings,
  account_status, billing_setup_complete, tier, and business profile fields.
- user gains billing_account_id FK, loses billing columns.
- organization gains billing_account_id FK, loses billing columns.
- recharge gains billing_account_id FK, loses user_id and organization_id.
- credit_card_fingerprint gains billing_account_id FK, loses user_id.

Data is migrated with zero downtime: new columns are added first, data is
back-filled, then old columns are dropped.

Revision ID: add_billing_account
Revises: consolidate_user_tables
Create Date: 2026-02-15 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "add_billing_account"
down_revision = "consolidate_user_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # =====================================================================
    # Step 1: Create billing_account table
    # =====================================================================
    op.create_table(
        "billing_account",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        # Core billing
        sa.Column("credits", sa.Numeric(), nullable=False, server_default="0"),
        sa.Column("stripe_customer_id", sa.String(), nullable=True),
        sa.Column("autorecharge", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "autorecharge_threshold",
            sa.Numeric(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "autorecharge_qty",
            sa.Numeric(),
            nullable=False,
            server_default="25",
        ),
        sa.Column(
            "account_status",
            sa.String(),
            nullable=False,
            server_default="ACTIVE",
        ),
        sa.Column(
            "billing_setup_complete",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
        sa.Column("tier", sa.String(), nullable=False, server_default="developer"),
        # Billing profile
        sa.Column("billing_email", sa.String(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("tax_id", sa.String(length=100), nullable=True),
        sa.Column("tax_id_type", sa.String(length=50), nullable=True),
        sa.Column("tax_id_verification_status", sa.String(length=20), nullable=True),
        sa.Column(
            "billing_address",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        # Timestamps
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.Column("updated_at", sa.TIMESTAMP(), nullable=True),
        # Constraints
        sa.CheckConstraint(
            "account_status IN ('ACTIVE', 'PAST_DUE', 'SUSPENDED', 'CLOSED')",
            name="ck_billing_account_status",
        ),
    )

    # Indexes
    op.create_index(
        "ix_billing_account_stripe_customer_id",
        "billing_account",
        ["stripe_customer_id"],
        unique=True,
    )

    # =====================================================================
    # Step 2: Migrate user billing data → billing_account
    # =====================================================================

    # Add billing_account_id to user (nullable for now)
    op.add_column(
        "user",
        sa.Column("billing_account_id", sa.Integer(), nullable=True),
    )

    # Create a billing_account for each existing user and link them.
    # We use a temporary correlation column to reliably map rows back.
    op.add_column(
        "billing_account",
        sa.Column("_migration_source_id", sa.String(), nullable=True),
    )

    op.execute(
        """
        INSERT INTO billing_account (
            credits, stripe_customer_id, autorecharge,
            autorecharge_threshold, autorecharge_qty,
            account_status, tier, _migration_source_id
        )
        SELECT
            COALESCE(u.credits, 0),
            u.stripe_customer_id,
            COALESCE(u.autorecharge, false),
            COALESCE(u.autorecharge_threshold, 0),
            COALESCE(u.autorecharge_qty, 25),
            CASE WHEN u.frozen = true THEN 'SUSPENDED' ELSE 'ACTIVE' END,
            COALESCE(u.tier, 'developer'),
            u.id
        FROM "user" u
        """,
    )

    # Link user → billing_account via the correlation column
    op.execute(
        """
        UPDATE "user" u
        SET billing_account_id = ba.id
        FROM billing_account ba
        WHERE ba._migration_source_id = u.id
        """,
    )

    # Clear the correlation column (will be reused for orgs)
    op.execute(
        """UPDATE billing_account SET _migration_source_id = NULL""",
    )

    # Create FK and index
    op.create_index(
        "ix_user_billing_account_id",
        "user",
        ["billing_account_id"],
    )
    op.create_foreign_key(
        "fk_user_billing_account_id",
        "user",
        "billing_account",
        ["billing_account_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # =====================================================================
    # Step 3: Migrate organization billing data → billing_account
    # =====================================================================

    # Add billing_account_id to organization (nullable for now)
    op.add_column(
        "organization",
        sa.Column("billing_account_id", sa.Integer(), nullable=True),
    )

    # Create a billing_account for each org that has billing data
    # Reuse the _migration_source_id column (cast org int id to text)
    op.execute(
        """
        INSERT INTO billing_account (
            credits, stripe_customer_id, autorecharge,
            autorecharge_threshold, autorecharge_qty,
            account_status, billing_setup_complete,
            billing_email, name, tax_id, billing_address,
            _migration_source_id
        )
        SELECT
            COALESCE(o.credits, 0),
            o.stripe_customer_id,
            COALESCE(o.autorecharge, false),
            COALESCE(o.autorecharge_threshold, 0),
            COALESCE(o.autorecharge_qty, 25),
            COALESCE(o.account_status, 'ACTIVE'),
            COALESCE(o.billing_setup_complete, false),
            o.billing_email,
            o.business_name,
            o.tax_id,
            o.billing_address,
            o.id::text
        FROM organization o
        """,
    )

    # Link organization → billing_account via the correlation column
    op.execute(
        """
        UPDATE organization o
        SET billing_account_id = ba.id
        FROM billing_account ba
        WHERE ba._migration_source_id = o.id::text
          AND o.billing_account_id IS NULL
        """,
    )

    # Drop the temporary correlation column
    op.drop_column("billing_account", "_migration_source_id")

    # Create FK and index
    op.create_index(
        "ix_organization_billing_account_id",
        "organization",
        ["billing_account_id"],
    )
    op.create_foreign_key(
        "fk_organization_billing_account_id",
        "organization",
        "billing_account",
        ["billing_account_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # =====================================================================
    # Step 4: Migrate recharge table (user_id/org_id → billing_account_id)
    # =====================================================================

    # Add billing_account_id to recharge (nullable for now)
    op.add_column(
        "recharge",
        sa.Column("billing_account_id", sa.Integer(), nullable=True),
    )

    # Backfill from user's billing_account_id
    op.execute(
        """
        UPDATE recharge r
        SET billing_account_id = u.billing_account_id
        FROM "user" u
        WHERE r.user_id = u.id AND r.user_id IS NOT NULL
        """,
    )

    # Backfill from organization's billing_account_id
    op.execute(
        """
        UPDATE recharge r
        SET billing_account_id = o.billing_account_id
        FROM organization o
        WHERE r.organization_id = o.id AND r.organization_id IS NOT NULL
        """,
    )

    # Drop the XOR constraint before dropping columns
    op.execute("ALTER TABLE recharge DROP CONSTRAINT IF EXISTS ck_recharge_entity_xor")

    # Drop old FKs
    op.drop_constraint("recharge_user_id_fkey", "recharge", type_="foreignkey")
    # Try both naming conventions for the org FK
    op.execute(
        """
        DO $$
        BEGIN
            ALTER TABLE recharge DROP CONSTRAINT IF EXISTS recharge_organization_id_fkey;
        EXCEPTION WHEN undefined_object THEN NULL;
        END $$;
        """,
    )

    # Drop old index
    op.drop_index(
        "ix_recharge_organization_id",
        table_name="recharge",
        if_exists=True,
    )

    # Now make billing_account_id NOT NULL (all rows should be backfilled)
    op.alter_column(
        "recharge",
        "billing_account_id",
        nullable=False,
    )

    # Create new FK and index
    op.create_index(
        "ix_recharge_billing_account_id",
        "recharge",
        ["billing_account_id"],
    )
    op.create_foreign_key(
        "fk_recharge_billing_account_id",
        "recharge",
        "billing_account",
        ["billing_account_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # Drop old columns
    op.drop_column("recharge", "user_id")
    op.drop_column("recharge", "organization_id")

    # =====================================================================
    # Step 5: Migrate credit_card_fingerprint (user_id → billing_account_id)
    # =====================================================================

    # Add billing_account_id column
    op.add_column(
        "credit_card_fingerprint",
        sa.Column("billing_account_id", sa.Integer(), nullable=True),
    )

    # Backfill from user's billing_account_id
    op.execute(
        """
        UPDATE credit_card_fingerprint ccf
        SET billing_account_id = u.billing_account_id
        FROM "user" u
        WHERE ccf.user_id = u.id
        """,
    )

    # Drop old FK
    op.drop_constraint(
        "credit_card_fingerprint_user_id_fkey",
        "credit_card_fingerprint",
        type_="foreignkey",
    )

    # Make billing_account_id NOT NULL
    op.alter_column(
        "credit_card_fingerprint",
        "billing_account_id",
        nullable=False,
    )

    # Create new FK and index
    op.create_index(
        "ix_credit_card_fingerprint_billing_account_id",
        "credit_card_fingerprint",
        ["billing_account_id"],
    )
    op.create_foreign_key(
        "fk_credit_card_fingerprint_billing_account_id",
        "credit_card_fingerprint",
        "billing_account",
        ["billing_account_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # Drop old column
    op.drop_column("credit_card_fingerprint", "user_id")

    # =====================================================================
    # Step 6: Drop old billing columns from user
    # =====================================================================
    op.drop_column("user", "credits")
    op.drop_column("user", "stripe_customer_id")
    op.drop_column("user", "autorecharge")
    op.drop_column("user", "autorecharge_threshold")
    op.drop_column("user", "autorecharge_qty")
    op.drop_column("user", "frozen")
    op.drop_column("user", "credit_balance")
    op.drop_column("user", "billing_state")

    # =====================================================================
    # Step 7: Drop old billing columns from organization
    # =====================================================================
    # Drop the unique index on org.stripe_customer_id first
    op.drop_index(
        "ix_organization_stripe_customer_id",
        table_name="organization",
        if_exists=True,
    )
    # Drop account_status check constraint
    op.execute(
        "ALTER TABLE organization DROP CONSTRAINT IF EXISTS ck_organization_account_status",
    )

    op.drop_column("organization", "credits")
    op.drop_column("organization", "stripe_customer_id")
    op.drop_column("organization", "autorecharge")
    op.drop_column("organization", "autorecharge_threshold")
    op.drop_column("organization", "autorecharge_qty")
    op.drop_column("organization", "account_status")
    op.drop_column("organization", "billing_email")
    op.drop_column("organization", "business_name")
    op.drop_column("organization", "tax_id")
    op.drop_column("organization", "billing_address")
    op.drop_column("organization", "billing_setup_complete")


def downgrade() -> None:
    """
    Reverse the migration: restore billing columns on user/org and drop billing_account.

    WARNING: This is a destructive downgrade for data added after the upgrade.
    Business profile fields (billing_email, business_name, etc.) on billing_account
    will be lost for users since the old user table didn't have those columns.
    """

    # =====================================================================
    # Step 1: Restore billing columns on user
    # =====================================================================
    op.add_column(
        "user",
        sa.Column("credits", sa.Numeric(), nullable=False, server_default="0"),
    )
    op.add_column(
        "user",
        sa.Column("stripe_customer_id", sa.String(), nullable=True),
    )
    op.add_column(
        "user",
        sa.Column(
            "autorecharge",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )
    op.add_column(
        "user",
        sa.Column(
            "autorecharge_threshold",
            sa.Numeric(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "user",
        sa.Column(
            "autorecharge_qty",
            sa.Numeric(),
            nullable=False,
            server_default="25",
        ),
    )
    op.add_column(
        "user",
        sa.Column("frozen", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "user",
        sa.Column("credit_balance", sa.BigInteger(), nullable=True, server_default="0"),
    )
    op.add_column(
        "user",
        sa.Column("billing_state", sa.String(), nullable=True, server_default="OK"),
    )

    # Backfill user billing data from billing_account
    op.execute(
        """
        UPDATE "user" u
        SET
            credits = COALESCE(ba.credits, 0),
            stripe_customer_id = ba.stripe_customer_id,
            autorecharge = COALESCE(ba.autorecharge, false),
            autorecharge_threshold = COALESCE(ba.autorecharge_threshold, 0),
            autorecharge_qty = COALESCE(ba.autorecharge_qty, 25),
            frozen = CASE WHEN ba.account_status IN ('SUSPENDED', 'CLOSED') THEN true ELSE false END,
            billing_state = CASE WHEN ba.account_status = 'ACTIVE' THEN 'OK' ELSE 'PAST_DUE' END
        FROM billing_account ba
        WHERE u.billing_account_id = ba.id
        """,
    )

    # =====================================================================
    # Step 2: Restore billing columns on organization
    # =====================================================================
    op.add_column(
        "organization",
        sa.Column("credits", sa.Numeric(), nullable=False, server_default="0"),
    )
    op.add_column(
        "organization",
        sa.Column("stripe_customer_id", sa.String(), nullable=True),
    )
    op.add_column(
        "organization",
        sa.Column(
            "autorecharge",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )
    op.add_column(
        "organization",
        sa.Column(
            "autorecharge_threshold",
            sa.Numeric(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "organization",
        sa.Column(
            "autorecharge_qty",
            sa.Numeric(),
            nullable=False,
            server_default="25",
        ),
    )
    op.add_column(
        "organization",
        sa.Column(
            "account_status",
            sa.String(),
            nullable=False,
            server_default="ACTIVE",
        ),
    )
    op.add_column(
        "organization",
        sa.Column("billing_email", sa.String(), nullable=True),
    )
    op.add_column(
        "organization",
        sa.Column("business_name", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "organization",
        sa.Column("tax_id", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "organization",
        sa.Column(
            "billing_address",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "organization",
        sa.Column(
            "billing_setup_complete",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )

    # Backfill org billing data from billing_account
    op.execute(
        """
        UPDATE organization o
        SET
            credits = COALESCE(ba.credits, 0),
            stripe_customer_id = ba.stripe_customer_id,
            autorecharge = COALESCE(ba.autorecharge, false),
            autorecharge_threshold = COALESCE(ba.autorecharge_threshold, 0),
            autorecharge_qty = COALESCE(ba.autorecharge_qty, 25),
            account_status = COALESCE(ba.account_status, 'ACTIVE'),
            billing_email = ba.billing_email,
            business_name = ba.name,
            tax_id = ba.tax_id,
            billing_address = ba.billing_address,
            billing_setup_complete = COALESCE(ba.billing_setup_complete, false)
        FROM billing_account ba
        WHERE o.billing_account_id = ba.id
        """,
    )

    # Restore org stripe_customer_id unique index
    op.create_index(
        "ix_organization_stripe_customer_id",
        "organization",
        ["stripe_customer_id"],
        unique=True,
    )

    # Restore account_status check constraint
    op.execute(
        """
        ALTER TABLE organization
        ADD CONSTRAINT ck_organization_account_status
        CHECK (account_status IN ('ACTIVE', 'SUSPENDED', 'PAST_DUE', 'CLOSED'))
        """,
    )

    # =====================================================================
    # Step 3: Restore credit_card_fingerprint.user_id
    # =====================================================================
    op.add_column(
        "credit_card_fingerprint",
        sa.Column("user_id", sa.String(), nullable=True),
    )

    # Backfill user_id from billing_account → user
    op.execute(
        """
        UPDATE credit_card_fingerprint ccf
        SET user_id = u.id
        FROM "user" u
        WHERE u.billing_account_id = ccf.billing_account_id
        """,
    )

    # Drop new FK/index
    op.drop_constraint(
        "fk_credit_card_fingerprint_billing_account_id",
        "credit_card_fingerprint",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_credit_card_fingerprint_billing_account_id",
        table_name="credit_card_fingerprint",
    )
    op.drop_column("credit_card_fingerprint", "billing_account_id")

    # Restore old FK
    op.create_foreign_key(
        "credit_card_fingerprint_user_id_fkey",
        "credit_card_fingerprint",
        "user",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # =====================================================================
    # Step 4: Restore recharge.user_id and recharge.organization_id
    # =====================================================================
    op.add_column(
        "recharge",
        sa.Column("user_id", sa.String(), nullable=True),
    )
    op.add_column(
        "recharge",
        sa.Column("organization_id", sa.Integer(), nullable=True),
    )

    # Backfill user_id from billing_account → user
    op.execute(
        """
        UPDATE recharge r
        SET user_id = u.id
        FROM "user" u
        WHERE u.billing_account_id = r.billing_account_id
        """,
    )

    # Backfill organization_id from billing_account → organization
    op.execute(
        """
        UPDATE recharge r
        SET organization_id = o.id
        FROM organization o
        WHERE o.billing_account_id = r.billing_account_id
          AND r.user_id IS NULL  -- Only for recharges that weren't user-linked
        """,
    )

    # Drop new FK/index
    op.drop_constraint(
        "fk_recharge_billing_account_id",
        "recharge",
        type_="foreignkey",
    )
    op.drop_index("ix_recharge_billing_account_id", table_name="recharge")
    op.drop_column("recharge", "billing_account_id")

    # Restore old FKs
    op.create_foreign_key(
        "recharge_user_id_fkey",
        "recharge",
        "user",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_recharge_organization_id",
        "recharge",
        ["organization_id"],
    )
    op.create_foreign_key(
        "recharge_organization_id_fkey",
        "recharge",
        "organization",
        ["organization_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # Restore XOR constraint
    op.execute(
        """
        ALTER TABLE recharge
        ADD CONSTRAINT ck_recharge_entity_xor
        CHECK (
            (user_id IS NOT NULL AND organization_id IS NULL) OR
            (user_id IS NULL AND organization_id IS NOT NULL)
        )
        """,
    )

    # =====================================================================
    # Step 5: Drop billing_account FK from user and organization
    # =====================================================================
    op.drop_constraint("fk_user_billing_account_id", "user", type_="foreignkey")
    op.drop_index("ix_user_billing_account_id", table_name="user")
    op.drop_column("user", "billing_account_id")

    op.drop_constraint(
        "fk_organization_billing_account_id",
        "organization",
        type_="foreignkey",
    )
    op.drop_index("ix_organization_billing_account_id", table_name="organization")
    op.drop_column("organization", "billing_account_id")

    # =====================================================================
    # Step 6: Drop billing_account table
    # =====================================================================
    op.drop_index("ix_billing_account_stripe_customer_id", table_name="billing_account")
    op.drop_table("billing_account")
