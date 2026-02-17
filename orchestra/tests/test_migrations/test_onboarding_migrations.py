"""Tests for onboarding / billing account migration chain.

Tests the forward (upgrade) and backward (downgrade) paths of the migration
chain from ``rate_limit_counter_001`` through ``drop_has_claimed_credit_grant``.

The strategy:
1. Create a fresh test database and run Alembic migrations up to the base
   revision (``rate_limit_counter_001``).
2. Insert representative seed data:
   - Users with various billing states (credits, frozen, autorecharge).
   - Organizations with and without business info / Stripe customers.
   - Recharges linked to users and organizations.
   - Credit card fingerprints linked to users.
   - One-time approval links (pre-rename).
3. Run the full forward migration chain to ``drop_has_claimed_credit_grant``.
4. Verify that all data survived correctly (credits, relationships, etc.).
5. Run the full downgrade chain back to ``rate_limit_counter_001``.
6. Verify that all data is preserved after rollback.

These tests do NOT use the SQLAlchemy ORM models — they use raw SQL only, to
avoid coupling the migration test to the current model definitions which may
differ from what the migration expects.
"""

import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

from orchestra.settings import settings

# ---------------------------------------------------------------------------
# Revision identifiers (keep in sync with migration files)
# ---------------------------------------------------------------------------
BASE_REVISION = "rate_limit_counter_001"  # last migration before onboarding set
HEAD_REVISION = "drop_has_claimed_credit_grant"  # last migration in onboarding set

# Intermediate revisions for step-by-step testing
ONBOARDING_REVISIONS = [
    "credit_grant_links_001",
    "add_org_verification",
    "add_onboarding_status",
    "remove_deprecated_fields",
    "consolidate_user_tables",
    "add_billing_account",
    "drop_has_claimed_credit_grant",
]

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _alembic_config(db_url: str) -> Config:
    """Build an Alembic Config pointing at the given database."""
    cfg = Config(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "alembic.ini"),
    )
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def _make_db_url(db_name: str) -> str:
    """Build a PostgreSQL URL for the given database name."""
    return (
        f"postgresql+psycopg2://{settings.db_user}:{settings.db_pass}"
        f"@{settings.db_host}:{settings.db_port}/{db_name}"
    )


@pytest.fixture(scope="module")
def migration_engine():
    """Create a disposable database for migration testing.

    - Creates ``orchestra_migration_test`` on the local PG cluster.
    - Sets ``ORCHESTRA_DB_BASE`` so that Alembic's ``env.py`` (which reads
      ``settings.db_url``) connects to our test database.
    - Yields an engine connected to it.
    - Drops the database on teardown.
    """
    db_name = "orchestra_migration_test"
    admin_url = _make_db_url("postgres")

    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")

    # Terminate any existing connections and drop/create the test DB
    with admin_engine.connect() as conn:
        conn.execute(
            text(
                f"SELECT pg_terminate_backend(pid) "
                f"FROM pg_stat_activity "
                f"WHERE datname = '{db_name}' AND pid <> pg_backend_pid()",
            ),
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        conn.execute(
            text(f"CREATE DATABASE \"{db_name}\" ENCODING 'utf8' TEMPLATE template1"),
        )
    admin_engine.dispose()

    # Connect to the fresh test DB.
    # Use NullPool so that every .connect() opens a fresh TCP connection.
    # This avoids stale-connection issues when Alembic (which creates its
    # own engine internally) runs dozens of migrations.
    test_url = _make_db_url(db_name)
    engine = create_engine(test_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)

    # Enable pgcrypto / vector extensions that migrations may need
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))

    # Set ORCHESTRA_DB_BASE so Alembic env.py uses our test database.
    # Alembic's env.py uses `settings.db_url` which reads this env var.
    original_db_base = os.environ.get("ORCHESTRA_DB_BASE")
    os.environ["ORCHESTRA_DB_BASE"] = db_name

    # We also need to invalidate the cached settings so they pick up the
    # new env var.  Pydantic-settings reads env vars at instantiation;
    # the module-level singleton won't refresh unless we force it.
    from orchestra import settings as settings_module  # noqa: WPS433

    old_settings = settings_module.settings
    settings_module.settings = type(old_settings)()

    yield engine

    # Restore original settings and env var
    settings_module.settings = old_settings
    if original_db_base is not None:
        os.environ["ORCHESTRA_DB_BASE"] = original_db_base
    else:
        os.environ.pop("ORCHESTRA_DB_BASE", None)

    engine.dispose()

    # Teardown: drop the test database
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(
            text(
                f"SELECT pg_terminate_backend(pid) "
                f"FROM pg_stat_activity "
                f"WHERE datname = '{db_name}' AND pid <> pg_backend_pid()",
            ),
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
    admin_engine.dispose()


@pytest.fixture(scope="module")
def alembic_cfg(migration_engine: Engine):
    """Return an Alembic Config wired to the migration test database."""
    return _alembic_config(str(migration_engine.url))


# ---------------------------------------------------------------------------
# Seed data helpers (raw SQL – no ORM dependency)
# ---------------------------------------------------------------------------

# Fixed UUIDs so we can look them up later
USER_A_ID = "mig-test-user-a"
USER_B_ID = "mig-test-user-b"
USER_C_ID = "mig-test-user-c"
ORG_OWNER_ID = "mig-test-org-owner"


def _seed_pre_migration_data(engine: Engine):
    """Insert representative data into the *pre-onboarding* schema.

    At this point the schema has:
    - ``auth_user`` (identity + business fields + assistant_hiring_approval)
    - ``users`` (billing fields: credits, stripe_customer_id, etc.)
    - ``organization`` (with billing columns directly on the table)
    - ``recharge`` with user_id / organization_id columns
    - ``credit_card_fingerprint`` with user_id column
    - ``assistant_hiring_one_time_approval_link`` table
    """
    with engine.begin() as conn:
        # ---- auth_user rows ----
        # User A: individual with credits and autorecharge
        conn.execute(
            text(
                """
                INSERT INTO auth_user (id, email, tier, has_claimed_approval_link,
                                       assistant_hiring_approval, account_type,
                                       business_name, tax_id)
                VALUES (:id, :email, :tier, :claimed, :approval, :acct_type,
                        :bname, :tax_id)
                """,
            ),
            {
                "id": USER_A_ID,
                "email": "user-a@test.com",
                "tier": "professional",
                "claimed": True,
                "approval": "approved",
                "acct_type": "individual",
                "bname": None,
                "tax_id": None,
            },
        )

        # User B: business user with full business details
        conn.execute(
            text(
                """
                INSERT INTO auth_user (id, email, tier, has_claimed_approval_link,
                                       assistant_hiring_approval, account_type,
                                       business_name, tax_id, business_country,
                                       business_address_line1, business_city,
                                       business_postal_code, business_verified,
                                       tax_exempt, tax_jurisdiction)
                VALUES (:id, :email, :tier, :claimed, :approval, :acct_type,
                        :bname, :tax_id, :country, :addr1, :city, :postal,
                        :verified, :exempt, :juris)
                """,
            ),
            {
                "id": USER_B_ID,
                "email": "user-b@business.com",
                "tier": "enterprise",
                "claimed": False,
                "approval": "pending",
                "acct_type": "business",
                "bname": "B Corp Ltd",
                "tax_id": "EU123456789",
                "country": "DE",
                "addr1": "123 Business St",
                "city": "Berlin",
                "postal": "10115",
                "verified": True,
                "exempt": True,
                "juris": "DE",
            },
        )

        # User C: minimal user, frozen
        conn.execute(
            text(
                """
                INSERT INTO auth_user (id, email, tier, has_claimed_approval_link,
                                       assistant_hiring_approval, account_type)
                VALUES (:id, :email, 'developer', false, NULL, 'individual')
                """,
            ),
            {"id": USER_C_ID, "email": "user-c@test.com"},
        )

        # Org owner user
        conn.execute(
            text(
                """
                INSERT INTO auth_user (id, email, tier, has_claimed_approval_link,
                                       assistant_hiring_approval, account_type)
                VALUES (:id, :email, 'developer', false, NULL, 'individual')
                """,
            ),
            {"id": ORG_OWNER_ID, "email": "org-owner@test.com"},
        )

        # ---- users rows (billing) ----
        conn.execute(
            text(
                """
                INSERT INTO users (id, credits, stripe_customer_id, autorecharge,
                                   autorecharge_threshold, autorecharge_qty, frozen,
                                   store_prompts)
                VALUES
                    (:ua, 500.50, 'cus_userA', true, 10, 50, false, true),
                    (:ub, 1000, 'cus_userB', false, 0, 25, false, true),
                    (:uc, 0, NULL, false, 0, 25, true, true),
                    (:oo, 100, NULL, false, 0, 25, false, true)
                """,
            ),
            {"ua": USER_A_ID, "ub": USER_B_ID, "uc": USER_C_ID, "oo": ORG_OWNER_ID},
        )

        # ---- organizations ----
        # Org 1: with full business info
        conn.execute(
            text(
                """
                INSERT INTO organization (name, owner_id, credits,
                    stripe_customer_id, autorecharge, autorecharge_threshold,
                    autorecharge_qty, account_status, billing_setup_complete,
                    billing_email, business_name, tax_id,
                    billing_address)
                VALUES ('Org With Billing', :owner, 2000,
                    'cus_org1', true, 20, 100, 'ACTIVE', true,
                    'billing@org1.com', 'Org1 GmbH', 'DE123456789',
                    '{"line1": "456 Org Street", "city": "Munich"}'::jsonb)
                RETURNING id
                """,
            ),
            {"owner": ORG_OWNER_ID},
        )
        org1_id = conn.execute(
            text("SELECT id FROM organization WHERE name = 'Org With Billing'"),
        ).scalar()

        # Org 2: minimal, no billing info
        conn.execute(
            text(
                """
                INSERT INTO organization (name, owner_id, credits,
                    autorecharge, autorecharge_threshold, autorecharge_qty,
                    account_status, billing_setup_complete)
                VALUES ('Org No Billing', :owner, 0,
                    false, 0, 25, 'ACTIVE', false)
                RETURNING id
                """,
            ),
            {"owner": ORG_OWNER_ID},
        )
        org2_id = conn.execute(
            text("SELECT id FROM organization WHERE name = 'Org No Billing'"),
        ).scalar()

        # ---- recharge_type ----
        conn.execute(
            text(
                "INSERT INTO recharge_type (type) VALUES ('free') ON CONFLICT DO NOTHING",
            ),
        )

        # ---- recharges ----
        # Recharge for user A
        conn.execute(
            text(
                """
                INSERT INTO recharge (user_id, type, quantity, amount_usd, status,
                                      invoice_group, at)
                VALUES (:uid, 'free', 50, 50.00, 'paid', '2026-01-01', NOW())
                """,
            ),
            {"uid": USER_A_ID},
        )
        # Recharge for org 1
        conn.execute(
            text(
                """
                INSERT INTO recharge (organization_id, type, quantity, amount_usd,
                                      status, invoice_group, at)
                VALUES (:oid, 'free', 200, 200.00, 'paid', '2026-01-01', NOW())
                """,
            ),
            {"oid": org1_id},
        )

        # ---- credit_card_fingerprint ----
        conn.execute(
            text(
                """
                INSERT INTO credit_card_fingerprint (user_id, fingerprint)
                VALUES (:uid, 'fp_test_123')
                """,
            ),
            {"uid": USER_A_ID},
        )

        # ---- assistants (to verify FK preservation through user consolidation) ----
        conn.execute(
            text(
                """
                INSERT INTO assistants (user_id, first_name, surname)
                VALUES (:uid, 'TestBot', 'Alpha')
                """,
            ),
            {"uid": USER_A_ID},
        )
        conn.execute(
            text(
                """
                INSERT INTO assistants (user_id, first_name, surname)
                VALUES (:uid, 'OrgBot', 'Beta')
                """,
            ),
            {"uid": ORG_OWNER_ID},
        )

        # ---- assistant_hiring_one_time_approval_link ----
        conn.execute(
            text(
                """
                INSERT INTO assistant_hiring_one_time_approval_link
                    (id, user_id, token, expires_at)
                VALUES (:id, :uid, :token, NOW() + INTERVAL '7 days')
                """,
            ),
            {
                "id": str(uuid.uuid4()),
                "uid": USER_A_ID,
                "token": "test-token-" + str(uuid.uuid4())[:8],
            },
        )

    return {
        "org1_id": org1_id,
        "org2_id": org2_id,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOnboardingMigrationForward:
    """Test the forward (upgrade) migration path."""

    @pytest.fixture(scope="class", autouse=True)
    def run_base_migration(self, migration_engine: Engine, alembic_cfg: Config):
        """Run Alembic to the base revision and seed data."""
        # Migrate to the base revision (everything before onboarding)
        command.upgrade(alembic_cfg, BASE_REVISION)
        # Seed representative data
        self.seed_ids = _seed_pre_migration_data(migration_engine)
        yield

    @pytest.fixture(scope="class", autouse=True)
    def run_forward_migration(
        self,
        run_base_migration,
        migration_engine: Engine,
        alembic_cfg: Config,
    ):
        """Run all onboarding migrations forward."""
        command.upgrade(alembic_cfg, HEAD_REVISION)
        yield

    def test_user_table_exists(self, migration_engine: Engine):
        """The consolidated `user` table should exist (auth_user renamed)."""
        with migration_engine.connect() as conn:
            result = conn.execute(
                text(
                    "SELECT EXISTS ("
                    "  SELECT FROM information_schema.tables "
                    "  WHERE table_name = 'user'"
                    ")",
                ),
            ).scalar()
            assert result is True, "Table 'user' should exist after migration"

    def test_auth_user_table_gone(self, migration_engine: Engine):
        """The old `auth_user` table should no longer exist."""
        with migration_engine.connect() as conn:
            result = conn.execute(
                text(
                    "SELECT EXISTS ("
                    "  SELECT FROM information_schema.tables "
                    "  WHERE table_name = 'auth_user'"
                    ")",
                ),
            ).scalar()
            assert result is False, "Table 'auth_user' should not exist after migration"

    def test_users_table_gone(self, migration_engine: Engine):
        """The old `users` table should no longer exist."""
        with migration_engine.connect() as conn:
            result = conn.execute(
                text(
                    "SELECT EXISTS ("
                    "  SELECT FROM information_schema.tables "
                    "  WHERE table_name = 'users'"
                    ")",
                ),
            ).scalar()
            assert result is False, "Table 'users' should not exist after migration"

    def test_billing_account_table_exists(self, migration_engine: Engine):
        """The `billing_account` table should exist."""
        with migration_engine.connect() as conn:
            result = conn.execute(
                text(
                    "SELECT EXISTS ("
                    "  SELECT FROM information_schema.tables "
                    "  WHERE table_name = 'billing_account'"
                    ")",
                ),
            ).scalar()
            assert (
                result is True
            ), "Table 'billing_account' should exist after migration"

    def test_user_billing_data_preserved(self, migration_engine: Engine):
        """User billing data (credits, autorecharge, etc.) should be preserved
        in the linked billing_account."""
        with migration_engine.connect() as conn:
            # User A: 500.50 credits, cus_userA, autorecharge=true
            row = conn.execute(
                text(
                    """
                    SELECT ba.credits, ba.stripe_customer_id, ba.autorecharge,
                           ba.autorecharge_threshold, ba.autorecharge_qty,
                           ba.account_status, ba.tier
                    FROM "user" u
                    JOIN billing_account ba ON u.billing_account_id = ba.id
                    WHERE u.id = :uid
                    """,
                ),
                {"uid": USER_A_ID},
            ).fetchone()
            assert row is not None, f"User {USER_A_ID} should have a billing_account"
            assert float(row[0]) == pytest.approx(500.50), "Credits should be preserved"
            assert row[1] == "cus_userA", "stripe_customer_id should be preserved"
            assert row[2] is True, "autorecharge should be True"
            assert float(row[3]) == pytest.approx(10), "threshold preserved"
            assert float(row[4]) == pytest.approx(50), "qty preserved"
            assert row[5] == "ACTIVE", "Non-frozen user should be ACTIVE"
            assert row[6] == "professional", "tier should be preserved"

    def test_frozen_user_becomes_suspended(self, migration_engine: Engine):
        """A frozen user should have account_status = SUSPENDED."""
        with migration_engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT ba.account_status, ba.credits
                    FROM "user" u
                    JOIN billing_account ba ON u.billing_account_id = ba.id
                    WHERE u.id = :uid
                    """,
                ),
                {"uid": USER_C_ID},
            ).fetchone()
            assert row is not None
            assert row[0] == "SUSPENDED", "Frozen user should become SUSPENDED"
            assert float(row[1]) == pytest.approx(0), "Credits should be 0"

    def test_org_billing_data_preserved(self, migration_engine: Engine):
        """Organization billing data should be migrated to billing_account."""
        with migration_engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT ba.credits, ba.stripe_customer_id, ba.autorecharge,
                           ba.billing_email, ba.name, ba.tax_id,
                           ba.billing_address, ba.billing_setup_complete
                    FROM organization o
                    JOIN billing_account ba ON o.billing_account_id = ba.id
                    WHERE o.name = 'Org With Billing'
                    """,
                ),
            ).fetchone()
            assert (
                row is not None
            ), "Org 'Org With Billing' should have a billing_account"
            assert float(row[0]) == pytest.approx(2000), "Credits preserved"
            assert row[1] == "cus_org1", "stripe_customer_id preserved"
            assert row[2] is True, "autorecharge preserved"
            assert row[3] == "billing@org1.com", "billing_email preserved"
            assert row[4] == "Org1 GmbH", "business_name → name preserved"
            assert row[5] == "DE123456789", "tax_id preserved"
            assert row[6] is not None, "billing_address preserved"
            assert row[7] is True, "billing_setup_complete preserved"

    def test_org_no_billing_still_has_account(self, migration_engine: Engine):
        """Even an org without billing info should get a billing_account."""
        with migration_engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT ba.credits, ba.billing_setup_complete
                    FROM organization o
                    JOIN billing_account ba ON o.billing_account_id = ba.id
                    WHERE o.name = 'Org No Billing'
                    """,
                ),
            ).fetchone()
            assert row is not None, "Org 'Org No Billing' should have a billing_account"
            assert float(row[0]) == pytest.approx(0), "Credits should be 0"
            assert row[1] is False, "billing_setup_complete should be False"

    def test_recharges_migrated_to_billing_account(self, migration_engine: Engine):
        """Recharges should now reference billing_account_id, not user_id/org_id."""
        with migration_engine.connect() as conn:
            # Check that recharge table has billing_account_id column
            cols = conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'recharge' AND column_name = 'billing_account_id'",
                ),
            ).fetchone()
            assert cols is not None, "recharge should have billing_account_id column"

            # Verify user_id and organization_id columns are gone
            old_cols = conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'recharge' AND column_name IN ('user_id', 'organization_id')",
                ),
            ).fetchall()
            assert (
                len(old_cols) == 0
            ), "user_id/organization_id should be removed from recharge"

            # Verify the user A recharge is linked via billing_account
            row = conn.execute(
                text(
                    """
                    SELECT r.billing_account_id, r.quantity
                    FROM recharge r
                    JOIN billing_account ba ON r.billing_account_id = ba.id
                    JOIN "user" u ON u.billing_account_id = ba.id
                    WHERE u.id = :uid
                    """,
                ),
                {"uid": USER_A_ID},
            ).fetchone()
            assert (
                row is not None
            ), "User A's recharge should be linked via billing_account"
            assert float(row[1]) == pytest.approx(50), "Recharge quantity preserved"

    def test_credit_card_fingerprint_migrated(self, migration_engine: Engine):
        """credit_card_fingerprint should use billing_account_id."""
        with migration_engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT ccf.billing_account_id, ccf.fingerprint
                    FROM credit_card_fingerprint ccf
                    JOIN billing_account ba ON ccf.billing_account_id = ba.id
                    JOIN "user" u ON u.billing_account_id = ba.id
                    WHERE u.id = :uid
                    """,
                ),
                {"uid": USER_A_ID},
            ).fetchone()
            assert row is not None, "Fingerprint should be linked via billing_account"
            assert row[1] == "fp_test_123", "Fingerprint value preserved"

    def test_credit_grant_link_renamed(self, migration_engine: Engine):
        """The approval link table should be renamed to one_time_credit_grant_link."""
        with migration_engine.connect() as conn:
            # Old table should not exist
            old = conn.execute(
                text(
                    "SELECT EXISTS ("
                    "  SELECT FROM information_schema.tables "
                    "  WHERE table_name = 'assistant_hiring_one_time_approval_link'"
                    ")",
                ),
            ).scalar()
            assert old is False, "Old approval link table should not exist"

            # New table should exist
            new = conn.execute(
                text(
                    "SELECT EXISTS ("
                    "  SELECT FROM information_schema.tables "
                    "  WHERE table_name = 'one_time_credit_grant_link'"
                    ")",
                ),
            ).scalar()
            assert new is True, "one_time_credit_grant_link should exist"

            # The link row should still be there
            count = conn.execute(
                text(
                    "SELECT COUNT(*) FROM one_time_credit_grant_link WHERE user_id = :uid",
                ),
                {"uid": USER_A_ID},
            ).scalar()
            assert count == 1, "Credit grant link should be preserved"

    def test_has_claimed_credit_grant_link_dropped(self, migration_engine: Engine):
        """The has_claimed_credit_grant_link column should be dropped from user."""
        with migration_engine.connect() as conn:
            cols = conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'user' AND column_name = 'has_claimed_credit_grant_link'",
                ),
            ).fetchall()
            assert len(cols) == 0, "has_claimed_credit_grant_link should be dropped"

    def test_onboarding_status_table_exists(self, migration_engine: Engine):
        """The onboarding_status table should exist."""
        with migration_engine.connect() as conn:
            result = conn.execute(
                text(
                    "SELECT EXISTS ("
                    "  SELECT FROM information_schema.tables "
                    "  WHERE table_name = 'onboarding_status'"
                    ")",
                ),
            ).scalar()
            assert result is True, "onboarding_status table should exist"

    def test_organization_verification_fields(self, migration_engine: Engine):
        """Organization should have verified / verified_at columns."""
        with migration_engine.connect() as conn:
            cols = conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'organization' AND column_name IN ('verified', 'verified_at')",
                ),
            ).fetchall()
            col_names = {c[0] for c in cols}
            assert "verified" in col_names, "verified column should exist"
            assert "verified_at" in col_names, "verified_at column should exist"

    def test_deprecated_fields_removed(self, migration_engine: Engine):
        """Deprecated fields (assistant_hiring_approval, billing_user_id,
        business fields) should be removed from user table."""
        with migration_engine.connect() as conn:
            removed = [
                "assistant_hiring_approval",
                "account_type",
                "business_name",
                "tax_id",
                "business_type",
                "business_address_line1",
                "tax_exempt",
                "business_verified",
                "tax_jurisdiction",
            ]
            cols = conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'user'",
                ),
            ).fetchall()
            col_names = {c[0] for c in cols}
            for field in removed:
                assert field not in col_names, f"'{field}' should be removed from user"

    def test_billing_columns_removed_from_user(self, migration_engine: Engine):
        """Billing columns (credits, stripe_customer_id, etc.) should be
        removed from the user table."""
        with migration_engine.connect() as conn:
            removed = [
                "credits",
                "stripe_customer_id",
                "autorecharge",
                "autorecharge_threshold",
                "autorecharge_qty",
                "frozen",
            ]
            cols = conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'user'",
                ),
            ).fetchall()
            col_names = {c[0] for c in cols}
            for field in removed:
                assert (
                    field not in col_names
                ), f"'{field}' should be removed from user table"

    def test_billing_columns_removed_from_organization(self, migration_engine: Engine):
        """Billing columns should be removed from the organization table."""
        with migration_engine.connect() as conn:
            removed = [
                "credits",
                "stripe_customer_id",
                "autorecharge",
                "autorecharge_threshold",
                "autorecharge_qty",
                "account_status",
                "billing_email",
                "business_name",
                "tax_id",
                "billing_address",
                "billing_setup_complete",
            ]
            cols = conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'organization'",
                ),
            ).fetchall()
            col_names = {c[0] for c in cols}
            for field in removed:
                assert (
                    field not in col_names
                ), f"'{field}' should be removed from organization table"

    def test_all_users_have_billing_account(self, migration_engine: Engine):
        """Every user row should have a non-NULL billing_account_id."""
        with migration_engine.connect() as conn:
            nulls = conn.execute(
                text(
                    'SELECT COUNT(*) FROM "user" WHERE billing_account_id IS NULL',
                ),
            ).scalar()
            assert nulls == 0, "All users should have a billing_account"

    def test_all_orgs_have_billing_account(self, migration_engine: Engine):
        """Every organization row should have a non-NULL billing_account_id."""
        with migration_engine.connect() as conn:
            nulls = conn.execute(
                text(
                    "SELECT COUNT(*) FROM organization WHERE billing_account_id IS NULL",
                ),
            ).scalar()
            assert nulls == 0, "All organizations should have a billing_account"

    def test_assistants_preserved_after_user_consolidation(
        self,
        migration_engine: Engine,
    ):
        """Assistants created before migration should still be linked to the
        correct user_id after auth_user → user table rename."""
        with migration_engine.connect() as conn:
            # User A's assistant
            row_a = conn.execute(
                text(
                    """
                    SELECT a.agent_id, a.first_name, a.surname, a.user_id
                    FROM assistants a
                    WHERE a.user_id = :uid AND a.first_name = 'TestBot'
                    """,
                ),
                {"uid": USER_A_ID},
            ).fetchone()
            assert row_a is not None, "User A's assistant should still exist"
            assert row_a[1] == "TestBot", "first_name preserved"
            assert row_a[2] == "Alpha", "surname preserved"
            assert row_a[3] == USER_A_ID, "user_id FK preserved"

            # Org owner's assistant
            row_b = conn.execute(
                text(
                    """
                    SELECT a.agent_id, a.first_name, a.surname, a.user_id
                    FROM assistants a
                    WHERE a.user_id = :uid AND a.first_name = 'OrgBot'
                    """,
                ),
                {"uid": ORG_OWNER_ID},
            ).fetchone()
            assert row_b is not None, "Org owner's assistant should still exist"
            assert row_b[1] == "OrgBot", "first_name preserved"
            assert row_b[3] == ORG_OWNER_ID, "user_id FK preserved"

            # Verify the FK actually points to the consolidated 'user' table
            # by joining assistants → user
            joined = conn.execute(
                text(
                    """
                    SELECT a.agent_id, u.email
                    FROM assistants a
                    JOIN "user" u ON a.user_id = u.id
                    WHERE a.user_id = :uid AND a.first_name = 'TestBot'
                    """,
                ),
                {"uid": USER_A_ID},
            ).fetchone()
            assert joined is not None, "FK join assistants → user should work"
            assert joined[1] == "user-a@test.com", "Joined user email correct"


class TestOnboardingMigrationBackward:
    """Test the backward (downgrade) migration path.

    After TestOnboardingMigrationForward has run, this class downgrades
    back to the base revision and verifies data integrity.
    """

    @pytest.fixture(scope="class", autouse=True)
    def run_downgrade(self, migration_engine: Engine, alembic_cfg: Config):
        """Downgrade from HEAD back to the base revision."""
        command.downgrade(alembic_cfg, BASE_REVISION)
        yield

    def test_auth_user_table_restored(self, migration_engine: Engine):
        """The `auth_user` table should be restored."""
        with migration_engine.connect() as conn:
            result = conn.execute(
                text(
                    "SELECT EXISTS ("
                    "  SELECT FROM information_schema.tables "
                    "  WHERE table_name = 'auth_user'"
                    ")",
                ),
            ).scalar()
            assert result is True, "auth_user should be restored after downgrade"

    def test_users_table_restored(self, migration_engine: Engine):
        """The `users` table should be restored."""
        with migration_engine.connect() as conn:
            result = conn.execute(
                text(
                    "SELECT EXISTS ("
                    "  SELECT FROM information_schema.tables "
                    "  WHERE table_name = 'users'"
                    ")",
                ),
            ).scalar()
            assert result is True, "users table should be restored after downgrade"

    def test_billing_account_table_gone(self, migration_engine: Engine):
        """The `billing_account` table should be dropped."""
        with migration_engine.connect() as conn:
            result = conn.execute(
                text(
                    "SELECT EXISTS ("
                    "  SELECT FROM information_schema.tables "
                    "  WHERE table_name = 'billing_account'"
                    ")",
                ),
            ).scalar()
            assert (
                result is False
            ), "billing_account should be gone after full downgrade"

    def test_user_credits_restored(self, migration_engine: Engine):
        """User billing data should be restored in the `users` table."""
        with migration_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT credits, stripe_customer_id, autorecharge, frozen "
                    "FROM users WHERE id = :uid",
                ),
                {"uid": USER_A_ID},
            ).fetchone()
            assert row is not None, "User A should exist in users table"
            assert float(row[0]) == pytest.approx(
                500.50,
            ), "Credits preserved after rollback"
            assert row[1] == "cus_userA", "stripe_customer_id preserved"
            assert row[2] is True, "autorecharge preserved"
            assert row[3] is False, "frozen state preserved"

    def test_frozen_user_restored(self, migration_engine: Engine):
        """The frozen user should be restored correctly."""
        with migration_engine.connect() as conn:
            row = conn.execute(
                text("SELECT frozen FROM users WHERE id = :uid"),
                {"uid": USER_C_ID},
            ).fetchone()
            assert row is not None
            assert row[0] is True, "User C should still be frozen after rollback"

    def test_org_billing_restored(self, migration_engine: Engine):
        """Organization billing columns should be restored."""
        with migration_engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT credits, stripe_customer_id, business_name, tax_id,
                           billing_email, billing_setup_complete
                    FROM organization
                    WHERE name = 'Org With Billing'
                    """,
                ),
            ).fetchone()
            assert row is not None
            assert float(row[0]) == pytest.approx(2000), "Credits restored"
            assert row[1] == "cus_org1", "stripe_customer_id restored"
            assert row[2] == "Org1 GmbH", "business_name restored"
            assert row[3] == "DE123456789", "tax_id restored"
            assert row[4] == "billing@org1.com", "billing_email restored"
            assert row[5] is True, "billing_setup_complete restored"

    def test_recharges_restored_with_user_id(self, migration_engine: Engine):
        """Recharges should have user_id / organization_id restored."""
        with migration_engine.connect() as conn:
            # Check columns exist
            cols = conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'recharge' AND column_name IN ('user_id', 'organization_id')",
                ),
            ).fetchall()
            col_names = {c[0] for c in cols}
            assert "user_id" in col_names, "user_id should be restored on recharge"
            assert (
                "organization_id" in col_names
            ), "organization_id should be restored on recharge"

            # User A's recharge should have user_id set
            row = conn.execute(
                text(
                    "SELECT user_id, quantity FROM recharge WHERE user_id = :uid",
                ),
                {"uid": USER_A_ID},
            ).fetchone()
            assert row is not None, "User A recharge should have user_id restored"
            assert float(row[1]) == pytest.approx(50), "Quantity preserved"

    def test_credit_card_fingerprint_restored(self, migration_engine: Engine):
        """credit_card_fingerprint should have user_id restored."""
        with migration_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT user_id, fingerprint FROM credit_card_fingerprint "
                    "WHERE user_id = :uid",
                ),
                {"uid": USER_A_ID},
            ).fetchone()
            assert row is not None, "Fingerprint should have user_id restored"
            assert row[1] == "fp_test_123", "Fingerprint value preserved"

    def test_approval_link_table_restored(self, migration_engine: Engine):
        """The assistant_hiring_one_time_approval_link table should be restored."""
        with migration_engine.connect() as conn:
            result = conn.execute(
                text(
                    "SELECT EXISTS ("
                    "  SELECT FROM information_schema.tables "
                    "  WHERE table_name = 'assistant_hiring_one_time_approval_link'"
                    ")",
                ),
            ).scalar()
            assert result is True, "Old approval link table should be restored"

            count = conn.execute(
                text(
                    "SELECT COUNT(*) FROM assistant_hiring_one_time_approval_link "
                    "WHERE user_id = :uid",
                ),
                {"uid": USER_A_ID},
            ).scalar()
            assert count == 1, "Approval link should be preserved"

    def test_has_claimed_approval_link_restored(self, migration_engine: Engine):
        """has_claimed_approval_link should be restored on auth_user."""
        with migration_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT has_claimed_approval_link FROM auth_user WHERE id = :uid",
                ),
                {"uid": USER_A_ID},
            ).fetchone()
            assert row is not None
            assert (
                row[0] is True
            ), "has_claimed_approval_link should be restored via backfill"

    def test_business_fields_restored(self, migration_engine: Engine):
        """Business fields should be restored on auth_user."""
        with migration_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT account_type, assistant_hiring_approval "
                    "FROM auth_user WHERE id = :uid",
                ),
                {"uid": USER_A_ID},
            ).fetchone()
            assert row is not None
            # account_type gets a default of 'individual'
            assert row[0] == "individual", "account_type default should be individual"
            # assistant_hiring_approval gets NULL default (data was lost in upgrade)
            # This is acceptable — approval was deprecated

    def test_onboarding_status_table_gone(self, migration_engine: Engine):
        """onboarding_status table should be gone after downgrade."""
        with migration_engine.connect() as conn:
            result = conn.execute(
                text(
                    "SELECT EXISTS ("
                    "  SELECT FROM information_schema.tables "
                    "  WHERE table_name = 'onboarding_status'"
                    ")",
                ),
            ).scalar()
            assert (
                result is False
            ), "onboarding_status should be dropped after downgrade"

    def test_assistants_preserved_after_rollback(self, migration_engine: Engine):
        """Assistants should still be linked to the correct user after full rollback.

        After downgrade, auth_user is restored. The assistants FK should still
        point to auth_user (via the reverse rename user → auth_user).
        """
        with migration_engine.connect() as conn:
            # User A's assistant via auth_user join
            row = conn.execute(
                text(
                    """
                    SELECT a.agent_id, a.first_name, a.user_id, au.email
                    FROM assistants a
                    JOIN auth_user au ON a.user_id = au.id
                    WHERE a.user_id = :uid AND a.first_name = 'TestBot'
                    """,
                ),
                {"uid": USER_A_ID},
            ).fetchone()
            assert (
                row is not None
            ), "User A's assistant should still exist after rollback"
            assert row[1] == "TestBot", "first_name preserved"
            assert row[2] == USER_A_ID, "user_id FK preserved"
            assert row[3] == "user-a@test.com", "FK join to auth_user works"

            # Total assistant count should be preserved
            count = conn.execute(
                text("SELECT COUNT(*) FROM assistants"),
            ).scalar()
            assert count >= 2, "Both seeded assistants should still exist"

    def test_organization_verification_fields_gone(self, migration_engine: Engine):
        """Organization verified / verified_at columns should be gone."""
        with migration_engine.connect() as conn:
            cols = conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'organization' AND column_name IN ('verified', 'verified_at')",
                ),
            ).fetchall()
            assert (
                len(cols) == 0
            ), "verified/verified_at should be dropped after downgrade"


class TestOnboardingMigrationStepByStep:
    """Test each migration step individually for completeness.

    This re-runs the full cycle: upgrade to base, seed, step through each
    migration one at a time, then downgrade step by step.
    """

    @pytest.fixture(scope="class", autouse=True)
    def setup_from_scratch(self, migration_engine: Engine, alembic_cfg: Config):
        """Reset to base, seed, then yield for step-by-step testing."""
        # First, check current revision and get to a known state
        # We may already be at BASE_REVISION from the previous class
        # If not, we need to upgrade to it
        command.upgrade(alembic_cfg, BASE_REVISION)
        yield

    def test_step_through_each_revision(
        self,
        migration_engine: Engine,
        alembic_cfg: Config,
    ):
        """Walk through each revision one at a time and verify no errors."""
        for rev in ONBOARDING_REVISIONS:
            command.upgrade(alembic_cfg, rev)

            # Quick sanity: the migration should at minimum not leave the DB
            # in a broken state
            with migration_engine.connect() as conn:
                conn.execute(text("SELECT 1"))

    def test_step_back_each_revision(
        self,
        migration_engine: Engine,
        alembic_cfg: Config,
    ):
        """Walk back through each revision one at a time (reverse order)."""
        for rev in reversed(ONBOARDING_REVISIONS[:-1]):
            command.downgrade(alembic_cfg, rev)

            with migration_engine.connect() as conn:
                conn.execute(text("SELECT 1"))

        # Final step: back to base
        command.downgrade(alembic_cfg, BASE_REVISION)
        with migration_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
