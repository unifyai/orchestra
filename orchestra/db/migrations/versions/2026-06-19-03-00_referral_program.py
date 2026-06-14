"""Referral program: shareable codes + payment-gated attribution.

Adds two tables:

* ``referral_code`` — shareable codes owned by a user. A user may own
  many codes; all resolve back to the same referrer.
* ``referral_attribution`` — one row per referred signup. The
  ``uq_referral_referee`` unique constraint guarantees a user can be
  referred at most once. The reward is granted (and later reversed on
  refund/chargeback) by the Stripe webhook path, keyed on the friend's
  first qualifying paid subscription invoice.

Both reference existing ``user`` / ``billing_account`` tables; no changes
to those tables are required (attribution is recorded after signup via the
authenticated ``/v0/user/referral/attribute`` endpoint, mirroring the
one-time credit-grant-link claim flow).
"""

import sqlalchemy as sa
from alembic import op

revision = "referral_program"
down_revision = "project_public_read"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "referral_code",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("code", sa.String(), nullable=False),
        sa.Column("referrer_user_id", sa.String(), nullable=False),
        sa.Column("referrer_organization_id", sa.Integer(), nullable=True),
        sa.Column("label", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column("disabled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["referrer_user_id"],
            ["user.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["referrer_organization_id"],
            ["organization.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("code", name="uq_referral_code_code"),
    )
    op.create_index(
        "ix_referral_code_code",
        "referral_code",
        ["code"],
        unique=True,
    )
    op.create_index(
        "ix_referral_code_referrer_user_id",
        "referral_code",
        ["referrer_user_id"],
    )
    op.create_index(
        "ix_referral_code_referrer_organization_id",
        "referral_code",
        ["referrer_organization_id"],
    )

    op.create_table(
        "referral_attribution",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("code", sa.String(), nullable=False),
        sa.Column("referrer_user_id", sa.String(), nullable=False),
        sa.Column("referrer_organization_id", sa.Integer(), nullable=True),
        sa.Column("referee_user_id", sa.String(), nullable=False),
        sa.Column("referee_billing_account_id", sa.Integer(), nullable=True),
        sa.Column("referrer_billing_account_id", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            sa.String(),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("signup_ip", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column("rewarded_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("reversed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("first_payment_invoice_id", sa.String(), nullable=True),
        sa.Column("reward_amount", sa.Numeric(), nullable=True),
        sa.Column("referee_bonus_amount", sa.Numeric(), nullable=True),
        sa.ForeignKeyConstraint(["referrer_user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(
            ["referrer_organization_id"],
            ["organization.id"],
        ),
        sa.ForeignKeyConstraint(["referee_user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(
            ["referee_billing_account_id"],
            ["billing_account.id"],
        ),
        sa.ForeignKeyConstraint(
            ["referrer_billing_account_id"],
            ["billing_account.id"],
        ),
        sa.UniqueConstraint("referee_user_id", name="uq_referral_referee"),
    )
    op.create_index(
        "ix_referral_attribution_code",
        "referral_attribution",
        ["code"],
    )
    op.create_index(
        "ix_referral_attribution_referrer_user_id",
        "referral_attribution",
        ["referrer_user_id"],
    )
    op.create_index(
        "ix_referral_attribution_referrer_organization_id",
        "referral_attribution",
        ["referrer_organization_id"],
    )
    op.create_index(
        "ix_referral_attribution_referee_user_id",
        "referral_attribution",
        ["referee_user_id"],
    )
    op.create_index(
        "ix_referral_attribution_referee_billing_account_id",
        "referral_attribution",
        ["referee_billing_account_id"],
    )
    op.create_index(
        "ix_referral_attribution_first_payment_invoice_id",
        "referral_attribution",
        ["first_payment_invoice_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_referral_attribution_first_payment_invoice_id",
        table_name="referral_attribution",
    )
    op.drop_index(
        "ix_referral_attribution_referee_billing_account_id",
        table_name="referral_attribution",
    )
    op.drop_index(
        "ix_referral_attribution_referee_user_id",
        table_name="referral_attribution",
    )
    op.drop_index(
        "ix_referral_attribution_referrer_organization_id",
        table_name="referral_attribution",
    )
    op.drop_index(
        "ix_referral_attribution_referrer_user_id",
        table_name="referral_attribution",
    )
    op.drop_index(
        "ix_referral_attribution_code",
        table_name="referral_attribution",
    )
    op.drop_table("referral_attribution")

    op.drop_index(
        "ix_referral_code_referrer_organization_id",
        table_name="referral_code",
    )
    op.drop_index(
        "ix_referral_code_referrer_user_id",
        table_name="referral_code",
    )
    op.drop_index("ix_referral_code_code", table_name="referral_code")
    op.drop_table("referral_code")
