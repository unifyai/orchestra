"""Rename historically-named platform foreign keys to postgres-default form.

Twelve foreign keys on the platform side were given semantic
``fk_<table>_<short>`` names by their original migrations rather than
the postgres-default ``<table>_<column>_fkey`` form that
`sa.ForeignKey()` generates via `meta.create_all`. As a result, fresh
test databases (built from the model) and production databases have
different names for the *same* underlying constraints.

This migration renames the production constraints to match what the
model produces, eliminating the drift in one shot. After this migration
runs, `meta.create_all` (used in tests) and the production schema agree
on every constraint name.

Each rename is idempotent — wrapped in a `pg_constraint` existence
check — so it is safe on:

- Fresh DBs (rename is a no-op; the old names never existed).
- Already-converged DBs (rename is a no-op).
- Pre-converged production DBs (rename runs once).

`fk_assistants_voices` is left alone: it is a *composite* foreign key
that the model already names explicitly via
`ForeignKeyConstraint(name="fk_assistants_voices")`, so model and
production already agree.

This revision is also a *merge node*: its `down_revision` is a tuple
joining the two branches that emerge from `0001_core_initial`
(`_platform_initial` for the platform and `0002_kernel_drift_fixes`
for the kernel) into a single chain head, so `alembic upgrade head`
runs cleanly without ambiguity.

Revision ID: 2026_platform_drift_fixes
Revises: _platform_initial, 0002_kernel_drift_fixes
Create Date: 2026-05-22 23:00:00.000000
"""

from __future__ import annotations

from alembic import op

revision = "2026_platform_drift_fixes"
down_revision = ("_platform_initial", "0002_kernel_drift_fixes")
branch_labels = None
depends_on = None

# Each tuple is (table, legacy_name, postgres_default_name).
# The postgres-default name is what `meta.create_all` produces from a
# bare `sa.ForeignKey("ref.col")`, i.e. `<table>_<column>_fkey`.
_RENAMES: tuple[tuple[str, str, str], ...] = (
    ("assistants", "fk_assistants_demo_meta", "assistants_demo_id_fkey"),
    (
        "billing_account",
        "fk_billing_account_plan_assignment",
        "billing_account_plan_assignment_id_fkey",
    ),
    (
        "billing_account",
        "fk_billing_account_plan_group",
        "billing_account_plan_group_id_fkey",
    ),
    (
        "contact_memberships",
        "fk_contact_memberships_authoring_assistant_id",
        "contact_memberships_authoring_assistant_id_fkey",
    ),
    (
        "credit_transaction",
        "fk_credit_txn_plan_assignment",
        "credit_transaction_plan_assignment_id_fkey",
    ),
    (
        "demo_assistant_meta",
        "fk_demo_meta_demoer",
        "demo_assistant_meta_demoer_user_id_fkey",
    ),
    (
        "demo_assistant_meta",
        "fk_demo_meta_source_assistant",
        "demo_assistant_meta_source_assistant_id_fkey",
    ),
    (
        "organization",
        "fk_organization_billing_account_id",
        "organization_billing_account_id_fkey",
    ),
    (
        "organization_member",
        "fk_organization_member_role_id",
        "organization_member_role_id_fkey",
    ),
    (
        "recharge",
        "fk_recharge_billing_account_id",
        "recharge_billing_account_id_fkey",
    ),
    ("recharge", "fk_recharge_plan", "recharge_plan_id_fkey"),
    (
        "user",
        "fk_user_billing_account_id",
        "user_billing_account_id_fkey",
    ),
)


def _rename_if_present(table: str, old: str, new: str) -> None:
    """Rename a constraint only when the legacy name still exists.

    `user` is a reserved keyword so the table reference must be quoted.
    """
    quoted = f'"{table}"' if table == "user" else table
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = '{old}'
                  AND conrelid = 'public.{table}'::regclass
            ) THEN
                ALTER TABLE public.{quoted}
                    RENAME CONSTRAINT {old} TO {new};
            END IF;
        END$$;
        """)


def upgrade() -> None:
    for table, old, new in _RENAMES:
        _rename_if_present(table, old, new)


def downgrade() -> None:
    """Reverse the renames, again idempotently."""
    for table, old, new in _RENAMES:
        # Swap roles: rename `new` back to `old` if the new name is present.
        _rename_if_present(table, new, old)
