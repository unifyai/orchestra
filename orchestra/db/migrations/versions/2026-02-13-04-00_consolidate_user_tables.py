"""Consolidate users and auth_user tables into single user table.

Previously:
- `users` table: billing fields (credits, stripe_customer_id, autorecharge, etc.)
- `auth_user` table: profile/identity fields (email, name, tier, etc.)

After:
- `user` table: all user fields consolidated

Revision ID: consolidate_user_tables
Revises: remove_deprecated_fields
Create Date: 2026-02-13 04:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "consolidate_user_tables"
down_revision = "remove_deprecated_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Step 1: Rename auth_user to user
    op.rename_table("auth_user", "user")

    # Step 2: Add billing columns from users table to user table
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
        sa.Column("autorecharge", sa.Boolean(), nullable=False, server_default="false"),
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
        sa.Column("store_prompts", sa.Boolean(), nullable=False, server_default="true"),
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

    # Step 3: Copy billing data from users to user (IDs match)
    op.execute(
        """
        UPDATE "user" u
        SET
            credits = COALESCE(us.credits, 0),
            stripe_customer_id = us.stripe_customer_id,
            autorecharge = COALESCE(us.autorecharge, false),
            autorecharge_threshold = COALESCE(us.autorecharge_threshold, 0),
            autorecharge_qty = COALESCE(us.autorecharge_qty, 25),
            store_prompts = COALESCE(us.store_prompts, true),
            frozen = COALESCE(us.frozen, false),
            credit_balance = COALESCE(us.credit_balance, 0),
            billing_state = COALESCE(us.billing_state, 'OK')
        FROM users us
        WHERE u.id = us.id
        """,
    )

    # Step 4: Update FK references from users.id to user.id
    # First drop the existing FKs
    op.drop_constraint("recharge_user_id_fkey", "recharge", type_="foreignkey")
    op.drop_constraint(
        "credit_card_fingerprint_user_id_fkey",
        "credit_card_fingerprint",
        type_="foreignkey",
    )

    # Step 4.1: Back up and clean up orphan data — rows in recharge,
    # credit_card_fingerprint, and users that reference user_ids present in the
    # old `users` table but not in `user` (the renamed auth_user).
    # These are dead users with no auth records, no accounts, no sessions,
    # and zero credit balance. We back them up for rollback safety.
    op.execute("DROP TABLE IF EXISTS _backup_orphan_recharge")
    op.execute("DROP TABLE IF EXISTS _backup_orphan_credit_card_fingerprint")
    op.execute("DROP TABLE IF EXISTS _backup_orphan_users")
    op.execute(
        """
        CREATE TABLE _backup_orphan_recharge AS
        SELECT * FROM recharge
        WHERE user_id NOT IN (SELECT id FROM "user")
        """,
    )
    op.execute(
        """
        CREATE TABLE _backup_orphan_credit_card_fingerprint AS
        SELECT * FROM credit_card_fingerprint
        WHERE user_id NOT IN (SELECT id FROM "user")
        """,
    )
    op.execute(
        """
        CREATE TABLE _backup_orphan_users AS
        SELECT * FROM users
        WHERE id NOT IN (SELECT id FROM "user")
        """,
    )
    op.execute(
        """
        DELETE FROM recharge
        WHERE user_id NOT IN (SELECT id FROM "user")
        """,
    )
    op.execute(
        """
        DELETE FROM credit_card_fingerprint
        WHERE user_id NOT IN (SELECT id FROM "user")
        """,
    )

    # Create new FKs pointing to user.id
    op.create_foreign_key(
        "recharge_user_id_fkey",
        "recharge",
        "user",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "credit_card_fingerprint_user_id_fkey",
        "credit_card_fingerprint",
        "user",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # Step 5: Drop the old users table
    op.drop_table("users")

    # Step 6: Rename indexes from auth_user to user
    # The indexes were automatically renamed when the table was renamed,
    # but we should ensure they have consistent naming
    op.execute(
        'ALTER INDEX IF EXISTS "auth_user_pkey" RENAME TO "user_pkey"',
    )
    op.execute(
        'ALTER INDEX IF EXISTS "ix_auth_user_email" RENAME TO "ix_user_email"',
    )


def downgrade() -> None:
    # Step 1: Recreate users table
    op.create_table(
        "users",
        sa.Column("id", sa.String(), primary_key=True),
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
        sa.Column("store_prompts", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("frozen", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("credit_balance", sa.BigInteger(), nullable=True),
        sa.Column("billing_state", sa.String(), nullable=True, server_default="OK"),
    )

    # Step 2: Copy billing data from user to users
    op.execute(
        """
        INSERT INTO users (
            id, credits, stripe_customer_id, autorecharge,
            autorecharge_threshold, autorecharge_qty, store_prompts,
            frozen, credit_balance, billing_state
        )
        SELECT
            id, credits, stripe_customer_id, autorecharge,
            autorecharge_threshold, autorecharge_qty, store_prompts,
            frozen, credit_balance, billing_state
        FROM "user"
        """,
    )

    # Step 2.1: Restore orphan data from backup tables (if they exist)
    op.execute(
        """
        INSERT INTO users
        SELECT * FROM _backup_orphan_users b
        WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.id = b.id)
          AND EXISTS (SELECT 1 FROM information_schema.tables
                      WHERE table_name = '_backup_orphan_users')
        """,
    )

    # Step 3: Update FK references back to users.id
    op.drop_constraint("recharge_user_id_fkey", "recharge", type_="foreignkey")
    op.drop_constraint(
        "credit_card_fingerprint_user_id_fkey",
        "credit_card_fingerprint",
        type_="foreignkey",
    )

    # Restore orphan recharges and credit card fingerprints before creating FKs
    op.execute(
        """
        INSERT INTO recharge
        SELECT * FROM _backup_orphan_recharge b
        WHERE NOT EXISTS (SELECT 1 FROM recharge r WHERE r.id = b.id)
          AND EXISTS (SELECT 1 FROM information_schema.tables
                      WHERE table_name = '_backup_orphan_recharge')
        """,
    )
    op.execute(
        """
        INSERT INTO credit_card_fingerprint
        SELECT * FROM _backup_orphan_credit_card_fingerprint b
        WHERE NOT EXISTS (SELECT 1 FROM credit_card_fingerprint c WHERE c.id = b.id)
          AND EXISTS (SELECT 1 FROM information_schema.tables
                      WHERE table_name = '_backup_orphan_credit_card_fingerprint')
        """,
    )

    op.create_foreign_key(
        "recharge_user_id_fkey",
        "recharge",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "credit_card_fingerprint_user_id_fkey",
        "credit_card_fingerprint",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # Step 3.1: Drop backup tables
    op.execute("DROP TABLE IF EXISTS _backup_orphan_recharge")
    op.execute("DROP TABLE IF EXISTS _backup_orphan_credit_card_fingerprint")
    op.execute("DROP TABLE IF EXISTS _backup_orphan_users")

    # Step 4: Drop billing columns from user table
    op.drop_column("user", "credits")
    op.drop_column("user", "stripe_customer_id")
    op.drop_column("user", "autorecharge")
    op.drop_column("user", "autorecharge_threshold")
    op.drop_column("user", "autorecharge_qty")
    op.drop_column("user", "store_prompts")
    op.drop_column("user", "frozen")
    op.drop_column("user", "credit_balance")
    op.drop_column("user", "billing_state")

    # Step 5: Rename indexes back
    op.execute('ALTER INDEX IF EXISTS "user_pkey" RENAME TO "auth_user_pkey"')
    op.execute('ALTER INDEX IF EXISTS "ix_user_email" RENAME TO "ix_auth_user_email"')

    # Step 6: Rename user back to auth_user
    op.rename_table("user", "auth_user")
