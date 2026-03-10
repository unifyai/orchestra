"""
Shared fixtures and helpers for E2E billing tests (webhooks & flows).

Provides:
- Stripe CLI availability check and skip markers
- Stripe configuration fixtures
- Stripe resource cleanup
- Server / webhook-forwarding guard fixtures
- Utility functions: stripe_trigger, wait_for_db_condition
- Factory helpers: create_stripe_customer, create_test_user_with_stripe,
  create_test_org_with_stripe
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from decimal import Decimal
from typing import Optional

import httpx
import pytest
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    Assistant,
    AssistantContact,
    BillingAccount,
    Organization,
    User,
)

# ---------------------------------------------------------------------------
# Shared test-entity factories (used by all billing test modules)
# ---------------------------------------------------------------------------


def make_billing_account(
    dbsession: Session,
    *,
    credits: float | Decimal = 0,
    account_status: str = "ACTIVE",
    stripe_customer_id: str | None = None,
    autorecharge: bool = False,
    autorecharge_threshold: float | Decimal = 0,
    autorecharge_qty: float | Decimal = 25,
) -> BillingAccount:
    """Create a standalone :class:`BillingAccount`."""
    ba = BillingAccount(
        credits=Decimal(str(credits)),
        account_status=account_status,
        stripe_customer_id=stripe_customer_id,
        autorecharge=autorecharge,
        autorecharge_threshold=Decimal(str(autorecharge_threshold)),
        autorecharge_qty=Decimal(str(autorecharge_qty)),
    )
    dbsession.add(ba)
    dbsession.flush()
    return ba


def make_user(
    dbsession: Session,
    uid: str,
    ba: BillingAccount,
    *,
    email: str | None = None,
) -> User:
    """Create a :class:`User` linked to an existing *ba*."""
    user = User(
        id=uid,
        email=email or f"{uid}@test.com",
        billing_account_id=ba.id,
    )
    dbsession.add(user)
    dbsession.flush()
    return user


def make_user_with_billing(
    dbsession: Session,
    uid: str,
    *,
    email: str | None = None,
    credits: float | Decimal = 0,
    stripe_customer_id: str | None = None,
    autorecharge: bool = False,
    autorecharge_threshold: float | Decimal = 0,
    autorecharge_qty: float | Decimal = 25,
    account_status: str = "ACTIVE",
) -> tuple[User, BillingAccount]:
    """Create a :class:`User` **and** its :class:`BillingAccount` in one step."""
    ba = make_billing_account(
        dbsession,
        credits=credits,
        stripe_customer_id=stripe_customer_id,
        autorecharge=autorecharge,
        autorecharge_threshold=autorecharge_threshold,
        autorecharge_qty=autorecharge_qty,
        account_status=account_status,
    )
    user = make_user(dbsession, uid, ba, email=email)
    return user, ba


def make_org(
    dbsession: Session,
    owner: User,
    ba: BillingAccount,
    name: str = "TestOrg",
) -> Organization:
    """Create an :class:`Organization` linked to *owner* and *ba*."""
    org = Organization(
        owner_id=owner.id,
        name=name,
        billing_account_id=ba.id,
    )
    dbsession.add(org)
    dbsession.flush()
    return org


def make_org_with_billing(
    dbsession: Session,
    name: str,
    stripe_customer_id: str | None,
    credits: float | Decimal = 100,
) -> tuple[Organization, BillingAccount]:
    """Create an :class:`Organization` with owner, owner-BA, and org-BA."""
    owner_ba = make_billing_account(dbsession, credits=Decimal("100"))
    owner_id = f"owner_{name.replace(' ', '_').lower()}"
    owner = make_user(dbsession, owner_id, owner_ba)

    org_ba = make_billing_account(
        dbsession,
        credits=credits,
        stripe_customer_id=stripe_customer_id,
    )
    org = make_org(dbsession, owner, org_ba, name=name)
    return org, org_ba


def make_assistant(
    dbsession: Session,
    user_id: str,
    first_name: str = "Test",
    surname: str = "Bot",
    organization_id: int | None = None,
    demo_id: int | None = None,
) -> Assistant:
    """Create an :class:`Assistant`."""
    a = Assistant(
        user_id=user_id,
        first_name=first_name,
        surname=surname,
        organization_id=organization_id,
        demo_id=demo_id,
    )
    dbsession.add(a)
    dbsession.flush()
    return a


def make_contact(
    dbsession: Session,
    assistant_id: int,
    contact_type: str = "phone",
    contact_value: str = "+15551000001",
    provider: str | None = "twilio",
    country_code: str | None = "US",
    provisioned_by: str = "platform",
    status: str = "active",
    last_billed_month: str | None = None,
) -> AssistantContact:
    """Create an :class:`AssistantContact`."""
    c = AssistantContact(
        assistant_id=assistant_id,
        contact_type=contact_type,
        contact_value=contact_value,
        provider=provider,
        country_code=country_code,
        provisioned_by=provisioned_by,
        status=status,
        last_billed_month=last_billed_month,
    )
    dbsession.add(c)
    dbsession.flush()
    return c


# ---------------------------------------------------------------------------
# Environment / availability checks
# ---------------------------------------------------------------------------

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
SERVER_URL = os.environ.get("ORCHESTRA_TEST_SERVER_URL", "http://localhost:8000")

SKIP_REASON = (
    "E2E webhook tests require: "
    "1) STRIPE_SECRET_KEY (sk_test_xxx), "
    "2) STRIPE_WEBHOOK_SECRET (whsec_xxx from stripe.sh), "
    "3) Stripe CLI installed, "
    "4) Local server running at localhost:8000, "
    "5) Webhook forwarding via ./scripts/stripe.sh bg"
)


def _check_stripe_cli() -> bool:
    try:
        result = subprocess.run(
            ["stripe", "version"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


STRIPE_CLI_AVAILABLE = _check_stripe_cli()


def _check_server_running() -> bool:
    try:
        response = httpx.get(f"{SERVER_URL}/v0/health", timeout=2)
        return response.status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Track Stripe customers for cleanup
# ---------------------------------------------------------------------------

_created_customers: list[str] = []


# ---------------------------------------------------------------------------
# Fixtures (auto-use)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _configure_stripe(monkeypatch):
    """Configure Stripe settings for E2E tests."""
    from orchestra.settings import settings

    if STRIPE_SECRET_KEY:
        monkeypatch.setattr(
            settings,
            "stripe_secret_key",
            STRIPE_SECRET_KEY,
            raising=False,
        )
    if STRIPE_WEBHOOK_SECRET:
        monkeypatch.setattr(
            settings,
            "stripe_webhook_secret",
            STRIPE_WEBHOOK_SECRET,
            raising=False,
        )


@pytest.fixture(autouse=True)
def _cleanup_stripe_resources():
    """Clean up Stripe resources after each test."""
    _created_customers.clear()
    yield

    if _created_customers and STRIPE_SECRET_KEY:
        import stripe

        stripe.api_key = STRIPE_SECRET_KEY

        for customer_id in _created_customers:
            try:
                stripe.Customer.delete(customer_id)
            except Exception:
                pass
        _created_customers.clear()


# ---------------------------------------------------------------------------
# Guard fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def require_server():
    """Skip test if server isn't running."""
    if not _check_server_running():
        pytest.skip(
            f"Server not running at {SERVER_URL}. "
            "Start with: poetry run python -m orchestra",
        )


@pytest.fixture
def require_webhook_forwarding():
    """Skip test if webhook forwarding isn't active."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "stripe listen"],
            capture_output=True,
        )
        if result.returncode != 0:
            pytest.skip("Webhook forwarding not active. Run: ./scripts/stripe.sh bg")
    except FileNotFoundError:
        pytest.skip("pgrep not available")


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def stripe_trigger(
    event_type: str,
    override: Optional[dict] = None,
    timeout: int = 30,
) -> tuple[bool, str]:
    """
    Trigger a Stripe test event using the CLI.

    Args:
        event_type: Stripe event type (e.g., 'checkout.session.completed')
        override: Dict of field overrides (e.g., {"customer": "cus_xxx"})
        timeout: Max seconds to wait

    Returns:
        Tuple of (success: bool, output: str)
    """
    cmd = ["stripe", "trigger", event_type]

    if override:
        for key, value in override.items():
            cmd.extend(["--override", f"{key}={value}"])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode == 0, result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return False, "Command timed out"
    except FileNotFoundError:
        return False, "Stripe CLI not found"


def wait_for_db_condition(
    session: Session,
    condition_fn,
    timeout: int = 10,
    interval: float = 0.5,
) -> bool:
    """
    Wait for a database condition to become true.

    Refreshes the session between checks to see committed changes.
    """
    start = time.time()
    while time.time() - start < timeout:
        session.expire_all()
        if condition_fn():
            return True
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


def create_stripe_customer(email: str, metadata: Optional[dict] = None) -> str:
    """Create a Stripe customer directly and track for cleanup."""
    import stripe

    stripe.api_key = STRIPE_SECRET_KEY

    customer = stripe.Customer.create(
        email=email,
        metadata=metadata or {},
    )
    _created_customers.append(customer.id)
    return customer.id


def create_test_user_with_stripe(session: Session, email: str) -> tuple[User, str]:
    """Create a test user in DB with a linked BillingAccount and Stripe customer."""
    user_id = str(uuid.uuid4())
    stripe_customer_id = create_stripe_customer(
        email=email,
        metadata={"user_id": user_id},
    )

    ba = BillingAccount(
        credits=Decimal("0"),
        stripe_customer_id=stripe_customer_id,
        account_status="ACTIVE",
    )
    session.add(ba)
    session.flush()

    user = User(
        id=user_id,
        email=email,
        billing_account_id=ba.id,
    )
    session.add(user)
    session.commit()

    return user, stripe_customer_id


def create_test_org_with_stripe(
    session: Session,
    name: str,
    owner_email: str,
) -> tuple[Organization, str]:
    """Create a test org with owner, linked BillingAccount, and Stripe customer."""
    from sqlalchemy import text

    from orchestra.db.models.orchestra_models import OrganizationMember

    # Create owner (with their own BillingAccount)
    owner_id = str(uuid.uuid4())
    owner_ba = BillingAccount(credits=Decimal("0"), account_status="ACTIVE")
    session.add(owner_ba)
    session.flush()

    owner = User(id=owner_id, email=owner_email, billing_account_id=owner_ba.id)
    session.add(owner)
    session.flush()

    # Create org with Stripe customer
    org_id = session.execute(text("SELECT nextval('organization_id_seq')")).scalar()

    stripe_customer_id = create_stripe_customer(
        email=owner_email,
        metadata={"organization_id": str(org_id)},
    )

    org_ba = BillingAccount(
        credits=Decimal("0"),
        stripe_customer_id=stripe_customer_id,
        account_status="ACTIVE",
    )
    session.add(org_ba)
    session.flush()

    org = Organization(
        id=org_id,
        name=name,
        owner_id=owner_id,
        billing_account_id=org_ba.id,
    )
    session.add(org)
    session.flush()

    # Get the Owner role ID
    owner_role_id = session.execute(
        text("SELECT id FROM role WHERE name = 'Owner' AND is_system_role = true"),
    ).scalar()

    member = OrganizationMember(
        organization_id=org_id,
        user_id=owner_id,
        role_id=owner_role_id,
    )
    session.add(member)
    session.commit()

    return org, stripe_customer_id
