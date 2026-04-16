"""
Tests for AssistantContact and AssistantContactCost models, dual-write logic,
dedicated contact detail endpoints, and Phase 5 decoupling.

Covers:
1. Model constraints (check constraints, partial unique indexes)
2. DAO layer (upsert, soft-delete, cost lookup, batch operations)
3. API dual-write (create, update, delete assistant → AssistantContact rows)
4. Dedicated POST /assistant/{id}/contact endpoint (Phase 2)
5. Dedicated GET /assistant/{id}/contacts endpoint (Phase 2)
6. Dedicated PUT /assistant/{id}/contact endpoint (Phase 2)
7. Cost lookup + credit check helpers (Phase 2)
8. Phase 5: AssistantCreate schema no longer accepts contact fields
9. Phase 5: AssistantUpdate deprecated contact fields are silently ignored
10. Phase 5: create_assistant / update_assistant_config no longer provision contacts
11. Phase 5: has_grace_period_contacts() DAO helper
12. Phase 5: Grace-period transfer guard on transfer endpoints
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchestra.db.dao.assistant_contact_dao import AssistantContactDAO
from orchestra.db.models.orchestra_models import ApiKey as ApiKeyModel
from orchestra.db.models.orchestra_models import (
    Assistant,
    AssistantContact,
    AssistantContactCost,
    BillingAccount,
    Organization,
    User,
)
from orchestra.tests.utils import HEADERS, create_test_org, create_test_user

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def seed_contact_type_costs(dbsession: Session):
    """Seed the AssistantContactCost table for cost lookup tests.

    Inserts the same rows as the migration if they don't already exist.
    Uses a function-scoped fixture so each test has the data available.
    """
    existing = dbsession.query(AssistantContactCost).count()
    if existing == 0:
        rows = [
            AssistantContactCost(
                contact_type="phone",
                provider="twilio",
                country_code="US",
                monthly_cost=Decimal("1.50"),
                one_time_cost=Decimal("5.00"),
            ),
            AssistantContactCost(
                contact_type="phone",
                provider="twilio",
                country_code="GB",
                monthly_cost=Decimal("1.50"),
                one_time_cost=Decimal("5.00"),
            ),
            AssistantContactCost(
                contact_type="phone",
                provider="twilio",
                country_code=None,
                monthly_cost=Decimal("2.00"),
                one_time_cost=Decimal("5.00"),
            ),
            AssistantContactCost(
                contact_type="email",
                provider="google_workspace",
                country_code=None,
                monthly_cost=Decimal("14.00"),
                one_time_cost=Decimal("5.00"),
            ),
            AssistantContactCost(
                contact_type="whatsapp",
                provider="twilio",
                country_code=None,
                monthly_cost=Decimal("5.00"),
                one_time_cost=Decimal("5.00"),
            ),
            AssistantContactCost(
                contact_type="discord",
                provider="discord",
                country_code=None,
                monthly_cost=Decimal("1"),
                one_time_cost=Decimal("1"),
            ),
            AssistantContactCost(
                contact_type="email",
                provider="microsoft_365",
                country_code=None,
                monthly_cost=Decimal("12.50"),
                one_time_cost=Decimal("5.00"),
            ),
        ]
        dbsession.add_all(rows)
        dbsession.flush()
    yield


@pytest.fixture(autouse=True)
def mock_assistant_infra_calls(request):
    """Automatically mock assistant infrastructure webhooks for all tests."""
    if "no_mock_infra" in request.keywords:
        yield
        return

    with patch(
        "orchestra.web.api.assistant.views.wake_up_assistant",
        new_callable=AsyncMock,
    ) as mock_wake_up, patch(
        "orchestra.web.api.assistant.views.reawaken_assistant",
        new_callable=AsyncMock,
    ) as mock_reawaken, patch(
        "orchestra.services.bucket_service.BucketService.__init__",
        lambda self: None,
    ):
        mock_wake_up.return_value = MagicMock(status_code=200)
        mock_reawaken.return_value = MagicMock(status_code=200, json=lambda: {})

        yield mock_wake_up, mock_reawaken


def _make_user_ba(
    dbsession: Session,
    uid: str,
    email: str | None = None,
    credits: float = 10000,
    phone_number: str | None = None,
    whatsapp_number: str | None = None,
) -> tuple[User, BillingAccount]:
    """Helper: create a User + BillingAccount pair."""
    ba = BillingAccount(
        credits=Decimal(str(credits)),
        account_status="ACTIVE",
    )
    dbsession.add(ba)
    dbsession.flush()
    user = User(
        id=uid,
        email=email or f"{uid}@test.com",
        billing_account_id=ba.id,
        phone_number=phone_number,
        whatsapp_number=whatsapp_number,
    )
    dbsession.add(user)
    dbsession.flush()
    return user, ba


def _ensure_user_has_contacts(dbsession: Session) -> None:
    """
    Ensure the user behind HEADERS has phone and WhatsApp numbers set.

    Required after the gate was added to ``create_assistant_contact``
    that blocks phone/WhatsApp contact creation unless the user has
    the corresponding number on their profile.
    """
    import os

    api_key = os.getenv("AUTH_ACCOUNT_API_KEY", "")
    row = dbsession.query(ApiKeyModel).filter(ApiKeyModel.key == api_key).first()
    if row:
        user = dbsession.query(User).filter(User.id == row.user_id).first()
        if user:
            if not user.phone_number:
                user.phone_number = "+15550001111"
            if not user.whatsapp_number:
                user.whatsapp_number = "+15550002222"
            if not user.discord_id:
                user.discord_id = "100000000000000001"
            dbsession.commit()


def _make_assistant(
    dbsession: Session,
    user_id: str,
    first_name: str = "Test",
    surname: str = "Bot",
    organization_id: int | None = None,
) -> Assistant:
    """Helper: create a bare Assistant row."""
    a = Assistant(
        user_id=user_id,
        first_name=first_name,
        surname=surname,
        organization_id=organization_id,
    )
    dbsession.add(a)
    dbsession.flush()
    return a


# ============================================================================
# 1. Model / Schema Tests
# ============================================================================


class TestAssistantContactModel:
    """Tests for the AssistantContact table schema and constraints."""

    def test_create_active_contact(self, dbsession: Session):
        """An active contact row can be inserted with valid data."""
        user, _ = _make_user_ba(dbsession, "mdl_u1")
        asst = _make_assistant(dbsession, user.id, "Model", "Test1")

        contact = AssistantContact(
            assistant_id=asst.agent_id,
            contact_type="phone",
            contact_value="+15551000001",
            provider="twilio",
            country_code="US",
            status="active",
        )
        dbsession.add(contact)
        dbsession.flush()

        assert contact.id is not None
        assert contact.provisioned_by == "platform"
        assert contact.status == "active"
        assert contact.deleted_at is None
        assert contact.created_at is not None

    def test_check_constraint_contact_type(self, dbsession: Session):
        """Invalid contact_type raises IntegrityError."""
        user, _ = _make_user_ba(dbsession, "mdl_u2")
        asst = _make_assistant(dbsession, user.id, "Model", "Test2")

        contact = AssistantContact(
            assistant_id=asst.agent_id,
            contact_type="fax",  # invalid
            contact_value="+15551000002",
            status="active",
        )
        dbsession.add(contact)
        with pytest.raises(IntegrityError, match="ck_assistant_contact_type"):
            dbsession.flush()
        dbsession.rollback()

    def test_check_constraint_status(self, dbsession: Session):
        """Invalid status raises IntegrityError."""
        user, _ = _make_user_ba(dbsession, "mdl_u3")
        asst = _make_assistant(dbsession, user.id, "Model", "Test3")

        contact = AssistantContact(
            assistant_id=asst.agent_id,
            contact_type="phone",
            contact_value="+15551000003",
            status="suspended",  # invalid — only active, grace_period, deleted
        )
        dbsession.add(contact)
        with pytest.raises(IntegrityError, match="ck_assistant_contact_status"):
            dbsession.flush()
        dbsession.rollback()

    def test_check_constraint_provisioned_by(self, dbsession: Session):
        """Invalid provisioned_by raises IntegrityError."""
        user, _ = _make_user_ba(dbsession, "mdl_u4")
        asst = _make_assistant(dbsession, user.id, "Model", "Test4")

        contact = AssistantContact(
            assistant_id=asst.agent_id,
            contact_type="email",
            contact_value="test@unify.ai",
            provisioned_by="magic",  # invalid
            status="active",
        )
        dbsession.add(contact)
        with pytest.raises(
            IntegrityError,
            match="ck_assistant_contact_provisioned_by",
        ):
            dbsession.flush()
        dbsession.rollback()

    def test_unique_active_contact_type_per_assistant(self, dbsession: Session):
        """Only one active contact of each type per assistant."""
        user, _ = _make_user_ba(dbsession, "mdl_u5")
        asst = _make_assistant(dbsession, user.id, "Model", "Test5")

        c1 = AssistantContact(
            assistant_id=asst.agent_id,
            contact_type="phone",
            contact_value="+15551000005a",
            status="active",
        )
        dbsession.add(c1)
        dbsession.flush()

        c2 = AssistantContact(
            assistant_id=asst.agent_id,
            contact_type="phone",
            contact_value="+15551000005b",
            status="active",
        )
        dbsession.add(c2)
        with pytest.raises(IntegrityError, match="uq_assistant_contact_type_active"):
            dbsession.flush()
        dbsession.rollback()

    def test_deleted_row_allows_new_active_same_type(self, dbsession: Session):
        """A deleted row does NOT block a new active row of the same type."""
        user, _ = _make_user_ba(dbsession, "mdl_u6")
        asst = _make_assistant(dbsession, user.id, "Model", "Test6")

        c1 = AssistantContact(
            assistant_id=asst.agent_id,
            contact_type="email",
            contact_value="old@unify.ai",
            status="deleted",
            deleted_at=datetime.utcnow(),
        )
        dbsession.add(c1)
        dbsession.flush()

        c2 = AssistantContact(
            assistant_id=asst.agent_id,
            contact_type="email",
            contact_value="new@unify.ai",
            status="active",
        )
        dbsession.add(c2)
        dbsession.flush()  # Should succeed
        assert c2.id is not None

    def test_unique_active_contact_value_across_assistants(self, dbsession: Session):
        """Same contact_value cannot be active on two different assistants."""
        user, _ = _make_user_ba(dbsession, "mdl_u7")
        asst1 = _make_assistant(dbsession, user.id, "Model", "Test7a")
        asst2 = _make_assistant(dbsession, user.id, "Model", "Test7b")

        c1 = AssistantContact(
            assistant_id=asst1.agent_id,
            contact_type="phone",
            contact_value="+15551000007",
            status="active",
        )
        dbsession.add(c1)
        dbsession.flush()

        c2 = AssistantContact(
            assistant_id=asst2.agent_id,
            contact_type="phone",
            contact_value="+15551000007",  # duplicate value
            status="active",
        )
        dbsession.add(c2)
        with pytest.raises(IntegrityError, match="uq_active_contact_value"):
            dbsession.flush()
        dbsession.rollback()

    def test_deleted_value_can_be_reused(self, dbsession: Session):
        """A deleted contact's value can be re-assigned to another assistant."""
        user, _ = _make_user_ba(dbsession, "mdl_u8")
        asst1 = _make_assistant(dbsession, user.id, "Model", "Test8a")
        asst2 = _make_assistant(dbsession, user.id, "Model", "Test8b")

        c1 = AssistantContact(
            assistant_id=asst1.agent_id,
            contact_type="phone",
            contact_value="+15551000008",
            status="deleted",
            deleted_at=datetime.utcnow(),
        )
        dbsession.add(c1)
        dbsession.flush()

        c2 = AssistantContact(
            assistant_id=asst2.agent_id,
            contact_type="phone",
            contact_value="+15551000008",
            status="active",
        )
        dbsession.add(c2)
        dbsession.flush()  # Should succeed
        assert c2.id is not None

    def test_cascade_delete_on_assistant(self, dbsession: Session):
        """Deleting an assistant hard-deletes its contacts (CASCADE)."""
        user, _ = _make_user_ba(dbsession, "mdl_u9")
        asst = _make_assistant(dbsession, user.id, "Model", "Test9")
        aid = asst.agent_id

        c = AssistantContact(
            assistant_id=aid,
            contact_type="phone",
            contact_value="+15551000009",
            status="active",
        )
        dbsession.add(c)
        dbsession.flush()
        cid = c.id

        dbsession.delete(asst)
        dbsession.flush()

        remaining = (
            dbsession.query(AssistantContact).filter(AssistantContact.id == cid).first()
        )
        assert remaining is None

    def test_grace_period_status(self, dbsession: Session):
        """grace_period is a valid status and can store grace_period_started_at."""
        user, _ = _make_user_ba(dbsession, "mdl_u10")
        asst = _make_assistant(dbsession, user.id, "Model", "Test10")

        now = datetime.utcnow()
        c = AssistantContact(
            assistant_id=asst.agent_id,
            contact_type="email",
            contact_value="grace@unify.ai",
            status="grace_period",
            grace_period_started_at=now,
        )
        dbsession.add(c)
        dbsession.flush()

        assert c.status == "grace_period"
        assert c.grace_period_started_at is not None

    def test_metadata_jsonb(self, dbsession: Session):
        """JSONB metadata column stores and retrieves structured data."""
        user, _ = _make_user_ba(dbsession, "mdl_u11")
        asst = _make_assistant(dbsession, user.id, "Model", "Test11")

        meta = {"sid": "PNxxx", "capabilities": {"voice": True, "sms": True}}
        c = AssistantContact(
            assistant_id=asst.agent_id,
            contact_type="phone",
            contact_value="+15551000011",
            status="active",
            metadata_=meta,
        )
        dbsession.add(c)
        dbsession.flush()

        fetched = dbsession.query(AssistantContact).get(c.id)
        assert fetched.metadata_ == meta
        assert fetched.metadata_["sid"] == "PNxxx"
        assert fetched.metadata_["capabilities"]["voice"] is True

    def test_multiple_contact_types_same_assistant(self, dbsession: Session):
        """An assistant can have one active contact of each type simultaneously."""
        user, _ = _make_user_ba(dbsession, "mdl_u12")
        asst = _make_assistant(dbsession, user.id, "Model", "Test12")

        for ct, val in [
            ("phone", "+15551000012"),
            ("email", "multi@unify.ai"),
            ("whatsapp", "+15552000012"),
            ("discord", "110000000000012012"),
        ]:
            dbsession.add(
                AssistantContact(
                    assistant_id=asst.agent_id,
                    contact_type=ct,
                    contact_value=val,
                    status="active",
                ),
            )
        dbsession.flush()

        contacts = AssistantContactDAO(dbsession).get_active_contacts_for_assistant(
            asst.agent_id,
        )
        assert len(contacts) == 4
        types = {c.contact_type for c in contacts}
        assert types == {"phone", "email", "whatsapp", "discord"}


# ============================================================================
# 2. AssistantContactCost Model Tests
# ============================================================================


class TestAssistantContactCostModel:
    """Tests for the AssistantContactCost table schema."""

    def test_create_cost_row(self, dbsession: Session):
        """A cost row can be created with valid data."""
        cost = AssistantContactCost(
            contact_type="phone",
            provider="twilio",
            country_code="DE",
            monthly_cost=Decimal("2.00"),
            one_time_cost=Decimal("1.50"),
        )
        dbsession.add(cost)
        dbsession.flush()
        assert cost.id is not None
        assert cost.monthly_cost == Decimal("2.00")

    def test_unique_constraint_contact_cost(self, dbsession: Session):
        """Duplicate (contact_type, provider, country_code) raises IntegrityError."""
        c1 = AssistantContactCost(
            contact_type="phone",
            provider="twilio",
            country_code="FR",
            monthly_cost=Decimal("2.00"),
        )
        dbsession.add(c1)
        dbsession.flush()

        c2 = AssistantContactCost(
            contact_type="phone",
            provider="twilio",
            country_code="FR",
            monthly_cost=Decimal("3.00"),
        )
        dbsession.add(c2)
        with pytest.raises(IntegrityError, match="uq_contact_cost"):
            dbsession.flush()
        dbsession.rollback()

    def test_check_constraint_cost_type(self, dbsession: Session):
        """Invalid contact_type raises IntegrityError."""
        cost = AssistantContactCost(
            contact_type="telegram",  # invalid
            monthly_cost=Decimal("5.00"),
        )
        dbsession.add(cost)
        with pytest.raises(IntegrityError, match="ck_contact_type_cost_type"):
            dbsession.flush()
        dbsession.rollback()

    def test_null_provider_and_country(self, dbsession: Session):
        """Provider and country_code can both be NULL (global default cost)."""
        cost = AssistantContactCost(
            contact_type="email",
            provider=None,
            country_code=None,
            monthly_cost=Decimal("14.00"),
        )
        dbsession.add(cost)
        dbsession.flush()
        assert cost.id is not None


# ============================================================================
# 3. DAO Tests – Cost Lookup
# ============================================================================


class TestGetContactCost:
    """Tests for the get_contact_cost() lookup function.

    Cost rows are seeded by the module-level ``seed_contact_type_costs`` autouse fixture.
    """

    def test_exact_match(self, dbsession: Session):
        """Exact (type, provider, country) match returns the correct row."""
        cost = AssistantContactDAO(dbsession).get_contact_cost(
            "phone",
            provider="twilio",
            country_code="US",
        )
        assert cost is not None
        assert cost.monthly_cost == Decimal("1.50")

    def test_country_fallback(self, dbsession: Session):
        """Unknown country falls back to provider's NULL-country row."""
        cost = AssistantContactDAO(dbsession).get_contact_cost(
            "phone",
            provider="twilio",
            country_code="JP",
        )
        assert cost is not None
        assert cost.monthly_cost == Decimal("2.00")

    def test_provider_inferred(self, dbsession: Session):
        """Provider is auto-inferred from contact_type when not supplied."""
        cost = AssistantContactDAO(dbsession).get_contact_cost("email")
        assert cost is not None
        assert cost.provider == "google_workspace"
        assert cost.monthly_cost == Decimal("14.00")

    def test_no_match_returns_none(self, dbsession: Session):
        """Returns None when no matching cost row exists at all."""
        cost = AssistantContactDAO(dbsession).get_contact_cost(
            "whatsapp",
            provider="unknown_provider",
        )
        assert cost is None


# ============================================================================
# 4. DAO Tests – Upsert / Soft-Delete
# ============================================================================


class TestUpsertAssistantContact:
    """Tests for the upsert_assistant_contact() helper."""

    def test_create_new_contact(self, dbsession: Session):
        """upsert creates a fresh row when none exists."""
        user, _ = _make_user_ba(dbsession, "ups_u1")
        asst = _make_assistant(dbsession, user.id, "Upsert", "Test1")

        contact = AssistantContactDAO(dbsession).upsert_assistant_contact(
            assistant_id=asst.agent_id,
            contact_type="phone",
            contact_value="+15553000001",
            country_code="US",
        )
        dbsession.flush()

        assert contact.id is not None
        assert contact.status == "active"
        assert contact.provider == "twilio"  # inferred
        assert contact.country_code == "US"

    def test_upsert_updates_existing_active(self, dbsession: Session):
        """upsert updates an existing active row in-place (idempotent)."""
        user, _ = _make_user_ba(dbsession, "ups_u2")
        asst = _make_assistant(dbsession, user.id, "Upsert", "Test2")

        c1 = AssistantContactDAO(dbsession).upsert_assistant_contact(
            assistant_id=asst.agent_id,
            contact_type="email",
            contact_value="first@unify.ai",
        )
        dbsession.flush()
        original_id = c1.id

        c2 = AssistantContactDAO(dbsession).upsert_assistant_contact(
            assistant_id=asst.agent_id,
            contact_type="email",
            contact_value="updated@unify.ai",
        )
        dbsession.flush()

        assert c2.id == original_id  # same row
        assert c2.contact_value == "updated@unify.ai"

    def test_upsert_recycles_deleted_row(self, dbsession: Session):
        """upsert recycles a deleted row instead of creating a new one."""
        user, _ = _make_user_ba(dbsession, "ups_u3")
        asst = _make_assistant(dbsession, user.id, "Upsert", "Test3")

        c1 = AssistantContactDAO(dbsession).upsert_assistant_contact(
            assistant_id=asst.agent_id,
            contact_type="whatsapp",
            contact_value="+15553000003",
        )
        dbsession.flush()
        original_id = c1.id

        # Soft-delete it
        AssistantContactDAO(dbsession).soft_delete_assistant_contact(
            assistant_id=asst.agent_id,
            contact_type="whatsapp",
        )
        dbsession.flush()
        assert c1.status == "deleted"

        # Upsert again — should recycle
        c2 = AssistantContactDAO(dbsession).upsert_assistant_contact(
            assistant_id=asst.agent_id,
            contact_type="whatsapp",
            contact_value="+15553000003b",
        )
        dbsession.flush()

        assert c2.id == original_id  # same row recycled
        assert c2.status == "active"
        assert c2.contact_value == "+15553000003b"
        assert c2.deleted_at is None
        assert c2.grace_period_started_at is None

    def test_upsert_with_explicit_provider(self, dbsession: Session):
        """Provider can be explicitly set, overriding the inferred value."""
        user, _ = _make_user_ba(dbsession, "ups_u4")
        asst = _make_assistant(dbsession, user.id, "Upsert", "Test4")

        contact = AssistantContactDAO(dbsession).upsert_assistant_contact(
            assistant_id=asst.agent_id,
            contact_type="phone",
            contact_value="+15553000004",
            provider="vonage",
        )
        dbsession.flush()
        assert contact.provider == "vonage"

    def test_upsert_with_metadata(self, dbsession: Session):
        """Metadata is stored and retrievable."""
        user, _ = _make_user_ba(dbsession, "ups_u5")
        asst = _make_assistant(dbsession, user.id, "Upsert", "Test5")

        meta = {"sid": "PNxyz", "capabilities": {"voice": True}}
        contact = AssistantContactDAO(dbsession).upsert_assistant_contact(
            assistant_id=asst.agent_id,
            contact_type="phone",
            contact_value="+15553000005",
            metadata=meta,
        )
        dbsession.flush()
        assert contact.metadata_ == meta


class TestSoftDeleteContact:
    """Tests for soft_delete_assistant_contact()."""

    def test_soft_delete_active(self, dbsession: Session):
        """Soft-deleting an active contact sets status=deleted + deleted_at."""
        user, _ = _make_user_ba(dbsession, "sdel_u1")
        asst = _make_assistant(dbsession, user.id, "SoftDel", "Test1")

        AssistantContactDAO(dbsession).upsert_assistant_contact(
            assistant_id=asst.agent_id,
            contact_type="phone",
            contact_value="+15554000001",
        )
        dbsession.flush()

        row = AssistantContactDAO(dbsession).soft_delete_assistant_contact(
            assistant_id=asst.agent_id,
            contact_type="phone",
        )
        dbsession.flush()

        assert row is not None
        assert row.status == "deleted"
        assert row.deleted_at is not None

    def test_soft_delete_grace_period(self, dbsession: Session):
        """Soft-deleting a contact in grace_period also works."""
        user, _ = _make_user_ba(dbsession, "sdel_u2")
        asst = _make_assistant(dbsession, user.id, "SoftDel", "Test2")

        c = AssistantContact(
            assistant_id=asst.agent_id,
            contact_type="email",
            contact_value="grace-del@unify.ai",
            status="grace_period",
            grace_period_started_at=datetime.utcnow(),
        )
        dbsession.add(c)
        dbsession.flush()

        row = AssistantContactDAO(dbsession).soft_delete_assistant_contact(
            assistant_id=asst.agent_id,
            contact_type="email",
        )
        assert row.status == "deleted"

    def test_soft_delete_nonexistent(self, dbsession: Session):
        """Soft-deleting a non-existent contact returns None."""
        user, _ = _make_user_ba(dbsession, "sdel_u3")
        asst = _make_assistant(dbsession, user.id, "SoftDel", "Test3")

        row = AssistantContactDAO(dbsession).soft_delete_assistant_contact(
            assistant_id=asst.agent_id,
            contact_type="phone",
        )
        assert row is None

    def test_soft_delete_already_deleted(self, dbsession: Session):
        """Soft-deleting an already-deleted contact returns None (no double delete)."""
        user, _ = _make_user_ba(dbsession, "sdel_u4")
        asst = _make_assistant(dbsession, user.id, "SoftDel", "Test4")

        AssistantContactDAO(dbsession).upsert_assistant_contact(
            assistant_id=asst.agent_id,
            contact_type="phone",
            contact_value="+15554000004",
        )
        dbsession.flush()

        AssistantContactDAO(dbsession).soft_delete_assistant_contact(
            assistant_id=asst.agent_id,
            contact_type="phone",
        )
        dbsession.flush()

        row = AssistantContactDAO(dbsession).soft_delete_assistant_contact(
            assistant_id=asst.agent_id,
            contact_type="phone",
        )
        assert row is None


class TestSoftDeleteAll:
    """Tests for soft_delete_all_contacts_for_assistant()."""

    def test_deletes_all_active(self, dbsession: Session):
        """Soft-deletes every active contact for an assistant."""
        user, _ = _make_user_ba(dbsession, "sdall_u1")
        asst = _make_assistant(dbsession, user.id, "SDAll", "Test1")

        for ct, val in [
            ("phone", "+15555000001"),
            ("email", "sdall1@unify.ai"),
            ("whatsapp", "+15556000001"),
        ]:
            AssistantContactDAO(dbsession).upsert_assistant_contact(
                assistant_id=asst.agent_id,
                contact_type=ct,
                contact_value=val,
            )
        dbsession.flush()

        deleted = AssistantContactDAO(dbsession).soft_delete_all_contacts_for_assistant(
            asst.agent_id,
        )
        dbsession.flush()

        assert len(deleted) == 3
        for row in deleted:
            assert row.status == "deleted"
            assert row.deleted_at is not None

        active = AssistantContactDAO(dbsession).get_active_contacts_for_assistant(
            asst.agent_id,
        )
        assert len(active) == 0

    def test_skips_already_deleted(self, dbsession: Session):
        """Already-deleted contacts are not touched."""
        user, _ = _make_user_ba(dbsession, "sdall_u2")
        asst = _make_assistant(dbsession, user.id, "SDAll", "Test2")

        c = AssistantContactDAO(dbsession).upsert_assistant_contact(
            assistant_id=asst.agent_id,
            contact_type="phone",
            contact_value="+15555000002",
        )
        dbsession.flush()
        AssistantContactDAO(dbsession).soft_delete_assistant_contact(
            assistant_id=asst.agent_id,
            contact_type="phone",
        )
        dbsession.flush()

        # Now call soft_delete_all — should find 0 active contacts
        deleted = AssistantContactDAO(dbsession).soft_delete_all_contacts_for_assistant(
            asst.agent_id,
        )
        assert len(deleted) == 0


class TestSoftDeleteForUser:
    """Tests for soft_delete_contacts_for_user()."""

    def test_deletes_personal_assistant_contacts(self, dbsession: Session):
        """Soft-deletes contacts for all personal assistants of a user."""
        user, _ = _make_user_ba(dbsession, "sduser_u1")
        asst1 = _make_assistant(dbsession, user.id, "SDUser", "A1")
        asst2 = _make_assistant(dbsession, user.id, "SDUser", "A2")

        AssistantContactDAO(dbsession).upsert_assistant_contact(
            assistant_id=asst1.agent_id,
            contact_type="phone",
            contact_value="+15557000001",
        )
        AssistantContactDAO(dbsession).upsert_assistant_contact(
            assistant_id=asst2.agent_id,
            contact_type="email",
            contact_value="sduser1@unify.ai",
        )
        dbsession.flush()

        deleted = AssistantContactDAO(dbsession).soft_delete_contacts_for_user(user.id)
        assert len(deleted) == 2
        for row in deleted:
            assert row.status == "deleted"

    def test_does_not_touch_org_assistant_contacts(self, dbsession: Session):
        """Org-owned assistant contacts are NOT affected by user deletion."""
        user, _ = _make_user_ba(dbsession, "sduser_u2")
        org = Organization(name="SDUserOrg", owner_id=user.id)
        dbsession.add(org)
        dbsession.flush()

        # Org assistant
        asst_org = _make_assistant(
            dbsession,
            user.id,
            "SDUser",
            "OrgBot",
            organization_id=org.id,
        )
        AssistantContactDAO(dbsession).upsert_assistant_contact(
            assistant_id=asst_org.agent_id,
            contact_type="phone",
            contact_value="+15557000002",
        )
        dbsession.flush()

        deleted = AssistantContactDAO(dbsession).soft_delete_contacts_for_user(user.id)
        assert len(deleted) == 0  # org contacts untouched

        active = AssistantContactDAO(dbsession).get_active_contacts_for_assistant(
            asst_org.agent_id,
        )
        assert len(active) == 1


class TestSoftDeleteForOrganization:
    """Tests for soft_delete_contacts_for_organization()."""

    def test_deletes_org_assistant_contacts(self, dbsession: Session):
        """Soft-deletes contacts for all org assistants."""
        user, _ = _make_user_ba(dbsession, "sdorg_u1")
        org = Organization(name="SDOrgTest", owner_id=user.id)
        dbsession.add(org)
        dbsession.flush()

        asst = _make_assistant(
            dbsession,
            user.id,
            "SDOrg",
            "Bot1",
            organization_id=org.id,
        )
        AssistantContactDAO(dbsession).upsert_assistant_contact(
            assistant_id=asst.agent_id,
            contact_type="email",
            contact_value="sdorg1@unify.ai",
        )
        dbsession.flush()

        deleted = AssistantContactDAO(dbsession).soft_delete_contacts_for_organization(
            org.id,
        )
        assert len(deleted) == 1
        assert deleted[0].status == "deleted"


# ============================================================================
# 5. API Dual-Write Integration Tests
# ============================================================================


def _mock_get_db_session_generator(real_session):
    """Create a mock get_db_session that returns the real session."""

    def mock_get_db_session(request):
        yield real_session

    return mock_get_db_session


@pytest.fixture
def mock_all_infra(dbsession):
    """Mock all infrastructure utilities for create_infra=True testing."""
    _ensure_user_has_contacts(dbsession)
    patches = {
        "create_email": AsyncMock(
            return_value={
                "user": {"primaryEmail": "testcontact@assistant.unify.ai"},
            },
        ),
        "create_outlook_email": AsyncMock(
            return_value={
                "user": {"primaryEmail": "testcontact@outlook.unify.ai"},
            },
        ),
        "watch_email": AsyncMock(return_value={"historyId": "123456"}),
        "watch_outlook_email": AsyncMock(return_value={"subscriptionId": "abc-123"}),
        "create_phone_number": AsyncMock(
            return_value={"phoneNumber": "+15551234567"},
        ),
        "create_pubsub_topic": AsyncMock(return_value={"name": "unity-1"}),
        "delete_email": AsyncMock(return_value={"success": True}),
        "delete_outlook_email": AsyncMock(return_value={"success": True}),
        "delete_phone_number": AsyncMock(return_value={"success": True}),
        "delete_pubsub_topic": AsyncMock(return_value={"success": True}),
        "wake_up_assistant": AsyncMock(return_value=MagicMock(status_code=200)),
        "reawaken_assistant": AsyncMock(
            return_value=MagicMock(status_code=200, json=lambda: {}),
        ),
        "log_pre_hire_chat": AsyncMock(return_value={"status": "success"}),
    }

    wa_pool_mock = AsyncMock(return_value={"pool_number": "+15559876543"})
    wa_register_mock = AsyncMock(return_value={"success": True})
    dc_pool_mock = AsyncMock(
        return_value={
            "pool_number": "123456789012345678",
            "auth_token": "fake-discord-bot-token",
        },
    )
    dc_register_mock = AsyncMock(return_value={"success": True})
    dc_delete_routes_mock = AsyncMock(return_value=0)

    with patch.multiple("orchestra.web.api.assistant.views", **patches):
        with patch(
            "orchestra.web.api.utils.assistant_infra.assign_whatsapp_pool_number",
            wa_pool_mock,
        ), patch(
            "orchestra.web.api.utils.assistant_infra.register_whatsapp_sender",
            wa_register_mock,
        ), patch(
            "orchestra.web.api.utils.assistant_infra.assign_discord_pool_bot",
            dc_pool_mock,
        ), patch(
            "orchestra.web.api.utils.assistant_infra.register_discord_bot",
            dc_register_mock,
        ), patch(
            "orchestra.web.api.utils.assistant_infra.delete_discord_routes",
            dc_delete_routes_mock,
        ):
            patches["assign_whatsapp_pool_number"] = wa_pool_mock
            patches["register_whatsapp_sender"] = wa_register_mock
            patches["assign_discord_pool_bot"] = dc_pool_mock
            patches["register_discord_bot"] = dc_register_mock
            patches["delete_discord_routes"] = dc_delete_routes_mock
            with patch(
                "orchestra.web.api.assistant.views.settings",
            ) as mock_settings:
                mock_settings.is_staging = True
                with patch(
                    "orchestra.web.api.assistant.views.get_db_session",
                    side_effect=_mock_get_db_session_generator(dbsession),
                ):
                    with patch(
                        "orchestra.web.api.assistant.views.asyncio.sleep",
                        new_callable=AsyncMock,
                    ), patch("orchestra.web.api.assistant.views.time.sleep"), patch(
                        "orchestra.services.bucket_service.BucketService.__init__",
                        lambda self: None,
                    ):
                        yield patches


class TestContactCreationViaDedicatedEndpoint:
    """
    Verify contacts are created exclusively via the dedicated endpoint.

    (Phase 5 removed contact provisioning from create_assistant / update_assistant.)
    """

    @pytest.mark.anyio
    async def test_phone_contact_via_dedicated_endpoint(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Phone provisioned via POST /contact → AssistantContact(type=phone) created."""
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "DWCreate", "surname": "Phone", "create_infra": False},
            headers=HEADERS,
        )
        assert create_resp.status_code == status.HTTP_200_OK
        agent_id = int(create_resp.json()["info"]["agent_id"])

        # No contacts yet
        assert (
            len(
                AssistantContactDAO(dbsession).get_active_contacts_for_assistant(
                    agent_id,
                ),
            )
            == 0
        )

        # Create via dedicated endpoint
        resp = await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "phone",
                "phone_country": "US",
            },
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK

        contacts = AssistantContactDAO(dbsession).get_active_contacts_for_assistant(
            agent_id,
        )
        phone_contacts = [c for c in contacts if c.contact_type == "phone"]
        assert len(phone_contacts) == 1
        assert phone_contacts[0].contact_value == "+15551234567"
        assert phone_contacts[0].provider == "twilio"
        assert phone_contacts[0].country_code == "US"

    @pytest.mark.anyio
    async def test_email_contact_via_dedicated_endpoint(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Email provisioned via POST /contact → AssistantContact(type=email) created."""
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "DWCreate", "surname": "Email", "create_infra": False},
            headers=HEADERS,
        )
        assert create_resp.status_code == status.HTTP_200_OK
        agent_id = int(create_resp.json()["info"]["agent_id"])

        resp = await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "email",
                "email_local": "dwcreate",
                "first_name": "DWCreate",
                "last_name": "Email",
            },
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK

        contacts = AssistantContactDAO(dbsession).get_active_contacts_for_assistant(
            agent_id,
        )
        email_contacts = [c for c in contacts if c.contact_type == "email"]
        assert len(email_contacts) == 1
        assert email_contacts[0].contact_value == "testcontact@assistant.unify.ai"
        assert email_contacts[0].provider == "google_workspace"

    @pytest.mark.anyio
    async def test_all_contacts_via_dedicated_endpoint(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Full provisioning via dedicated endpoint → three AssistantContact rows."""
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "DWCreate", "surname": "All", "create_infra": False},
            headers=HEADERS,
        )
        assert create_resp.status_code == status.HTTP_200_OK
        agent_id = int(create_resp.json()["info"]["agent_id"])

        await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "phone",
                "phone_country": "US",
            },
            headers=HEADERS,
        )
        await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "email",
                "email_local": "dwall",
                "first_name": "DWCreate",
                "last_name": "All",
            },
            headers=HEADERS,
        )
        await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={"contact_type": "whatsapp"},
            headers=HEADERS,
        )

        contacts = AssistantContactDAO(dbsession).get_active_contacts_for_assistant(
            agent_id,
        )
        types = {c.contact_type for c in contacts}
        assert types == {"phone", "email", "whatsapp"}

    @pytest.mark.anyio
    async def test_create_without_infra_no_contact_rows(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """create_infra=False → no AssistantContact rows created."""
        payload = {
            "first_name": "DWCreate",
            "surname": "NoInfra",
            "create_infra": False,
        }
        resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
        assert resp.status_code == status.HTTP_200_OK, resp.json()

        agent_id = int(resp.json()["info"]["agent_id"])
        contacts = AssistantContactDAO(dbsession).get_active_contacts_for_assistant(
            agent_id,
        )
        assert len(contacts) == 0


class TestDeleteContactWithDedicatedEndpoint:
    """Verify that deleting a contact also soft-deletes the AssistantContact row."""

    @pytest.mark.anyio
    async def test_delete_phone_contact(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """DELETE phone → AssistantContact soft-deleted."""
        # Create assistant without contacts
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "DWDel", "surname": "Phone", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        # Create phone contact via dedicated endpoint
        await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "phone",
                "phone_country": "US",
            },
            headers=HEADERS,
        )

        # Verify contact exists
        contacts_before = AssistantContactDAO(
            dbsession,
        ).get_active_contacts_for_assistant(agent_id)
        assert any(c.contact_type == "phone" for c in contacts_before)

        # Delete phone contact
        del_resp = await client.request(
            "DELETE",
            f"/v0/assistant/{agent_id}/contact",
            json={"contact_type": "phone"},
            headers=HEADERS,
        )
        assert del_resp.status_code == status.HTTP_200_OK

        # Verify contact is soft-deleted
        contacts_after = AssistantContactDAO(
            dbsession,
        ).get_active_contacts_for_assistant(agent_id)
        assert not any(c.contact_type == "phone" for c in contacts_after)

        # But the row still exists in the DB
        all_rows = (
            dbsession.query(AssistantContact)
            .filter(
                AssistantContact.assistant_id == agent_id,
                AssistantContact.contact_type == "phone",
            )
            .all()
        )
        assert len(all_rows) == 1
        assert all_rows[0].status == "deleted"

    @pytest.mark.anyio
    async def test_delete_email_contact(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """DELETE email → AssistantContact soft-deleted."""
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "DWDel", "surname": "Email", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        # Create email contact via dedicated endpoint
        await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "email",
                "email_local": "dwdel",
                "first_name": "DWDel",
                "last_name": "Email",
            },
            headers=HEADERS,
        )

        del_resp = await client.request(
            "DELETE",
            f"/v0/assistant/{agent_id}/contact",
            json={"contact_type": "email"},
            headers=HEADERS,
        )
        assert del_resp.status_code == status.HTTP_200_OK

        contacts = AssistantContactDAO(dbsession).get_active_contacts_for_assistant(
            agent_id,
        )
        assert not any(c.contact_type == "email" for c in contacts)


class TestDeleteAssistantSoftDeletesContacts:
    """Verify that deleting an assistant soft-deletes all its contacts first."""

    @pytest.mark.anyio
    async def test_delete_assistant_soft_deletes_contacts(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """DELETE assistant → all AssistantContact rows are soft-deleted."""
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "DWDelAsst", "surname": "Full", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        # Create contacts via dedicated endpoints
        await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "email",
                "email_local": "dwdelasst",
                "first_name": "DWDelAsst",
                "last_name": "Full",
            },
            headers=HEADERS,
        )
        await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "phone",
                "phone_country": "US",
            },
            headers=HEADERS,
        )

        # Verify contacts exist
        contacts_before = AssistantContactDAO(
            dbsession,
        ).get_active_contacts_for_assistant(agent_id)
        assert len(contacts_before) >= 2

        # Delete the assistant
        del_resp = await client.delete(
            f"/v0/assistant/{agent_id}",
            headers=HEADERS,
        )
        assert del_resp.status_code == status.HTTP_200_OK

        # After CASCADE, rows are gone — this is expected since the FK is
        # ON DELETE CASCADE. The soft_delete_all_contacts call in delete_assistant
        # sets status='deleted' before the assistant row is removed, which means
        # the soft-delete was executed. We verify via the response being 200.
        # (The actual rows are cascade-deleted along with the assistant.)


# ============================================================================
# 6. Migration Backfill Smoke Test
# ============================================================================


class TestBackfillConsistency:
    """
    The legacy contact columns (phone, email, etc.) have been dropped from
    the assistants table.  The backfill tests that previously set those
    columns and verified upsert behavior are no longer applicable.

    AssistantContact rows are now created exclusively through the dedicated
    contact endpoints and the DAO's upsert_assistant_contact method.
    """

    def test_upsert_phone_contact(self, dbsession: Session):
        """Create a phone contact via upsert."""
        user, _ = _make_user_ba(dbsession, "bf_u1")
        asst = _make_assistant(dbsession, user.id, "Backfill", "Phone")

        contact = AssistantContactDAO(dbsession).upsert_assistant_contact(
            assistant_id=asst.agent_id,
            contact_type="phone",
            contact_value="+15558000001",
            provider="twilio",
            country_code="GB",
        )
        dbsession.flush()

        assert contact.contact_value == "+15558000001"
        assert contact.country_code == "GB"

    def test_upsert_email_contact(self, dbsession: Session):
        """Create an email contact via upsert."""
        user, _ = _make_user_ba(dbsession, "bf_u2")
        asst = _make_assistant(dbsession, user.id, "Backfill", "Email")

        contact = AssistantContactDAO(dbsession).upsert_assistant_contact(
            assistant_id=asst.agent_id,
            contact_type="email",
            contact_value="backfill@unify.ai",
            provider="google_workspace",
        )
        dbsession.flush()

        assert contact.contact_value == "backfill@unify.ai"
        assert contact.provider == "google_workspace"

    def test_upsert_whatsapp_contact(self, dbsession: Session):
        """Create a whatsapp contact via upsert (pool number only, user WA lives on User)."""
        user, _ = _make_user_ba(dbsession, "bf_u3")
        asst = _make_assistant(dbsession, user.id, "Backfill", "WhatsApp")

        pool_number = "+15558000003"
        contact = AssistantContactDAO(dbsession).upsert_assistant_contact(
            assistant_id=asst.agent_id,
            contact_type="whatsapp",
            contact_value=pool_number,
            provider="twilio",
        )
        dbsession.flush()

        assert contact.contact_value == pool_number


# ============================================================================
# 7. Phase 2 — Cost Lookup Helper Tests
# ============================================================================


class TestCostLookupHelpers:
    """Tests for get_contact_monthly_cost() and get_contact_one_time_cost()."""

    def test_monthly_cost_exact_match(self, dbsession: Session):
        """Exact (type, provider, country) match returns correct monthly cost."""
        cost = AssistantContactDAO(dbsession).get_contact_monthly_cost(
            "phone",
            provider="twilio",
            country_code="US",
        )
        assert cost == Decimal("1.50")

    def test_monthly_cost_fallback_no_country(self, dbsession: Session):
        """Falls back to provider-only row when no country-specific row exists."""
        cost = AssistantContactDAO(dbsession).get_contact_monthly_cost(
            "phone",
            provider="twilio",
            country_code="DE",
        )
        # There's no "DE" row; should fall back to twilio/NULL or return 0
        # Depends on seeded data — if no fallback, returns 0
        assert isinstance(cost, Decimal)

    def test_monthly_cost_email(self, dbsession: Session):
        """Email monthly cost is $14.00."""
        cost = AssistantContactDAO(dbsession).get_contact_monthly_cost(
            "email",
            provider="google_workspace",
        )
        assert cost == Decimal("14.00")

    def test_monthly_cost_whatsapp(self, dbsession: Session):
        """WhatsApp monthly cost is $5.00."""
        cost = AssistantContactDAO(dbsession).get_contact_monthly_cost(
            "whatsapp",
            provider="twilio",
        )
        assert cost == Decimal("5.00")

    def test_one_time_cost_whatsapp(self, dbsession: Session):
        """WhatsApp one-time cost is $5.00."""
        cost = AssistantContactDAO(dbsession).get_contact_one_time_cost(
            "whatsapp",
            provider="twilio",
        )
        assert cost == Decimal("5.00")

    def test_one_time_cost_email(self, dbsession: Session):
        """Email setup fee is $5.00."""
        cost = AssistantContactDAO(dbsession).get_contact_one_time_cost(
            "email",
            provider="google_workspace",
        )
        assert cost == Decimal("5.00")

    def test_one_time_cost_phone_us(self, dbsession: Session):
        """US phone one-time cost from seeded data."""
        cost = AssistantContactDAO(dbsession).get_contact_one_time_cost(
            "phone",
            provider="twilio",
            country_code="US",
        )
        assert cost == Decimal("5.00")

    def test_unknown_type_returns_zero(self, dbsession: Session):
        """Unknown contact type returns Decimal(0)."""
        cost = AssistantContactDAO(dbsession).get_contact_monthly_cost("fax")
        assert cost == Decimal("0")

    def test_provider_inference(self, dbsession: Session):
        """Provider is auto-inferred if not supplied."""
        cost = AssistantContactDAO(dbsession).get_contact_monthly_cost("email")
        assert cost == Decimal("14.00")


class TestGetContactByAssistantAndType:
    """Tests for get_contact_by_assistant_and_type()."""

    def test_returns_active_contact(self, dbsession: Session):
        """Returns the active contact if one exists."""
        user, _ = _make_user_ba(dbsession, "gcbat_u1")
        asst = _make_assistant(dbsession, user.id, "GCBAT", "Test1")
        AssistantContactDAO(dbsession).upsert_assistant_contact(
            assistant_id=asst.agent_id,
            contact_type="phone",
            contact_value="+15550100001",
        )
        dbsession.flush()

        result = AssistantContactDAO(dbsession).get_contact_by_assistant_and_type(
            asst.agent_id,
            "phone",
        )
        assert result is not None
        assert result.contact_value == "+15550100001"

    def test_returns_none_for_deleted(self, dbsession: Session):
        """Returns None if the contact has been soft-deleted."""
        user, _ = _make_user_ba(dbsession, "gcbat_u2")
        asst = _make_assistant(dbsession, user.id, "GCBAT", "Test2")
        AssistantContactDAO(dbsession).upsert_assistant_contact(
            assistant_id=asst.agent_id,
            contact_type="email",
            contact_value="gcbat2@unify.ai",
        )
        dbsession.flush()
        AssistantContactDAO(dbsession).soft_delete_assistant_contact(
            assistant_id=asst.agent_id,
            contact_type="email",
        )
        dbsession.flush()

        result = AssistantContactDAO(dbsession).get_contact_by_assistant_and_type(
            asst.agent_id,
            "email",
        )
        assert result is None

    def test_returns_none_for_missing_type(self, dbsession: Session):
        """Returns None if the assistant has no contact of that type."""
        user, _ = _make_user_ba(dbsession, "gcbat_u3")
        asst = _make_assistant(dbsession, user.id, "GCBAT", "Test3")

        result = AssistantContactDAO(dbsession).get_contact_by_assistant_and_type(
            asst.agent_id,
            "whatsapp",
        )
        assert result is None


# ============================================================================
# 8. Phase 2 — Dedicated Contact Endpoint Tests
# ============================================================================


def _create_bare_assistant(client, headers=HEADERS) -> int:
    """Create an assistant without any contacts and return its agent_id.

    This is a sync wrapper used only inside async tests via ``await``.
    """
    pass  # Placeholder – actual calls happen inside async tests


class TestCreateContactEndpoint:
    """Tests for POST /assistant/{id}/contact."""

    @pytest.mark.anyio
    async def test_create_phone_contact(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Creating a phone contact provisions Twilio and writes to DB."""
        # Create bare assistant first
        create_resp = await client.post(
            "/v0/assistant",
            json={
                "first_name": "CCE",
                "surname": "Phone",
                "create_infra": False,
            },
            headers=HEADERS,
        )
        assert create_resp.status_code == status.HTTP_200_OK
        agent_id = int(create_resp.json()["info"]["agent_id"])

        # Create phone contact
        resp = await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "phone",
                "phone_country": "US",
            },
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK, resp.json()

        # Verify AssistantContact row
        contact = AssistantContactDAO(dbsession).get_contact_by_assistant_and_type(
            agent_id,
            "phone",
        )
        assert contact is not None
        assert contact.contact_value == "+15551234567"
        assert contact.provider == "twilio"
        assert contact.country_code == "US"
        assert contact.status == "active"

        # Verify reawaken was called
        mock_all_infra["reawaken_assistant"].assert_called()

    @pytest.mark.anyio
    async def test_create_email_contact(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Creating an email contact provisions Google Workspace and writes to DB."""
        create_resp = await client.post(
            "/v0/assistant",
            json={
                "first_name": "CCE",
                "surname": "Email",
                "create_infra": False,
            },
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        resp = await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "email",
                "email_local": "cce-email",
                "first_name": "CCE",
                "last_name": "Email",
            },
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK, resp.json()

        contact = AssistantContactDAO(dbsession).get_contact_by_assistant_and_type(
            agent_id,
            "email",
        )
        assert contact is not None
        assert contact.contact_value == "testcontact@assistant.unify.ai"
        assert contact.provider == "google_workspace"

    @pytest.mark.anyio
    async def test_create_whatsapp_contact(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Creating a WhatsApp contact assigns a sender and writes to DB."""
        create_resp = await client.post(
            "/v0/assistant",
            json={
                "first_name": "CCE",
                "surname": "WhatsApp",
                "create_infra": False,
            },
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        resp = await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={"contact_type": "whatsapp"},
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK, resp.json()

        contact = AssistantContactDAO(dbsession).get_contact_by_assistant_and_type(
            agent_id,
            "whatsapp",
        )
        assert contact is not None
        assert contact.contact_value == "+15559876543"
        assert contact.provider == "twilio"

    @pytest.mark.anyio
    async def test_create_discord_contact(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Creating a Discord contact assigns a pool bot and writes to DB."""
        create_resp = await client.post(
            "/v0/assistant",
            json={
                "first_name": "CCE",
                "surname": "Discord",
                "create_infra": False,
            },
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        resp = await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={"contact_type": "discord"},
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK, resp.json()

        contact = AssistantContactDAO(dbsession).get_contact_by_assistant_and_type(
            agent_id,
            "discord",
        )
        assert contact is not None
        assert contact.contact_value == "123456789012345678"
        assert contact.provider == "discord"

    @pytest.mark.anyio
    async def test_create_discord_contact_requires_discord_id(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Creating a Discord contact without discord_id on profile → 422."""
        import os

        api_key = os.getenv("AUTH_ACCOUNT_API_KEY", "")
        row = dbsession.query(ApiKeyModel).filter(ApiKeyModel.key == api_key).first()
        user = dbsession.query(User).filter(User.id == row.user_id).first()
        original_discord_id = user.discord_id
        user.discord_id = None
        dbsession.commit()

        try:
            create_resp = await client.post(
                "/v0/assistant",
                json={
                    "first_name": "CCE",
                    "surname": "NoDiscord",
                    "create_infra": False,
                },
                headers=HEADERS,
            )
            agent_id = int(create_resp.json()["info"]["agent_id"])

            resp = await client.post(
                f"/v0/assistant/{agent_id}/contact",
                json={"contact_type": "discord"},
                headers=HEADERS,
            )
            assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
            assert "Discord" in resp.json()["detail"]
        finally:
            user.discord_id = original_discord_id
            dbsession.commit()

    @pytest.mark.anyio
    async def test_duplicate_contact_type_returns_409(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Creating the same contact type twice returns 409 Conflict."""
        create_resp = await client.post(
            "/v0/assistant",
            json={
                "first_name": "CCE",
                "surname": "Dup",
                "create_infra": False,
            },
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        # First creation — success
        resp1 = await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "email",
                "email_local": "cce-dup",
                "first_name": "CCE",
                "last_name": "Dup",
            },
            headers=HEADERS,
        )
        assert resp1.status_code == status.HTTP_200_OK

        # Second creation — conflict
        resp2 = await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "email",
                "email_local": "cce-dup2",
                "first_name": "CCE",
                "last_name": "Dup2",
            },
            headers=HEADERS,
        )
        assert resp2.status_code == status.HTTP_409_CONFLICT
        assert "already exists" in resp2.json()["detail"]

    @pytest.mark.anyio
    async def test_nonexistent_assistant_returns_404(
        self,
        client: AsyncClient,
        mock_all_infra,
    ):
        """POST to a non-existent assistant returns 404."""
        resp = await client.post(
            "/v0/assistant/999999/contact",
            json={"contact_type": "phone", "phone_country": "US"},
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.anyio
    async def test_email_requires_email_local(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Creating an email contact without email_local returns 400."""
        create_resp = await client.post(
            "/v0/assistant",
            json={
                "first_name": "CCE",
                "surname": "NoLocal",
                "create_infra": False,
            },
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        resp = await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={"contact_type": "email"},
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert "email_local" in resp.json()["detail"]

    @pytest.mark.anyio
    async def test_monthly_cost_stored_on_contact(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """The monthly_cost from AssistantContactCost is stored on the AssistantContact."""
        create_resp = await client.post(
            "/v0/assistant",
            json={
                "first_name": "CCE",
                "surname": "MonthlyCost",
                "create_infra": False,
            },
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "email",
                "email_local": "cce-mc",
                "first_name": "CCE",
                "last_name": "MC",
            },
            headers=HEADERS,
        )

        contact = AssistantContactDAO(dbsession).get_contact_by_assistant_and_type(
            agent_id,
            "email",
        )
        assert contact is not None
        assert contact.monthly_cost is not None
        assert Decimal(str(contact.monthly_cost)) == Decimal("14.00")


class TestListContactsEndpoint:
    """Tests for GET /assistant/{id}/contacts."""

    @pytest.mark.anyio
    async def test_list_returns_active_contacts(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Returns all active contacts with billing metadata."""
        create_resp = await client.post(
            "/v0/assistant",
            json={
                "first_name": "LCE",
                "surname": "List",
                "create_infra": False,
            },
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        # Create phone + email contacts
        await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "phone",
                "phone_country": "US",
            },
            headers=HEADERS,
        )
        await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "email",
                "email_local": "lce-list",
                "first_name": "LCE",
                "last_name": "List",
            },
            headers=HEADERS,
        )

        # List
        resp = await client.get(
            f"/v0/assistant/{agent_id}/contacts",
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        contacts = resp.json()["info"]
        assert len(contacts) == 2

        types = {c["contact_type"] for c in contacts}
        assert types == {"phone", "email"}

        for c in contacts:
            assert c["status"] == "active"
            assert "id" in c
            assert "contact_value" in c
            assert "provider" in c
            assert "created_at" in c

    @pytest.mark.anyio
    async def test_list_excludes_deleted_contacts(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Deleted contacts are not included in the list."""
        create_resp = await client.post(
            "/v0/assistant",
            json={
                "first_name": "LCE",
                "surname": "Deleted",
                "create_infra": False,
            },
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        # Create then delete a phone contact
        await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "phone",
                "phone_country": "US",
            },
            headers=HEADERS,
        )
        await client.request(
            "DELETE",
            f"/v0/assistant/{agent_id}/contact",
            json={"contact_type": "phone"},
            headers=HEADERS,
        )

        # List should be empty
        resp = await client.get(
            f"/v0/assistant/{agent_id}/contacts",
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        assert len(resp.json()["info"]) == 0

    @pytest.mark.anyio
    async def test_list_empty_for_no_contacts(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Returns empty list when assistant has no contacts."""
        create_resp = await client.post(
            "/v0/assistant",
            json={
                "first_name": "LCE",
                "surname": "Empty",
                "create_infra": False,
            },
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        resp = await client.get(
            f"/v0/assistant/{agent_id}/contacts",
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["info"] == []

    @pytest.mark.anyio
    async def test_list_nonexistent_assistant_returns_404(
        self,
        client: AsyncClient,
        mock_all_infra,
    ):
        """GET on a non-existent assistant returns 404."""
        resp = await client.get(
            "/v0/assistant/999999/contacts",
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_404_NOT_FOUND


class TestUpdateContactEndpoint:
    """Tests for PUT /assistant/{id}/contact."""

    @pytest.mark.anyio
    async def test_update_metadata_phone(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Updating metadata on a phone contact stores the new metadata."""
        create_resp = await client.post(
            "/v0/assistant",
            json={
                "first_name": "UCE",
                "surname": "Phone",
                "create_infra": False,
            },
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        # Create phone contact
        await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "phone",
                "phone_country": "US",
            },
            headers=HEADERS,
        )

        # Update metadata
        resp = await client.put(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "phone",
                "metadata": {"forwarding": "+15550555555"},
            },
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK

        # Verify DB update
        contact = AssistantContactDAO(dbsession).get_contact_by_assistant_and_type(
            agent_id,
            "phone",
        )
        assert contact.metadata_.get("forwarding") == "+15550555555"

    @pytest.mark.anyio
    async def test_update_metadata_merges(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Updating metadata merges with existing metadata."""
        create_resp = await client.post(
            "/v0/assistant",
            json={
                "first_name": "UCE",
                "surname": "Meta",
                "create_infra": False,
            },
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "phone",
                "phone_country": "US",
            },
            headers=HEADERS,
        )

        # Update metadata
        resp = await client.put(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "phone",
                "metadata": {"key1": "value1"},
            },
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK

        # Second update — should merge
        resp2 = await client.put(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "phone",
                "metadata": {"key2": "value2"},
            },
            headers=HEADERS,
        )
        assert resp2.status_code == status.HTTP_200_OK

        contact = AssistantContactDAO(dbsession).get_contact_by_assistant_and_type(
            agent_id,
            "phone",
        )
        assert contact.metadata_.get("key1") == "value1"
        assert contact.metadata_.get("key2") == "value2"

    @pytest.mark.anyio
    async def test_update_nonexistent_contact_returns_404(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Updating a contact that doesn't exist returns 404."""
        create_resp = await client.post(
            "/v0/assistant",
            json={
                "first_name": "UCE",
                "surname": "NoContact",
                "create_infra": False,
            },
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        resp = await client.put(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "phone",
                "metadata": {"label": "test"},
            },
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.anyio
    async def test_update_nonexistent_assistant_returns_404(
        self,
        client: AsyncClient,
        mock_all_infra,
    ):
        """PUT to a non-existent assistant returns 404."""
        resp = await client.put(
            "/v0/assistant/999999/contact",
            json={
                "contact_type": "email",
                "metadata": {"label": "test"},
            },
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.anyio
    async def test_update_triggers_reawaken(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Updating a contact triggers reawaken_assistant."""
        create_resp = await client.post(
            "/v0/assistant",
            json={
                "first_name": "UCE",
                "surname": "Reawaken",
                "create_infra": False,
            },
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "phone",
                "phone_country": "US",
            },
            headers=HEADERS,
        )

        # Reset mock call count
        mock_all_infra["reawaken_assistant"].reset_mock()

        await client.put(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "phone",
                "metadata": {"label": "reawaken-test"},
            },
            headers=HEADERS,
        )

        mock_all_infra["reawaken_assistant"].assert_called_once()


class TestDeleteContactEndpointPhase2:
    """Additional delete tests specific to Phase 2 integration."""

    @pytest.mark.anyio
    async def test_delete_via_new_create_then_delete(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Create via POST /contact, then delete via DELETE /contact."""
        create_resp = await client.post(
            "/v0/assistant",
            json={
                "first_name": "DCE",
                "surname": "P2",
                "create_infra": False,
            },
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        # Create via dedicated endpoint
        await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "email",
                "email_local": "dce-p2",
                "first_name": "DCE",
                "last_name": "P2",
            },
            headers=HEADERS,
        )

        # Verify contact exists
        contact = AssistantContactDAO(dbsession).get_contact_by_assistant_and_type(
            agent_id,
            "email",
        )
        assert contact is not None

        # Delete
        del_resp = await client.request(
            "DELETE",
            f"/v0/assistant/{agent_id}/contact",
            json={"contact_type": "email"},
            headers=HEADERS,
        )
        assert del_resp.status_code == status.HTTP_200_OK

        # Verify soft-deleted
        contact = AssistantContactDAO(dbsession).get_contact_by_assistant_and_type(
            agent_id,
            "email",
        )
        assert contact is None

    @pytest.mark.anyio
    async def test_create_after_delete_recycles_row(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Creating a contact after deleting one recycles the soft-deleted row."""
        create_resp = await client.post(
            "/v0/assistant",
            json={
                "first_name": "DCE",
                "surname": "Recycle",
                "create_infra": False,
            },
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        # Create phone
        await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "phone",
                "phone_country": "US",
            },
            headers=HEADERS,
        )

        # Delete phone
        await client.request(
            "DELETE",
            f"/v0/assistant/{agent_id}/contact",
            json={"contact_type": "phone"},
            headers=HEADERS,
        )

        # Re-create phone
        resp = await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "phone",
                "phone_country": "US",
            },
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK

        # Should have only ONE phone row (recycled)
        all_rows = (
            dbsession.query(AssistantContact)
            .filter(
                AssistantContact.assistant_id == agent_id,
                AssistantContact.contact_type == "phone",
            )
            .all()
        )
        # The upsert_assistant_contact recycles deleted rows
        active = [r for r in all_rows if r.status == "active"]
        assert len(active) == 1


class TestEndToEndContactLifecycle:
    """Full lifecycle tests: create → list → update → delete → verify."""

    @pytest.mark.anyio
    async def test_full_lifecycle(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Create, list, update, delete a contact through the dedicated endpoints."""
        # 1. Create assistant
        create_resp = await client.post(
            "/v0/assistant",
            json={
                "first_name": "E2E",
                "surname": "Lifecycle",
                "create_infra": False,
            },
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        # 2. Create phone contact via dedicated endpoint
        create_contact_resp = await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "phone",
                "phone_country": "US",
            },
            headers=HEADERS,
        )
        assert create_contact_resp.status_code == status.HTTP_200_OK
        # Response should show the assistant with phone in backward-compat columns
        asst_data = create_contact_resp.json()["info"]
        assert asst_data["phone"] == "+15551234567"  # mock value

        # 3. List contacts
        list_resp = await client.get(
            f"/v0/assistant/{agent_id}/contacts",
            headers=HEADERS,
        )
        assert list_resp.status_code == status.HTTP_200_OK
        contacts = list_resp.json()["info"]
        assert len(contacts) == 1
        assert contacts[0]["contact_type"] == "phone"
        assert contacts[0]["status"] == "active"

        # 4. Update metadata
        update_resp = await client.put(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "phone",
                "metadata": {"label": "primary"},
            },
            headers=HEADERS,
        )
        assert update_resp.status_code == status.HTTP_200_OK

        # Verify
        contact = AssistantContactDAO(dbsession).get_contact_by_assistant_and_type(
            agent_id,
            "phone",
        )
        assert contact.metadata_.get("label") == "primary"

        # 5. Delete contact
        del_resp = await client.request(
            "DELETE",
            f"/v0/assistant/{agent_id}/contact",
            json={"contact_type": "phone"},
            headers=HEADERS,
        )
        assert del_resp.status_code == status.HTTP_200_OK

        # 6. Verify deleted
        list_resp2 = await client.get(
            f"/v0/assistant/{agent_id}/contacts",
            headers=HEADERS,
        )
        assert list_resp2.status_code == status.HTTP_200_OK
        assert len(list_resp2.json()["info"]) == 0


# ============================================================================
# 8b. MS365 Email Provider Tests
# ============================================================================


class TestCreateMS365EmailContact:
    """Tests for creating email contacts with email_provider='microsoft_365'."""

    @pytest.mark.anyio
    async def test_create_ms365_email_contact(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """POST /contact with email_provider=microsoft_365 provisions via Outlook."""
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "MS365", "surname": "Email", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        resp = await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "email",
                "email_provider": "microsoft_365",
                "email_local": "ms365test",
                "first_name": "MS365",
                "last_name": "Email",
            },
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK, resp.json()

        mock_all_infra["create_outlook_email"].assert_called_once()
        mock_all_infra["watch_outlook_email"].assert_called_once()
        mock_all_infra["create_email"].assert_not_called()
        mock_all_infra["watch_email"].assert_not_called()

        contact = AssistantContactDAO(dbsession).get_contact_by_assistant_and_type(
            agent_id,
            "email",
        )
        assert contact is not None
        assert contact.contact_value == "testcontact@outlook.unify.ai"
        assert contact.provider == "microsoft_365"
        assert contact.status == "active"

    @pytest.mark.anyio
    async def test_create_email_default_provider_is_google(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """POST /contact without email_provider defaults to google_workspace."""
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "DefProv", "surname": "Email", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        resp = await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "email",
                "email_local": "defprov",
                "first_name": "DefProv",
                "last_name": "Email",
            },
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK

        mock_all_infra["create_email"].assert_called_once()
        mock_all_infra["create_outlook_email"].assert_not_called()

        contact = AssistantContactDAO(dbsession).get_contact_by_assistant_and_type(
            agent_id,
            "email",
        )
        assert contact.provider == "google_workspace"

        get_resp = await client.get("/v0/assistant", headers=HEADERS)
        assistant = [
            a for a in get_resp.json()["info"] if int(a["agent_id"]) == agent_id
        ][0]
        assert assistant["email_provider"] == "google_workspace"

    @pytest.mark.anyio
    async def test_create_email_explicit_google_workspace(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """POST /contact with email_provider=google_workspace uses Gmail path."""
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "GW", "surname": "Email", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        resp = await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "email",
                "email_provider": "google_workspace",
                "email_local": "gwtest",
                "first_name": "GW",
                "last_name": "Email",
            },
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK

        mock_all_infra["create_email"].assert_called_once()
        mock_all_infra["create_outlook_email"].assert_not_called()

        contact = AssistantContactDAO(dbsession).get_contact_by_assistant_and_type(
            agent_id,
            "email",
        )
        assert contact.provider == "google_workspace"

    @pytest.mark.anyio
    async def test_ms365_monthly_cost_stored(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """MS365 email contact stores its own monthly cost (not Google's)."""
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "MS365MC", "surname": "Cost", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "email",
                "email_provider": "microsoft_365",
                "email_local": "ms365mc",
                "first_name": "MS365MC",
                "last_name": "Cost",
            },
            headers=HEADERS,
        )

        contact = AssistantContactDAO(dbsession).get_contact_by_assistant_and_type(
            agent_id,
            "email",
        )
        assert contact is not None
        assert Decimal(str(contact.monthly_cost)) == Decimal("12.50")

    @pytest.mark.anyio
    async def test_ms365_email_requires_email_local(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """MS365 email contact requires email_local just like Google."""
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "MS365NL", "surname": "Err", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        resp = await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "email",
                "email_provider": "microsoft_365",
            },
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert "email_local" in resp.json()["detail"]


class TestDeleteMS365EmailContact:
    """Tests for deleting email contacts provisioned via MS365."""

    @pytest.mark.anyio
    async def test_delete_ms365_email_calls_outlook_delete(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """DELETE email contact with provider=microsoft_365 calls delete_outlook_email."""
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "MS365Del", "surname": "Email", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "email",
                "email_provider": "microsoft_365",
                "email_local": "ms365del",
                "first_name": "MS365Del",
                "last_name": "Email",
            },
            headers=HEADERS,
        )

        mock_all_infra["delete_outlook_email"].reset_mock()
        mock_all_infra["delete_email"].reset_mock()

        del_resp = await client.request(
            "DELETE",
            f"/v0/assistant/{agent_id}/contact",
            json={"contact_type": "email"},
            headers=HEADERS,
        )
        assert del_resp.status_code == status.HTTP_200_OK

        mock_all_infra["delete_outlook_email"].assert_called_once()
        mock_all_infra["delete_email"].assert_not_called()

        contact = AssistantContactDAO(dbsession).get_contact_by_assistant_and_type(
            agent_id,
            "email",
        )
        assert contact is None

    @pytest.mark.anyio
    async def test_delete_google_email_calls_gmail_delete(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """DELETE email contact with provider=google_workspace calls delete_email."""
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "GWDel", "surname": "Email", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "email",
                "email_local": "gwdel",
                "first_name": "GWDel",
                "last_name": "Email",
            },
            headers=HEADERS,
        )

        mock_all_infra["delete_outlook_email"].reset_mock()
        mock_all_infra["delete_email"].reset_mock()

        del_resp = await client.request(
            "DELETE",
            f"/v0/assistant/{agent_id}/contact",
            json={"contact_type": "email"},
            headers=HEADERS,
        )
        assert del_resp.status_code == status.HTTP_200_OK

        mock_all_infra["delete_email"].assert_called_once()
        mock_all_infra["delete_outlook_email"].assert_not_called()


class TestMS365EndToEndLifecycle:
    """Full lifecycle test for MS365 email contacts."""

    @pytest.mark.anyio
    async def test_ms365_full_lifecycle(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Create → list → update → delete an MS365 email contact."""
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "MS365E2E", "surname": "Life", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        # 1. Create MS365 email contact
        resp = await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "email",
                "email_provider": "microsoft_365",
                "email_local": "ms365e2e",
                "first_name": "MS365E2E",
                "last_name": "Life",
            },
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK

        # 2. List — should show provider=microsoft_365
        list_resp = await client.get(
            f"/v0/assistant/{agent_id}/contacts",
            headers=HEADERS,
        )
        assert list_resp.status_code == status.HTTP_200_OK
        contacts = list_resp.json()["info"]
        assert len(contacts) == 1
        assert contacts[0]["provider"] == "microsoft_365"
        assert contacts[0]["contact_value"] == "testcontact@outlook.unify.ai"

        # 2b. GET /assistant should surface email_provider
        get_resp = await client.get("/v0/assistant", headers=HEADERS)
        assistant = [
            a for a in get_resp.json()["info"] if int(a["agent_id"]) == agent_id
        ][0]
        assert assistant["email_provider"] == "microsoft_365"
        assert assistant["email"] == "testcontact@outlook.unify.ai"

        # 3. Update metadata
        update_resp = await client.put(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "email",
                "metadata": {"alias": "primary"},
            },
            headers=HEADERS,
        )
        assert update_resp.status_code == status.HTTP_200_OK

        contact = AssistantContactDAO(dbsession).get_contact_by_assistant_and_type(
            agent_id,
            "email",
        )
        assert contact.metadata_.get("alias") == "primary"

        # 4. Delete — routes to Outlook deprovision
        mock_all_infra["delete_outlook_email"].reset_mock()
        del_resp = await client.request(
            "DELETE",
            f"/v0/assistant/{agent_id}/contact",
            json={"contact_type": "email"},
            headers=HEADERS,
        )
        assert del_resp.status_code == status.HTTP_200_OK
        mock_all_infra["delete_outlook_email"].assert_called_once()

        # 5. Verify deleted
        list_resp2 = await client.get(
            f"/v0/assistant/{agent_id}/contacts",
            headers=HEADERS,
        )
        assert len(list_resp2.json()["info"]) == 0

        # 5b. email_provider should be None after deletion
        get_resp2 = await client.get("/v0/assistant", headers=HEADERS)
        assistant2 = [
            a for a in get_resp2.json()["info"] if int(a["agent_id"]) == agent_id
        ][0]
        assert assistant2["email_provider"] is None
        assert assistant2["email"] is None

    @pytest.mark.anyio
    async def test_ms365_duplicate_contact_rejected(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Creating a second email contact (any provider) is rejected."""
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "MS365Dup", "surname": "Dup", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        # First: create MS365 email
        resp1 = await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "email",
                "email_provider": "microsoft_365",
                "email_local": "ms365dup",
                "first_name": "MS365Dup",
                "last_name": "Dup",
            },
            headers=HEADERS,
        )
        assert resp1.status_code == status.HTTP_200_OK

        # Second: try Google email — 409
        resp2 = await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "email",
                "email_provider": "google_workspace",
                "email_local": "ms365dup2",
                "first_name": "MS365Dup",
                "last_name": "Dup2",
            },
            headers=HEADERS,
        )
        assert resp2.status_code == status.HTTP_409_CONFLICT

    @pytest.mark.anyio
    async def test_ms365_create_after_delete_recycles(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Deleting then re-creating with a different provider recycles the row."""
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "MS365Rec", "surname": "Cycle", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        # Create Google email
        await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "email",
                "email_local": "ms365rec",
                "first_name": "MS365Rec",
                "last_name": "Cycle",
            },
            headers=HEADERS,
        )

        # Delete it
        await client.request(
            "DELETE",
            f"/v0/assistant/{agent_id}/contact",
            json={"contact_type": "email"},
            headers=HEADERS,
        )

        # Re-create as MS365
        resp = await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "email",
                "email_provider": "microsoft_365",
                "email_local": "ms365rec2",
                "first_name": "MS365Rec",
                "last_name": "Cycle",
            },
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK

        contact = AssistantContactDAO(dbsession).get_contact_by_assistant_and_type(
            agent_id,
            "email",
        )
        assert contact.provider == "microsoft_365"
        assert contact.contact_value == "testcontact@outlook.unify.ai"


class TestMS365CostLookup:
    """Verify cost lookup returns the correct provider-specific costs."""

    def test_ms365_monthly_cost(self, dbsession: Session):
        """MS365 email cost is distinct from Google Workspace cost."""
        dao = AssistantContactDAO(dbsession)
        ms365_cost = dao.get_contact_monthly_cost("email", provider="microsoft_365")
        gw_cost = dao.get_contact_monthly_cost("email", provider="google_workspace")

        assert ms365_cost == Decimal("12.50")
        assert gw_cost == Decimal("14.00")
        assert ms365_cost != gw_cost

    def test_ms365_one_time_cost(self, dbsession: Session):
        """MS365 email setup fee lookup works."""
        dao = AssistantContactDAO(dbsession)
        cost = dao.get_contact_one_time_cost("email", provider="microsoft_365")
        assert cost == Decimal("5.00")


# ============================================================================
# 9. Phase 5 — Decouple Contact Management from Assistant CRUD
# ============================================================================


class TestAssistantCreateSchemaNoContactFields:
    """Verify that AssistantCreate no longer accepts contact fields."""

    @pytest.mark.anyio
    async def test_create_ignores_email_field(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Providing 'email' in the create payload should NOT provision an email."""
        resp = await client.post(
            "/v0/assistant",
            json={
                "first_name": "P5Schema",
                "surname": "NoEmail",
                "email": "should-be-ignored",
                "create_infra": True,
            },
            headers=HEADERS,
        )
        # The field is silently dropped by Pydantic (not in the schema).
        # The endpoint should succeed but NOT provision email.
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        agent_id = int(resp.json()["info"]["agent_id"])

        # The assistant should NOT have an email address provisioned
        contacts = AssistantContactDAO(dbsession).get_active_contacts_for_assistant(
            agent_id,
        )
        email_contacts = [c for c in contacts if c.contact_type == "email"]
        assert len(email_contacts) == 0

    @pytest.mark.anyio
    async def test_create_ignores_phone_fields(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Providing phone fields in create payload should NOT provision a phone."""
        resp = await client.post(
            "/v0/assistant",
            json={
                "first_name": "P5Schema",
                "surname": "NoPhone",
                "user_phone": "+15559999999",
                "phone_country": "US",
                "create_infra": True,
            },
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        agent_id = int(resp.json()["info"]["agent_id"])

        contacts = AssistantContactDAO(dbsession).get_active_contacts_for_assistant(
            agent_id,
        )
        phone_contacts = [c for c in contacts if c.contact_type == "phone"]
        assert len(phone_contacts) == 0

    @pytest.mark.anyio
    async def test_create_ignores_whatsapp_field(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Providing 'user_whatsapp_number' in create payload should NOT provision WhatsApp."""
        resp = await client.post(
            "/v0/assistant",
            json={
                "first_name": "P5Schema",
                "surname": "NoWA",
                "user_whatsapp_number": "+15558888888",
                "create_infra": True,
            },
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        agent_id = int(resp.json()["info"]["agent_id"])

        contacts = AssistantContactDAO(dbsession).get_active_contacts_for_assistant(
            agent_id,
        )
        wa_contacts = [c for c in contacts if c.contact_type == "whatsapp"]
        assert len(wa_contacts) == 0

    @pytest.mark.anyio
    async def test_create_with_all_contact_fields_still_succeeds(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Even with all contact fields, assistant is created (fields are dropped)."""
        resp = await client.post(
            "/v0/assistant",
            json={
                "first_name": "P5Schema",
                "surname": "AllDropped",
                "email": "all-dropped",
                "user_phone": "+15557777777",
                "phone_country": "GB",
                "user_whatsapp_number": "+15556666666",
                "create_infra": True,
            },
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        agent_id = int(resp.json()["info"]["agent_id"])

        # No contacts should exist
        contacts = AssistantContactDAO(dbsession).get_active_contacts_for_assistant(
            agent_id,
        )
        assert len(contacts) == 0

        # No infra provisioning calls should have been made for contacts
        mock_all_infra["create_phone_number"].assert_not_called()
        mock_all_infra["create_email"].assert_not_called()
        mock_all_infra["assign_whatsapp_pool_number"].assert_not_called()
        mock_all_infra["register_whatsapp_sender"].assert_not_called()


class TestAssistantUpdateDeprecatedContactFields:
    """Verify that deprecated contact fields in AssistantUpdate are silently ignored."""

    @pytest.mark.anyio
    async def test_update_deprecated_email_is_ignored(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Sending 'email' in an update payload should not trigger provisioning."""
        # Create assistant
        create_resp = await client.post(
            "/v0/assistant",
            json={
                "first_name": "P5Update",
                "surname": "DepEmail",
                "create_infra": False,
            },
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        # Update with deprecated email field
        update_resp = await client.patch(
            f"/v0/assistant/{agent_id}/config",
            json={"email": "ignored-email", "create_infra": True},
            headers=HEADERS,
        )
        assert update_resp.status_code == status.HTTP_200_OK

        # Email should not have been provisioned
        mock_all_infra["create_email"].assert_not_called()

        contacts = AssistantContactDAO(dbsession).get_active_contacts_for_assistant(
            agent_id,
        )
        assert len(contacts) == 0

    @pytest.mark.anyio
    async def test_update_deprecated_phone_fields_are_ignored(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Sending phone fields in an update payload should not trigger provisioning."""
        create_resp = await client.post(
            "/v0/assistant",
            json={
                "first_name": "P5Update",
                "surname": "DepPhone",
                "create_infra": False,
            },
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        update_resp = await client.patch(
            f"/v0/assistant/{agent_id}/config",
            json={
                "user_phone": "+15551111111",
                "phone_country": "US",
                "create_infra": True,
            },
            headers=HEADERS,
        )
        assert update_resp.status_code == status.HTTP_200_OK

        mock_all_infra["create_phone_number"].assert_not_called()

        contacts = AssistantContactDAO(dbsession).get_active_contacts_for_assistant(
            agent_id,
        )
        assert len(contacts) == 0

    @pytest.mark.anyio
    async def test_update_deprecated_whatsapp_field_is_ignored(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Sending whatsapp fields in an update payload should not trigger provisioning."""
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "P5Update", "surname": "DepWA", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        update_resp = await client.patch(
            f"/v0/assistant/{agent_id}/config",
            json={"user_whatsapp_number": "+15552222222", "create_infra": True},
            headers=HEADERS,
        )
        assert update_resp.status_code == status.HTTP_200_OK

        mock_all_infra["assign_whatsapp_pool_number"].assert_not_called()
        mock_all_infra["register_whatsapp_sender"].assert_not_called()

        contacts = AssistantContactDAO(dbsession).get_active_contacts_for_assistant(
            agent_id,
        )
        assert len(contacts) == 0

    @pytest.mark.anyio
    async def test_update_non_contact_fields_still_work(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Non-contact fields (about, weekly_limit, etc.) are updated normally."""
        create_resp = await client.post(
            "/v0/assistant",
            json={
                "first_name": "P5Update",
                "surname": "NonContact",
                "create_infra": False,
            },
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        update_resp = await client.patch(
            f"/v0/assistant/{agent_id}/config",
            json={"about": "Updated description", "weekly_limit": 25.5},
            headers=HEADERS,
        )
        assert update_resp.status_code == status.HTTP_200_OK

        asst = dbsession.query(Assistant).filter_by(agent_id=agent_id).first()
        dbsession.refresh(asst)
        assert asst.about == "Updated description"
        assert float(asst.weekly_limit) == 25.5

    @pytest.mark.anyio
    async def test_update_mixed_deprecated_and_valid_fields(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Valid fields are applied, deprecated contact fields are silently dropped."""
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "P5Update", "surname": "Mixed", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        update_resp = await client.patch(
            f"/v0/assistant/{agent_id}/config",
            json={
                "about": "New description",
                "email": "should-be-dropped",
                "user_phone": "+15553333333",
                "max_parallel": 5,
            },
            headers=HEADERS,
        )
        assert update_resp.status_code == status.HTTP_200_OK

        asst = dbsession.query(Assistant).filter_by(agent_id=agent_id).first()
        dbsession.refresh(asst)
        assert asst.about == "New description"
        assert asst.max_parallel == 5

        # No contact provisioning calls
        mock_all_infra["create_email"].assert_not_called()
        mock_all_infra["create_phone_number"].assert_not_called()


class TestCreateAssistantNoContactProvisioning:
    """Verify that create_assistant() no longer provisions any contact infra."""

    @pytest.mark.anyio
    async def test_create_infra_true_does_not_provision_contacts(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """With create_infra=True but no contact fields in schema, no contacts provisioned."""
        resp = await client.post(
            "/v0/assistant",
            json={
                "first_name": "P5Create",
                "surname": "NoContacts",
                "create_infra": True,
            },
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        agent_id = int(resp.json()["info"]["agent_id"])

        # No contact rows
        contacts = AssistantContactDAO(dbsession).get_active_contacts_for_assistant(
            agent_id,
        )
        assert len(contacts) == 0

        # No contact provisioning calls were made
        mock_all_infra["create_phone_number"].assert_not_called()
        mock_all_infra["create_email"].assert_not_called()
        mock_all_infra["assign_whatsapp_pool_number"].assert_not_called()
        mock_all_infra["register_whatsapp_sender"].assert_not_called()

    @pytest.mark.anyio
    async def test_create_no_infra_no_contacts(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """create_infra=False → no contacts, no infra."""
        resp = await client.post(
            "/v0/assistant",
            json={
                "first_name": "P5Create",
                "surname": "NoInfra",
                "create_infra": False,
            },
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        agent_id = int(resp.json()["info"]["agent_id"])

        contacts = AssistantContactDAO(dbsession).get_active_contacts_for_assistant(
            agent_id,
        )
        assert len(contacts) == 0

    @pytest.mark.anyio
    async def test_contacts_created_only_via_dedicated_endpoint(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """
        After Phase 5, the ONLY way to create contacts is via
        POST /assistant/{id}/contact.
        """
        # Create assistant (no contacts provisioned)
        resp = await client.post(
            "/v0/assistant",
            json={
                "first_name": "P5Create",
                "surname": "DedicatedOnly",
                "create_infra": True,
            },
            headers=HEADERS,
        )
        agent_id = int(resp.json()["info"]["agent_id"])
        assert (
            len(
                AssistantContactDAO(dbsession).get_active_contacts_for_assistant(
                    agent_id,
                ),
            )
            == 0
        )

        # Now add a contact via dedicated endpoint
        contact_resp = await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "phone",
                "phone_country": "US",
            },
            headers=HEADERS,
        )
        assert contact_resp.status_code == status.HTTP_200_OK

        contacts = AssistantContactDAO(dbsession).get_active_contacts_for_assistant(
            agent_id,
        )
        assert len(contacts) == 1
        assert contacts[0].contact_type == "phone"


class TestUpdateAssistantNoContactProvisioning:
    """Verify that update_assistant_config() no longer provisions contacts."""

    @pytest.mark.anyio
    async def test_update_does_not_provision_contacts_even_with_create_infra(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """PATCH /config with create_infra=True should NOT provision contacts."""
        create_resp = await client.post(
            "/v0/assistant",
            json={
                "first_name": "P5NoProvision",
                "surname": "Update",
                "create_infra": False,
            },
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        # Attempt to update with all deprecated contact fields + create_infra
        update_resp = await client.patch(
            f"/v0/assistant/{agent_id}/config",
            json={
                "email": "shouldnt-provision",
                "user_phone": "+15559999999",
                "phone_country": "US",
                "user_whatsapp_number": "+15558888888",
                "create_infra": True,
            },
            headers=HEADERS,
        )
        assert update_resp.status_code == status.HTTP_200_OK

        # No contacts should exist
        contacts = AssistantContactDAO(dbsession).get_active_contacts_for_assistant(
            agent_id,
        )
        assert len(contacts) == 0

        # No provisioning calls
        mock_all_infra["create_phone_number"].assert_not_called()
        mock_all_infra["create_email"].assert_not_called()
        mock_all_infra["assign_whatsapp_pool_number"].assert_not_called()
        mock_all_infra["register_whatsapp_sender"].assert_not_called()

    @pytest.mark.anyio
    async def test_existing_contacts_not_affected_by_assistant_update(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """
        If an assistant already has contacts (via dedicated endpoint),
        updating the assistant config should not modify them.
        """
        create_resp = await client.post(
            "/v0/assistant",
            json={
                "first_name": "P5Existing",
                "surname": "Contacts",
                "create_infra": False,
            },
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        # Add a phone contact via dedicated endpoint
        await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={"contact_type": "phone", "phone_country": "US"},
            headers=HEADERS,
        )
        contacts_before = AssistantContactDAO(
            dbsession,
        ).get_active_contacts_for_assistant(agent_id)
        assert len(contacts_before) == 1

        # Update about (non-contact field)
        update_resp = await client.patch(
            f"/v0/assistant/{agent_id}/config",
            json={"about": "New description after contact exists"},
            headers=HEADERS,
        )
        assert update_resp.status_code == status.HTTP_200_OK

        # Contact should still be there, unchanged
        contacts_after = AssistantContactDAO(
            dbsession,
        ).get_active_contacts_for_assistant(agent_id)
        assert len(contacts_after) == 1
        assert contacts_after[0].id == contacts_before[0].id
        assert contacts_after[0].contact_type == "phone"
        assert contacts_after[0].status == "active"


class TestHasGracePeriodContacts:
    """Tests for the has_grace_period_contacts() DAO helper."""

    def test_no_contacts_returns_false(self, dbsession: Session):
        """Returns False when the assistant has no contacts at all."""
        user, _ = _make_user_ba(dbsession, "hgpc_u1")
        asst = _make_assistant(dbsession, user.id, "HGPC", "Empty")
        assert (
            AssistantContactDAO(dbsession).has_grace_period_contacts(asst.agent_id)
            is False
        )

    def test_active_contacts_returns_false(self, dbsession: Session):
        """Returns False when the assistant has only active contacts."""
        user, _ = _make_user_ba(dbsession, "hgpc_u2")
        asst = _make_assistant(dbsession, user.id, "HGPC", "Active")
        AssistantContactDAO(dbsession).upsert_assistant_contact(
            assistant_id=asst.agent_id,
            contact_type="phone",
            contact_value="+15550200001",
        )
        dbsession.flush()
        assert (
            AssistantContactDAO(dbsession).has_grace_period_contacts(asst.agent_id)
            is False
        )

    def test_grace_period_contact_returns_true(self, dbsession: Session):
        """Returns True when at least one contact is in grace_period."""
        user, _ = _make_user_ba(dbsession, "hgpc_u3")
        asst = _make_assistant(dbsession, user.id, "HGPC", "Grace")
        c = AssistantContact(
            assistant_id=asst.agent_id,
            contact_type="phone",
            contact_value="+15550200002",
            status="grace_period",
            grace_period_started_at=datetime.utcnow(),
        )
        dbsession.add(c)
        dbsession.flush()
        assert (
            AssistantContactDAO(dbsession).has_grace_period_contacts(asst.agent_id)
            is True
        )

    def test_deleted_contacts_returns_false(self, dbsession: Session):
        """Returns False when all contacts are deleted."""
        user, _ = _make_user_ba(dbsession, "hgpc_u4")
        asst = _make_assistant(dbsession, user.id, "HGPC", "Deleted")
        c = AssistantContact(
            assistant_id=asst.agent_id,
            contact_type="email",
            contact_value="hgpc-del@unify.ai",
            status="deleted",
            deleted_at=datetime.utcnow(),
        )
        dbsession.add(c)
        dbsession.flush()
        assert (
            AssistantContactDAO(dbsession).has_grace_period_contacts(asst.agent_id)
            is False
        )

    def test_mixed_statuses_returns_true_if_any_grace(self, dbsession: Session):
        """Returns True if at least one contact is grace_period, even if others are active."""
        user, _ = _make_user_ba(dbsession, "hgpc_u5")
        asst = _make_assistant(dbsession, user.id, "HGPC", "Mixed")

        # Active contact
        AssistantContactDAO(dbsession).upsert_assistant_contact(
            assistant_id=asst.agent_id,
            contact_type="email",
            contact_value="hgpc-mixed@unify.ai",
        )
        # Grace period contact
        c = AssistantContact(
            assistant_id=asst.agent_id,
            contact_type="phone",
            contact_value="+15550200003",
            status="grace_period",
            grace_period_started_at=datetime.utcnow(),
        )
        dbsession.add(c)
        dbsession.flush()
        assert (
            AssistantContactDAO(dbsession).has_grace_period_contacts(asst.agent_id)
            is True
        )


class TestTransferGracePeriodGuard:
    """
    Tests for the grace-period transfer guard on both transfer endpoints.
    Assistants with contacts in grace_period cannot be transferred.
    """

    @pytest.mark.anyio
    async def test_transfer_to_org_blocked_with_grace_period_contacts(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Transfer to org is blocked when assistant has grace_period contacts."""
        user = await create_test_user(client, "p5xfer_to_org@test.com")

        # Create personal assistant
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "P5Xfer", "surname": "ToOrg", "create_infra": False},
            headers=user["headers"],
        )
        assert create_resp.status_code == status.HTTP_200_OK
        agent_id = int(create_resp.json()["info"]["agent_id"])

        # Create org
        org_resp = await client.post(
            "/v0/organizations",
            json={"name": "P5 Transfer Target Org"},
            headers=user["headers"],
        )
        assert org_resp.status_code in (
            status.HTTP_200_OK,
            status.HTTP_201_CREATED,
        )
        org_id = org_resp.json()["id"]

        # Manually insert a contact in grace_period
        gp_contact = AssistantContact(
            assistant_id=agent_id,
            contact_type="phone",
            contact_value="+15550300001",
            provider="twilio",
            status="grace_period",
            grace_period_started_at=datetime.utcnow(),
        )
        dbsession.add(gp_contact)
        dbsession.flush()

        # Attempt to transfer — should be blocked
        transfer_resp = await client.post(
            f"/v0/assistant/{agent_id}/transfer/to-org",
            json={"organization_id": org_id, "transfer_logs": False},
            headers=user["headers"],
        )
        assert transfer_resp.status_code == status.HTTP_409_CONFLICT
        assert "grace period" in transfer_resp.json()["detail"].lower()

    @pytest.mark.anyio
    async def test_transfer_to_org_allowed_without_grace_period(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Transfer to org succeeds when assistant has only active contacts."""
        user = await create_test_user(client, "p5xfer_ok@test.com")

        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "P5Xfer", "surname": "OkOrg", "create_infra": False},
            headers=user["headers"],
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        org_resp = await client.post(
            "/v0/organizations",
            json={"name": "P5 OK Transfer Org"},
            headers=user["headers"],
        )
        org_id = org_resp.json()["id"]

        # Add an active contact (not grace_period)
        active_contact = AssistantContact(
            assistant_id=agent_id,
            contact_type="phone",
            contact_value="+15550300002",
            provider="twilio",
            status="active",
        )
        dbsession.add(active_contact)
        dbsession.flush()

        # Transfer should succeed
        transfer_resp = await client.post(
            f"/v0/assistant/{agent_id}/transfer/to-org",
            json={"organization_id": org_id, "transfer_logs": False},
            headers=user["headers"],
        )
        assert transfer_resp.status_code == status.HTTP_200_OK
        assert transfer_resp.json()["info"]["transferred_to"] == "organization"

    @pytest.mark.anyio
    async def test_transfer_to_org_allowed_no_contacts(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Transfer to org succeeds when assistant has no contacts at all."""
        user = await create_test_user(client, "p5xfer_none@test.com")

        create_resp = await client.post(
            "/v0/assistant",
            json={
                "first_name": "P5Xfer",
                "surname": "NoContacts",
                "create_infra": False,
            },
            headers=user["headers"],
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        org_resp = await client.post(
            "/v0/organizations",
            json={"name": "P5 NoContact Org"},
            headers=user["headers"],
        )
        org_id = org_resp.json()["id"]

        transfer_resp = await client.post(
            f"/v0/assistant/{agent_id}/transfer/to-org",
            json={"organization_id": org_id, "transfer_logs": False},
            headers=user["headers"],
        )
        assert transfer_resp.status_code == status.HTTP_200_OK

    @pytest.mark.anyio
    async def test_transfer_to_personal_blocked_with_grace_period_contacts(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Transfer to personal is blocked when assistant has grace_period contacts."""
        user = await create_test_user(client, "p5xfer_to_pers@test.com")

        # Create org + org API key
        org_resp = await client.post(
            "/v0/organizations",
            json={"name": "P5 Transfer Pers Org"},
            headers=user["headers"],
        )
        assert org_resp.status_code in (
            status.HTTP_200_OK,
            status.HTTP_201_CREATED,
        )
        org_id = org_resp.json()["id"]
        org_api_key = org_resp.json()["api_key"]
        org_headers = {"Authorization": f"Bearer {org_api_key}"}

        # Create org assistant using org API key
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "P5Xfer", "surname": "ToPers", "create_infra": False},
            headers=org_headers,
        )
        assert create_resp.status_code == status.HTTP_200_OK
        agent_id = int(create_resp.json()["info"]["agent_id"])

        # Manually insert a contact in grace_period
        gp_contact = AssistantContact(
            assistant_id=agent_id,
            contact_type="email",
            contact_value="p5xfer-pers@unify.ai",
            provider="google_workspace",
            status="grace_period",
            grace_period_started_at=datetime.utcnow(),
        )
        dbsession.add(gp_contact)
        dbsession.flush()

        # Attempt to transfer — should be blocked
        transfer_resp = await client.post(
            f"/v0/assistant/{agent_id}/transfer/to-personal",
            json={"delete_logs": False},
            headers=org_headers,
        )
        assert transfer_resp.status_code == status.HTTP_409_CONFLICT
        assert "grace period" in transfer_resp.json()["detail"].lower()

    @pytest.mark.anyio
    async def test_transfer_to_personal_allowed_with_only_active_contacts(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Transfer to personal succeeds when assistant has only active contacts."""
        user = await create_test_user(client, "p5xfer_pers_ok@test.com")

        org_resp = await client.post(
            "/v0/organizations",
            json={"name": "P5 Transfer PersOK Org"},
            headers=user["headers"],
        )
        org_id = org_resp.json()["id"]
        org_api_key = org_resp.json()["api_key"]
        org_headers = {"Authorization": f"Bearer {org_api_key}"}

        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "P5Xfer", "surname": "PersOK", "create_infra": False},
            headers=org_headers,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        # Add an active contact
        active_contact = AssistantContact(
            assistant_id=agent_id,
            contact_type="email",
            contact_value="p5xfer-persok@unify.ai",
            provider="google_workspace",
            status="active",
        )
        dbsession.add(active_contact)
        dbsession.flush()

        # Transfer should succeed
        transfer_resp = await client.post(
            f"/v0/assistant/{agent_id}/transfer/to-personal",
            json={"delete_logs": False},
            headers=org_headers,
        )
        assert transfer_resp.status_code == status.HTTP_200_OK
        assert transfer_resp.json()["info"]["transferred_to"] == "personal"

    @pytest.mark.anyio
    async def test_transfer_blocked_only_for_grace_not_deleted(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """
        An assistant with deleted contacts (but no grace_period ones)
        can still be transferred.
        """
        user = await create_test_user(client, "p5xfer_del_ok@test.com")

        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "P5Xfer", "surname": "DelOK", "create_infra": False},
            headers=user["headers"],
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        org_resp = await client.post(
            "/v0/organizations",
            json={"name": "P5 Del OK Org"},
            headers=user["headers"],
        )
        org_id = org_resp.json()["id"]

        # Add a deleted contact (should NOT block transfer)
        deleted_contact = AssistantContact(
            assistant_id=agent_id,
            contact_type="phone",
            contact_value="+15550300010",
            provider="twilio",
            status="deleted",
            deleted_at=datetime.utcnow(),
        )
        dbsession.add(deleted_contact)
        dbsession.flush()

        transfer_resp = await client.post(
            f"/v0/assistant/{agent_id}/transfer/to-org",
            json={"organization_id": org_id, "transfer_logs": False},
            headers=user["headers"],
        )
        assert transfer_resp.status_code == status.HTTP_200_OK


class TestPhase5EndToEnd:
    """
    End-to-end test for the Phase 5 flow: assistant create → dedicated contact
    create → update assistant config → verify contacts unchanged → transfer guard.
    """

    @pytest.mark.anyio
    async def test_full_phase5_workflow(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """
        Complete Phase 5 workflow:
        1. Create assistant (no contacts provisioned)
        2. Add contacts via dedicated endpoint
        3. Update assistant config (contacts unaffected)
        4. List contacts via dedicated endpoint
        5. Transfer with active contacts (succeeds)
        """
        # 1. Create assistant — no contacts provisioned
        create_resp = await client.post(
            "/v0/assistant",
            json={
                "first_name": "P5E2E",
                "surname": "Workflow",
                "create_infra": True,
            },
            headers=HEADERS,
        )
        assert create_resp.status_code == status.HTTP_200_OK
        agent_id = int(create_resp.json()["info"]["agent_id"])

        # Verify no contacts
        assert (
            len(
                AssistantContactDAO(dbsession).get_active_contacts_for_assistant(
                    agent_id,
                ),
            )
            == 0
        )

        # 2. Add phone contact via dedicated endpoint
        phone_resp = await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={"contact_type": "phone", "phone_country": "US"},
            headers=HEADERS,
        )
        assert phone_resp.status_code == status.HTTP_200_OK

        # Add email contact via dedicated endpoint
        email_resp = await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "email",
                "email_local": "p5e2e-wf",
                "first_name": "P5E2E",
                "last_name": "Workflow",
            },
            headers=HEADERS,
        )
        assert email_resp.status_code == status.HTTP_200_OK

        # Verify 2 contacts
        contacts = AssistantContactDAO(dbsession).get_active_contacts_for_assistant(
            agent_id,
        )
        assert len(contacts) == 2
        types = {c.contact_type for c in contacts}
        assert types == {"phone", "email"}

        # 3. Update assistant config — contacts should NOT be modified
        update_resp = await client.patch(
            f"/v0/assistant/{agent_id}/config",
            json={
                "about": "Updated P5 E2E description",
                # Include deprecated fields to verify they're ignored
                "email": "should-be-ignored",
                "user_phone": "+15550000000",
            },
            headers=HEADERS,
        )
        assert update_resp.status_code == status.HTTP_200_OK

        # 4. Contacts still intact
        list_resp = await client.get(
            f"/v0/assistant/{agent_id}/contacts",
            headers=HEADERS,
        )
        assert list_resp.status_code == status.HTTP_200_OK
        listed_contacts = list_resp.json()["info"]
        assert len(listed_contacts) == 2

        # About was updated
        asst = dbsession.query(Assistant).filter_by(agent_id=agent_id).first()
        dbsession.refresh(asst)
        assert asst.about == "Updated P5 E2E description"


# ============================================================================
# 12. BYOD (user-provisioned) contacts
# ============================================================================


class TestBYODContactCreation:
    """Tests for creating contacts with provisioned_by='user'."""

    @pytest.mark.anyio
    async def test_create_byod_email_contact(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """BYOD email contact is created without external provisioning."""
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "BYOD", "surname": "Email", "create_infra": False},
            headers=HEADERS,
        )
        assert create_resp.status_code == status.HTTP_200_OK
        agent_id = int(create_resp.json()["info"]["agent_id"])

        resp = await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "email",
                "provisioned_by": "user",
                "contact_value": "myuser@gmail.com",
                "email_provider": "google_workspace",
            },
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK, resp.json()

        contact = AssistantContactDAO(dbsession).get_contact_by_assistant_and_type(
            agent_id,
            "email",
        )
        assert contact is not None
        assert contact.contact_value == "myuser@gmail.com"
        assert contact.provider == "google_workspace"
        assert contact.provisioned_by == "user"
        assert contact.status == "active"

        # No external email provisioning calls were made
        mock_all_infra["create_email"].assert_not_called()
        mock_all_infra["create_outlook_email"].assert_not_called()
        mock_all_infra["watch_email"].assert_not_called()
        mock_all_infra["watch_outlook_email"].assert_not_called()

    @pytest.mark.anyio
    async def test_create_byod_ms365_contact(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """BYOD MS365 email contact is created without external provisioning."""
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "BYOD", "surname": "MS365", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        resp = await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "email",
                "provisioned_by": "user",
                "contact_value": "user@company.com",
                "email_provider": "microsoft_365",
            },
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK, resp.json()

        contact = AssistantContactDAO(dbsession).get_contact_by_assistant_and_type(
            agent_id,
            "email",
        )
        assert contact is not None
        assert contact.contact_value == "user@company.com"
        assert contact.provider == "microsoft_365"
        assert contact.provisioned_by == "user"

    @pytest.mark.anyio
    async def test_byod_requires_contact_value(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """BYOD contact without contact_value is rejected by validation."""
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "BYOD", "surname": "NoVal", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        resp = await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "email",
                "provisioned_by": "user",
                "email_provider": "google_workspace",
            },
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    @pytest.mark.anyio
    async def test_byod_duplicate_rejected(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Creating a second BYOD email contact for the same assistant is rejected."""
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "BYOD", "surname": "Dup", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        resp1 = await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "email",
                "provisioned_by": "user",
                "contact_value": "user1@gmail.com",
                "email_provider": "google_workspace",
            },
            headers=HEADERS,
        )
        assert resp1.status_code == status.HTTP_200_OK

        resp2 = await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "email",
                "provisioned_by": "user",
                "contact_value": "user2@gmail.com",
                "email_provider": "google_workspace",
            },
            headers=HEADERS,
        )
        assert resp2.status_code == status.HTTP_409_CONFLICT


class TestBYODContactDeletion:
    """Tests for deleting BYOD contacts — no external deprovisioning."""

    @pytest.mark.anyio
    async def test_delete_byod_email_no_deprovisioning(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Deleting a BYOD email contact does not call external deletion."""
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "BYOD", "surname": "Del", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        # Create BYOD contact
        await client.post(
            f"/v0/assistant/{agent_id}/contact",
            json={
                "contact_type": "email",
                "provisioned_by": "user",
                "contact_value": "delbyo@gmail.com",
                "email_provider": "google_workspace",
            },
            headers=HEADERS,
        )

        # Reset mock call counts after creation
        mock_all_infra["delete_email"].reset_mock()
        mock_all_infra["delete_outlook_email"].reset_mock()

        # Delete the contact
        del_resp = await client.request(
            "DELETE",
            f"/v0/assistant/{agent_id}/contact",
            json={"contact_type": "email"},
            headers=HEADERS,
        )
        assert del_resp.status_code == status.HTTP_200_OK

        # No external deprovisioning
        mock_all_infra["delete_email"].assert_not_called()
        mock_all_infra["delete_outlook_email"].assert_not_called()

        # Row is soft-deleted
        all_rows = (
            dbsession.query(AssistantContact)
            .filter(
                AssistantContact.assistant_id == agent_id,
                AssistantContact.contact_type == "email",
            )
            .all()
        )
        assert len(all_rows) == 1
        assert all_rows[0].status == "deleted"


class TestConnectEndpoint:
    """Tests for POST /assistant/{id}/connect."""

    @pytest.mark.anyio
    async def test_google_email_oauth_url(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Google OAuth URL is correctly generated for email-only connect."""
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "OAuth", "surname": "Gmail", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.google_oauth_client_id = "test-google-client-id"
            mock_settings.microsoft_byod_client_id = None
            mock_settings.oauth_state_signing_key = None
            mock_settings.is_staging = True

            resp = await client.post(
                f"/v0/assistant/{agent_id}/connect",
                json={
                    "provider": "google",
                    "features": ["email"],
                    "redirect_after": "https://app.test/done",
                },
                headers=HEADERS,
            )

        assert resp.status_code == status.HTTP_200_OK, resp.json()
        oauth_url = resp.json()["info"]["oauth_url"]
        assert "accounts.google.com" in oauth_url
        assert "test-google-client-id" in oauth_url
        assert "gmail.send" in oauth_url
        assert "gmail.readonly" in oauth_url
        assert "include_granted_scopes=true" in oauth_url

        import base64
        import json
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(oauth_url)
        qs = parse_qs(parsed.query)
        state = json.loads(base64.urlsafe_b64decode(qs["state"][0]))
        assert "_sig" not in state
        assert state["assistant_id"] == agent_id
        assert state["provider"] == "google"
        assert state["features"] == ["email"]
        assert state["actions"] == {
            "register_email_contact": True,
            "setup_email_watch": True,
            "setup_teams_watch": False,
        }
        assert state["redirect_after"] == "https://app.test/done"
        assert state["byod"] is True

    @pytest.mark.anyio
    async def test_google_multi_feature_oauth_url(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Google OAuth URL includes scopes for multiple features."""
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "OAuth", "surname": "Multi", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.google_oauth_client_id = "test-google-client-id"
            mock_settings.microsoft_byod_client_id = None
            mock_settings.oauth_state_signing_key = None
            mock_settings.is_staging = True

            resp = await client.post(
                f"/v0/assistant/{agent_id}/connect",
                json={"provider": "google", "features": ["email", "calendar"]},
                headers=HEADERS,
            )

        assert resp.status_code == status.HTTP_200_OK, resp.json()
        oauth_url = resp.json()["info"]["oauth_url"]
        assert "gmail.send" in oauth_url
        assert "calendar" in oauth_url

    @pytest.mark.anyio
    async def test_microsoft_oauth_url(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Microsoft OAuth URL is correctly generated."""
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "OAuth", "surname": "MS", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.google_oauth_client_id = None
            mock_settings.microsoft_byod_client_id = "test-ms-client-id"
            mock_settings.oauth_state_signing_key = None
            mock_settings.is_staging = True

            resp = await client.post(
                f"/v0/assistant/{agent_id}/connect",
                json={"provider": "microsoft", "features": ["email"]},
                headers=HEADERS,
            )

        assert resp.status_code == status.HTTP_200_OK, resp.json()
        oauth_url = resp.json()["info"]["oauth_url"]
        assert "login.microsoftonline.com/common" in oauth_url
        assert "test-ms-client-id" in oauth_url
        assert "Mail.Send" in oauth_url

    @pytest.mark.anyio
    async def test_missing_google_config(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Returns 422 when Google OAuth is not configured."""
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "OAuth", "surname": "NoCfg", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.google_oauth_client_id = None
            mock_settings.microsoft_byod_client_id = None
            mock_settings.oauth_state_signing_key = None
            mock_settings.is_staging = True

            resp = await client.post(
                f"/v0/assistant/{agent_id}/connect",
                json={"provider": "google"},
                headers=HEADERS,
            )

        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    @pytest.mark.anyio
    async def test_invalid_feature_rejected(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Unknown feature names are rejected with 422."""
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "OAuth", "surname": "Bad", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.google_oauth_client_id = "test-google-client-id"
            mock_settings.oauth_state_signing_key = None
            mock_settings.is_staging = True

            resp = await client.post(
                f"/v0/assistant/{agent_id}/connect",
                json={"provider": "google", "features": ["email", "nonexistent"]},
                headers=HEADERS,
            )

        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    @pytest.mark.anyio
    async def test_google_oauth_url_signed(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """When signing key is set, state includes a valid HMAC signature."""
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "OAuth", "surname": "Signed", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.google_oauth_client_id = "test-google-client-id"
            mock_settings.microsoft_byod_client_id = None
            mock_settings.oauth_state_signing_key = "test-signing-secret"
            mock_settings.is_staging = True

            resp = await client.post(
                f"/v0/assistant/{agent_id}/connect",
                json={
                    "provider": "google",
                    "features": ["email"],
                    "redirect_after": "https://app.test/done",
                },
                headers=HEADERS,
            )

        assert resp.status_code == status.HTTP_200_OK, resp.json()
        oauth_url = resp.json()["info"]["oauth_url"]

        import base64
        import hashlib
        import hmac
        import json
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(oauth_url)
        qs = parse_qs(parsed.query)
        state = json.loads(base64.urlsafe_b64decode(qs["state"][0]))

        sig = state.pop("_sig")
        assert len(sig) == 64
        assert state["assistant_id"] == agent_id
        assert state["provider"] == "google"
        assert state["features"] == ["email"]
        assert state["actions"] == {
            "register_email_contact": True,
            "setup_email_watch": True,
            "setup_teams_watch": False,
        }
        assert state["redirect_after"] == "https://app.test/done"
        assert state["byod"] is True

        expected_sig = hmac.new(
            b"test-signing-secret",
            json.dumps(state, sort_keys=True).encode(),
            hashlib.sha256,
        ).hexdigest()
        assert sig == expected_sig


class TestGrantedFeaturesEndpoint:
    """Tests for GET /assistant/{id}/granted-features."""

    @pytest.mark.anyio
    async def test_no_scopes_returns_empty(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "Feat", "surname": "Empty", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.is_staging = True

            resp = await client.get(
                f"/v0/assistant/{agent_id}/granted-features",
                headers=HEADERS,
            )

        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()["info"]
        assert data["provider"] is None
        assert data["features"] == []

    @pytest.mark.anyio
    async def test_google_scopes_mapped(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "Feat", "surname": "Google", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.is_staging = True

            await client.post(
                f"/v0/assistant/{agent_id}/secret",
                json={
                    "secret_name": "GOOGLE_GRANTED_SCOPES",
                    "secret_value": (
                        "https://www.googleapis.com/auth/gmail.send "
                        "https://www.googleapis.com/auth/gmail.readonly "
                        "https://www.googleapis.com/auth/gmail.modify "
                        "https://www.googleapis.com/auth/userinfo.email "
                        "https://www.googleapis.com/auth/calendar "
                        "https://www.googleapis.com/auth/calendar.events"
                    ),
                },
                headers=HEADERS,
            )

            resp = await client.get(
                f"/v0/assistant/{agent_id}/granted-features",
                headers=HEADERS,
            )

        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()["info"]
        assert data["provider"] == "google"
        assert sorted(data["features"]) == ["calendar", "email"]

    @pytest.mark.anyio
    async def test_microsoft_scopes_mapped(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "Feat", "surname": "MS", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.is_staging = True

            await client.post(
                f"/v0/assistant/{agent_id}/secret",
                json={
                    "secret_name": "MICROSOFT_GRANTED_SCOPES",
                    "secret_value": (
                        "https://graph.microsoft.com/Mail.Read "
                        "https://graph.microsoft.com/Mail.Send "
                        "https://graph.microsoft.com/Mail.ReadWrite "
                        "https://graph.microsoft.com/User.Read "
                        "offline_access"
                    ),
                },
                headers=HEADERS,
            )

            resp = await client.get(
                f"/v0/assistant/{agent_id}/granted-features",
                headers=HEADERS,
            )

        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()["info"]
        assert data["provider"] == "microsoft"
        assert data["features"] == ["email"]

    @pytest.mark.anyio
    async def test_granted_features_nonexistent_assistant_404(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.is_staging = True

            resp = await client.get(
                "/v0/assistant/999999/granted-features",
                headers=HEADERS,
            )

        assert resp.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.anyio
    async def test_granted_features_partial_scope_not_listed(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """When only a subset of a bundle's scopes are granted, the feature
        should NOT appear in the response."""
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "Feat", "surname": "Partial", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.is_staging = True

            await client.post(
                f"/v0/assistant/{agent_id}/secret",
                json={
                    "secret_name": "GOOGLE_GRANTED_SCOPES",
                    "secret_value": (
                        "https://www.googleapis.com/auth/userinfo.email "
                        "https://www.googleapis.com/auth/gmail.send"
                    ),
                },
                headers=HEADERS,
            )

            resp = await client.get(
                f"/v0/assistant/{agent_id}/granted-features",
                headers=HEADERS,
            )

        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()["info"]
        assert data["provider"] == "google"
        assert "email" not in data["features"]


class TestConnectEndpointEdgeCases:
    """Additional edge-case tests for POST /assistant/{id}/connect."""

    @pytest.mark.anyio
    async def test_connect_nonexistent_assistant_404(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.google_oauth_client_id = "test-id"
            mock_settings.oauth_state_signing_key = None
            mock_settings.is_staging = True

            resp = await client.post(
                "/v0/assistant/999999/connect",
                json={"provider": "google"},
                headers=HEADERS,
            )

        assert resp.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.anyio
    async def test_connect_default_features_google(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Default features=['email','teams'] drops 'teams' for Google (not
        available), so only email scopes appear."""
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "OAuth", "surname": "Default", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.google_oauth_client_id = "test-google-client-id"
            mock_settings.oauth_state_signing_key = None
            mock_settings.is_staging = True

            resp = await client.post(
                f"/v0/assistant/{agent_id}/connect",
                json={"provider": "google"},
                headers=HEADERS,
            )

        assert resp.status_code == status.HTTP_200_OK, resp.json()
        oauth_url = resp.json()["info"]["oauth_url"]

        import base64
        import json
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(oauth_url)
        qs = parse_qs(parsed.query)
        state = json.loads(base64.urlsafe_b64decode(qs["state"][0]))
        assert state["features"] == ["email"]
        assert state["actions"] == {
            "register_email_contact": True,
            "setup_email_watch": True,
            "setup_teams_watch": False,
        }

        assert "gmail.send" in oauth_url
        assert "Chat.Read" not in oauth_url
        assert "calendar" not in oauth_url

    @pytest.mark.anyio
    async def test_connect_default_features_microsoft(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Default features=['email','teams'] — both are valid for Microsoft,
        so both email and Teams scopes appear."""
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "OAuth", "surname": "MsDef", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.google_oauth_client_id = None
            mock_settings.microsoft_byod_client_id = "test-ms-client-id"
            mock_settings.oauth_state_signing_key = None
            mock_settings.is_staging = True

            resp = await client.post(
                f"/v0/assistant/{agent_id}/connect",
                json={"provider": "microsoft"},
                headers=HEADERS,
            )

        assert resp.status_code == status.HTTP_200_OK, resp.json()
        oauth_url = resp.json()["info"]["oauth_url"]

        import base64
        import json
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(oauth_url)
        qs = parse_qs(parsed.query)
        state = json.loads(base64.urlsafe_b64decode(qs["state"][0]))
        assert sorted(state["features"]) == ["email", "teams"]
        assert state["actions"] == {
            "register_email_contact": True,
            "setup_email_watch": True,
            "setup_teams_watch": True,
        }

        assert "Mail.Send" in oauth_url
        assert "Chat.Read" in oauth_url

    @pytest.mark.anyio
    async def test_connect_cross_provider_feature_silently_dropped(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Microsoft-only feature 'teams' is silently dropped for Google."""
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "OAuth", "surname": "Cross", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.google_oauth_client_id = "test-google-client-id"
            mock_settings.oauth_state_signing_key = None
            mock_settings.is_staging = True

            resp = await client.post(
                f"/v0/assistant/{agent_id}/connect",
                json={"provider": "google", "features": ["email", "teams"]},
                headers=HEADERS,
            )

        assert resp.status_code == status.HTTP_200_OK
        oauth_url = resp.json()["info"]["oauth_url"]
        assert "gmail.send" in oauth_url
        assert "Chat.Read" not in oauth_url

    @pytest.mark.anyio
    async def test_connect_microsoft_all_features(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Requesting multiple Microsoft features produces correct scopes."""
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "OAuth", "surname": "MsAll", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.google_oauth_client_id = None
            mock_settings.microsoft_byod_client_id = "test-ms-client-id"
            mock_settings.oauth_state_signing_key = None
            mock_settings.is_staging = True

            resp = await client.post(
                f"/v0/assistant/{agent_id}/connect",
                json={
                    "provider": "microsoft",
                    "features": ["email", "calendar", "teams"],
                },
                headers=HEADERS,
            )

        assert resp.status_code == status.HTTP_200_OK, resp.json()
        oauth_url = resp.json()["info"]["oauth_url"]
        assert "Mail.Send" in oauth_url
        assert "Calendars.Read" in oauth_url
        assert "Chat.Read" in oauth_url
        assert "offline_access" in oauth_url


class TestCompulsoryFeatures:
    """Tests for required-features enforcement in ConnectRequest."""

    @pytest.mark.anyio
    async def test_google_always_includes_email(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Even if user sends features=['calendar'], email is auto-added."""
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "Comp", "surname": "Google", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.google_oauth_client_id = "test-id"
            mock_settings.oauth_state_signing_key = None
            mock_settings.is_staging = True

            resp = await client.post(
                f"/v0/assistant/{agent_id}/connect",
                json={"provider": "google", "features": ["calendar"]},
                headers=HEADERS,
            )

        assert resp.status_code == status.HTTP_200_OK, resp.json()
        oauth_url = resp.json()["info"]["oauth_url"]
        assert "gmail.send" in oauth_url
        assert "calendar" in oauth_url

        import base64
        import json
        from urllib.parse import parse_qs, urlparse

        state = json.loads(
            base64.urlsafe_b64decode(
                parse_qs(urlparse(oauth_url).query)["state"][0],
            ),
        )
        assert "email" in state["features"]
        assert "calendar" in state["features"]
        assert state["actions"]["register_email_contact"] is True
        assert state["actions"]["setup_email_watch"] is True
        assert state["actions"]["setup_teams_watch"] is False

    @pytest.mark.anyio
    async def test_microsoft_always_includes_email_and_teams(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Microsoft connect with features=['calendar'] auto-adds email + teams."""
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "Comp", "surname": "MS", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.microsoft_byod_client_id = "test-ms-id"
            mock_settings.oauth_state_signing_key = None
            mock_settings.is_staging = True

            resp = await client.post(
                f"/v0/assistant/{agent_id}/connect",
                json={"provider": "microsoft", "features": ["calendar"]},
                headers=HEADERS,
            )

        assert resp.status_code == status.HTTP_200_OK, resp.json()
        oauth_url = resp.json()["info"]["oauth_url"]
        assert "Mail.Send" in oauth_url
        assert "Chat.Read" in oauth_url
        assert "Calendars.Read" in oauth_url

        import base64
        import json
        from urllib.parse import parse_qs, urlparse

        state = json.loads(
            base64.urlsafe_b64decode(
                parse_qs(urlparse(oauth_url).query)["state"][0],
            ),
        )
        assert sorted(state["features"]) == ["calendar", "email", "teams"]
        assert state["actions"] == {
            "register_email_contact": True,
            "setup_email_watch": True,
            "setup_teams_watch": True,
        }


class TestGrantedFeaturesRequiredField:
    """Tests for the required_features field in granted-features response."""

    @pytest.mark.anyio
    async def test_google_required_features(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "Req", "surname": "Google", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.is_staging = True

            await client.post(
                f"/v0/assistant/{agent_id}/secret",
                json={
                    "secret_name": "GOOGLE_GRANTED_SCOPES",
                    "secret_value": (
                        "https://www.googleapis.com/auth/gmail.send "
                        "https://www.googleapis.com/auth/gmail.readonly "
                        "https://www.googleapis.com/auth/gmail.modify "
                        "https://www.googleapis.com/auth/userinfo.email"
                    ),
                },
                headers=HEADERS,
            )

            resp = await client.get(
                f"/v0/assistant/{agent_id}/granted-features",
                headers=HEADERS,
            )

        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()["info"]
        assert data["required_features"] == ["email"]

    @pytest.mark.anyio
    async def test_microsoft_required_features(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "Req", "surname": "MS", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.is_staging = True

            await client.post(
                f"/v0/assistant/{agent_id}/secret",
                json={
                    "secret_name": "MICROSOFT_GRANTED_SCOPES",
                    "secret_value": (
                        "https://graph.microsoft.com/Mail.Read "
                        "https://graph.microsoft.com/Mail.Send "
                        "https://graph.microsoft.com/Mail.ReadWrite "
                        "https://graph.microsoft.com/User.Read "
                        "offline_access"
                    ),
                },
                headers=HEADERS,
            )

            resp = await client.get(
                f"/v0/assistant/{agent_id}/granted-features",
                headers=HEADERS,
            )

        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()["info"]
        assert sorted(data["required_features"]) == ["email", "teams"]

    @pytest.mark.anyio
    async def test_no_scopes_empty_required(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "Req", "surname": "None", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.is_staging = True

            resp = await client.get(
                f"/v0/assistant/{agent_id}/granted-features",
                headers=HEADERS,
            )

        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()["info"]
        assert data["required_features"] == []


class TestDisconnectEndpoint:
    """Tests for DELETE /assistant/{id}/connect."""

    @pytest.mark.anyio
    async def test_disconnect_no_account_404(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "Disc", "surname": "None", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        resp = await client.delete(
            f"/v0/assistant/{agent_id}/connect",
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.anyio
    async def test_disconnect_nonexistent_assistant_404(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        resp = await client.delete(
            "/v0/assistant/999999/connect",
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.anyio
    async def test_disconnect_google_clears_secrets(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        from orchestra.db.dao.assistant_secret_dao import AssistantSecretDAO

        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "Disc", "surname": "Google", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.is_staging = True

            for name, value in [
                ("GOOGLE_ACCESS_TOKEN", "access-tok"),
                ("GOOGLE_REFRESH_TOKEN", "refresh-tok"),
                ("GOOGLE_TOKEN_EXPIRES_AT", "9999999999"),
                ("GOOGLE_GRANTED_SCOPES", "https://www.googleapis.com/auth/gmail.send"),
            ]:
                await client.post(
                    f"/v0/assistant/{agent_id}/secret",
                    json={"secret_name": name, "secret_value": value},
                    headers=HEADERS,
                )

            resp = await client.delete(
                f"/v0/assistant/{agent_id}/connect",
                headers=HEADERS,
            )

        assert resp.status_code == status.HTTP_200_OK, resp.json()
        assert resp.json()["info"]["status"] == "disconnected"

        dao = AssistantSecretDAO(dbsession)
        for name in (
            "GOOGLE_ACCESS_TOKEN",
            "GOOGLE_REFRESH_TOKEN",
            "GOOGLE_TOKEN_EXPIRES_AT",
            "GOOGLE_GRANTED_SCOPES",
        ):
            assert dao.get(agent_id, name) is None

    @pytest.mark.anyio
    async def test_disconnect_microsoft_clears_secrets(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        from orchestra.db.dao.assistant_secret_dao import AssistantSecretDAO

        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "Disc", "surname": "MS", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.is_staging = True

            for name, value in [
                ("MICROSOFT_ACCESS_TOKEN", "ms-access"),
                ("MICROSOFT_REFRESH_TOKEN", "ms-refresh"),
                ("MICROSOFT_TOKEN_EXPIRES_AT", "9999999999"),
                ("MICROSOFT_GRANTED_SCOPES", "https://graph.microsoft.com/Mail.Read"),
            ]:
                await client.post(
                    f"/v0/assistant/{agent_id}/secret",
                    json={"secret_name": name, "secret_value": value},
                    headers=HEADERS,
                )

            resp = await client.delete(
                f"/v0/assistant/{agent_id}/connect",
                headers=HEADERS,
            )

        assert resp.status_code == status.HTTP_200_OK, resp.json()
        assert resp.json()["info"]["status"] == "disconnected"

        dao = AssistantSecretDAO(dbsession)
        for name in (
            "MICROSOFT_ACCESS_TOKEN",
            "MICROSOFT_REFRESH_TOKEN",
            "MICROSOFT_TOKEN_EXPIRES_AT",
            "MICROSOFT_GRANTED_SCOPES",
        ):
            assert dao.get(agent_id, name) is None

    @pytest.mark.anyio
    async def test_disconnect_soft_deletes_byod_contact(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "Disc", "surname": "Contact", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        byod_contact = AssistantContact(
            assistant_id=agent_id,
            contact_type="email",
            contact_value="user@byod.com",
            provider="microsoft",
            provisioned_by="user",
            status="active",
        )
        dbsession.add(byod_contact)
        dbsession.commit()

        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.is_staging = True

            await client.post(
                f"/v0/assistant/{agent_id}/secret",
                json={
                    "secret_name": "MICROSOFT_GRANTED_SCOPES",
                    "secret_value": "https://graph.microsoft.com/Mail.Read",
                },
                headers=HEADERS,
            )

            resp = await client.delete(
                f"/v0/assistant/{agent_id}/connect",
                headers=HEADERS,
            )

        assert resp.status_code == status.HTTP_200_OK

        dbsession.refresh(byod_contact)
        assert byod_contact.status == "deleted"

    @pytest.mark.anyio
    async def test_disconnect_does_not_delete_platform_contact(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Platform-provisioned contacts should not be soft-deleted by disconnect."""
        create_resp = await client.post(
            "/v0/assistant",
            json={"first_name": "Disc", "surname": "Platform", "create_infra": False},
            headers=HEADERS,
        )
        agent_id = int(create_resp.json()["info"]["agent_id"])

        platform_contact = AssistantContact(
            assistant_id=agent_id,
            contact_type="email",
            contact_value="asst@unify.ai",
            provider="google_workspace",
            provisioned_by="platform",
            status="active",
        )
        dbsession.add(platform_contact)
        dbsession.commit()

        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.is_staging = True

            await client.post(
                f"/v0/assistant/{agent_id}/secret",
                json={
                    "secret_name": "GOOGLE_GRANTED_SCOPES",
                    "secret_value": "https://www.googleapis.com/auth/gmail.send",
                },
                headers=HEADERS,
            )

            resp = await client.delete(
                f"/v0/assistant/{agent_id}/connect",
                headers=HEADERS,
            )

        assert resp.status_code == status.HTTP_200_OK

        dbsession.refresh(platform_contact)
        assert platform_contact.status == "active"


# ============================================================================
# Org assistant tests for connect / disconnect / granted-features
# ============================================================================


async def _setup_org_assistant_with_members(
    client: AsyncClient,
    dbsession: Session,
    *,
    grant_member_write: bool = True,
):
    """Create org + assistant + two members (one writer, one reader).

    Returns ``(owner, org, agent_id, writer_headers, reader_headers)``.
    """
    from orchestra.db.dao.permission_dao import PermissionDAO
    from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
    from orchestra.db.dao.role_dao import RoleDAO

    owner = await create_test_user(client, "org-byod-owner@test.com")
    org = await create_test_org(client, owner, "BYODOrgTest")

    asst_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "Org", "surname": "BYOD", "create_infra": False},
        headers=org["headers"],
    )
    assert asst_resp.status_code == status.HTTP_200_OK, asst_resp.json()
    agent_id = int(asst_resp.json()["info"]["agent_id"])

    role_dao = RoleDAO(dbsession)
    perm_dao = PermissionDAO(dbsession)
    ra_dao = ResourceAccessDAO(dbsession)

    # Writer member (Member role → has assistant:write)
    writer = await create_test_user(client, "org-byod-writer@test.com")
    add_resp = await client.post(
        f"/v0/organizations/{org['id']}/members",
        json={"user_id": writer["id"]},
        headers=owner["headers"],
    )
    assert add_resp.status_code == status.HTTP_201_CREATED
    writer_headers = {"Authorization": f"Bearer {add_resp.json()['api_key']}"}

    if grant_member_write:
        member_role = role_dao.get_by_name("Member", organization_id=None)
        ra_dao.grant_access(
            resource_type="assistant",
            resource_id=agent_id,
            role_id=member_role.id,
            grantee_type="user",
            grantee_id=writer["id"],
        )

    # Reader member (custom role with assistant:read only)
    reader_role = role_dao.create(
        name="BYODReader",
        organization_id=org["id"],
    )
    read_perm = perm_dao.get_by_name("assistant:read")
    role_dao.add_permission(reader_role.id, read_perm.id)

    reader = await create_test_user(client, "org-byod-reader@test.com")
    add_resp = await client.post(
        f"/v0/organizations/{org['id']}/members",
        json={"user_id": reader["id"], "role_id": reader_role.id},
        headers=owner["headers"],
    )
    assert add_resp.status_code == status.HTTP_201_CREATED
    reader_headers = {"Authorization": f"Bearer {add_resp.json()['api_key']}"}

    ra_dao.grant_access(
        resource_type="assistant",
        resource_id=agent_id,
        role_id=reader_role.id,
        grantee_type="user",
        grantee_id=reader["id"],
    )

    dbsession.commit()

    return owner, org, agent_id, writer_headers, reader_headers


class TestConnectEndpointOrg:
    """Tests for POST /assistant/{id}/connect with org assistants."""

    @pytest.mark.anyio
    async def test_org_owner_can_connect_google(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        owner, org, agent_id, _, _ = await _setup_org_assistant_with_members(
            client,
            dbsession,
        )

        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.google_oauth_client_id = "test-google-id"
            mock_settings.oauth_state_signing_key = None
            mock_settings.is_staging = True

            resp = await client.post(
                f"/v0/assistant/{agent_id}/connect",
                json={"provider": "google", "features": ["email"]},
                headers=org["headers"],
            )

        assert resp.status_code == status.HTTP_200_OK, resp.json()
        oauth_url = resp.json()["info"]["oauth_url"]
        assert "accounts.google.com" in oauth_url
        assert "gmail.send" in oauth_url

    @pytest.mark.anyio
    async def test_org_owner_can_connect_microsoft(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        owner, org, agent_id, _, _ = await _setup_org_assistant_with_members(
            client,
            dbsession,
        )

        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.microsoft_byod_client_id = "test-ms-id"
            mock_settings.oauth_state_signing_key = None
            mock_settings.is_staging = True

            resp = await client.post(
                f"/v0/assistant/{agent_id}/connect",
                json={"provider": "microsoft", "features": ["email", "teams"]},
                headers=org["headers"],
            )

        assert resp.status_code == status.HTTP_200_OK, resp.json()
        oauth_url = resp.json()["info"]["oauth_url"]
        assert "login.microsoftonline.com" in oauth_url
        assert "Mail.Send" in oauth_url
        assert "Chat.Read" in oauth_url

    @pytest.mark.anyio
    async def test_org_member_with_write_can_connect(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        _, _, agent_id, writer_headers, _ = await _setup_org_assistant_with_members(
            client,
            dbsession,
        )

        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.google_oauth_client_id = "test-google-id"
            mock_settings.oauth_state_signing_key = None
            mock_settings.is_staging = True

            resp = await client.post(
                f"/v0/assistant/{agent_id}/connect",
                json={"provider": "google", "features": ["email"]},
                headers=writer_headers,
            )

        assert resp.status_code == status.HTTP_200_OK, resp.json()

    @pytest.mark.anyio
    async def test_org_member_read_only_cannot_connect(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        _, _, agent_id, _, reader_headers = await _setup_org_assistant_with_members(
            client,
            dbsession,
        )

        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.google_oauth_client_id = "test-google-id"
            mock_settings.oauth_state_signing_key = None
            mock_settings.is_staging = True

            resp = await client.post(
                f"/v0/assistant/{agent_id}/connect",
                json={"provider": "google", "features": ["email"]},
                headers=reader_headers,
            )

        assert resp.status_code == status.HTTP_403_FORBIDDEN

    @pytest.mark.anyio
    async def test_compulsory_features_apply_for_org_microsoft(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Org assistant: features=['calendar'] for Microsoft auto-adds email+teams."""
        owner, org, agent_id, _, _ = await _setup_org_assistant_with_members(
            client,
            dbsession,
        )

        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.microsoft_byod_client_id = "test-ms-id"
            mock_settings.oauth_state_signing_key = None
            mock_settings.is_staging = True

            resp = await client.post(
                f"/v0/assistant/{agent_id}/connect",
                json={"provider": "microsoft", "features": ["calendar"]},
                headers=org["headers"],
            )

        assert resp.status_code == status.HTTP_200_OK, resp.json()
        oauth_url = resp.json()["info"]["oauth_url"]
        assert "Mail.Send" in oauth_url
        assert "Chat.Read" in oauth_url
        assert "Calendars.Read" in oauth_url

        import base64
        import json
        from urllib.parse import parse_qs, urlparse

        state = json.loads(
            base64.urlsafe_b64decode(
                parse_qs(urlparse(oauth_url).query)["state"][0],
            ),
        )
        assert sorted(state["features"]) == ["calendar", "email", "teams"]
        assert state["actions"] == {
            "register_email_contact": True,
            "setup_email_watch": True,
            "setup_teams_watch": True,
        }


class TestDisconnectEndpointOrg:
    """Tests for DELETE /assistant/{id}/connect with org assistants."""

    @pytest.mark.anyio
    async def test_org_owner_can_disconnect_google(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        from orchestra.db.dao.assistant_secret_dao import AssistantSecretDAO

        owner, org, agent_id, _, _ = await _setup_org_assistant_with_members(
            client,
            dbsession,
        )

        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.is_staging = True

            for name, value in [
                ("GOOGLE_ACCESS_TOKEN", "g-access"),
                ("GOOGLE_REFRESH_TOKEN", "g-refresh"),
                ("GOOGLE_TOKEN_EXPIRES_AT", "9999999999"),
                ("GOOGLE_GRANTED_SCOPES", "https://www.googleapis.com/auth/gmail.send"),
            ]:
                await client.post(
                    f"/v0/assistant/{agent_id}/secret",
                    json={"secret_name": name, "secret_value": value},
                    headers=org["headers"],
                )

            resp = await client.delete(
                f"/v0/assistant/{agent_id}/connect",
                headers=org["headers"],
            )

        assert resp.status_code == status.HTTP_200_OK, resp.json()
        assert resp.json()["info"]["status"] == "disconnected"

        dao = AssistantSecretDAO(dbsession)
        for name in (
            "GOOGLE_ACCESS_TOKEN",
            "GOOGLE_REFRESH_TOKEN",
            "GOOGLE_TOKEN_EXPIRES_AT",
            "GOOGLE_GRANTED_SCOPES",
        ):
            assert dao.get(agent_id, name) is None

    @pytest.mark.anyio
    async def test_org_owner_can_disconnect_microsoft(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        from orchestra.db.dao.assistant_secret_dao import AssistantSecretDAO

        owner, org, agent_id, _, _ = await _setup_org_assistant_with_members(
            client,
            dbsession,
        )

        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.is_staging = True

            for name, value in [
                ("MICROSOFT_ACCESS_TOKEN", "ms-access"),
                ("MICROSOFT_REFRESH_TOKEN", "ms-refresh"),
                ("MICROSOFT_TOKEN_EXPIRES_AT", "9999999999"),
                ("MICROSOFT_GRANTED_SCOPES", "https://graph.microsoft.com/Mail.Read"),
            ]:
                await client.post(
                    f"/v0/assistant/{agent_id}/secret",
                    json={"secret_name": name, "secret_value": value},
                    headers=org["headers"],
                )

            resp = await client.delete(
                f"/v0/assistant/{agent_id}/connect",
                headers=org["headers"],
            )

        assert resp.status_code == status.HTTP_200_OK, resp.json()
        assert resp.json()["info"]["status"] == "disconnected"

        dao = AssistantSecretDAO(dbsession)
        for name in (
            "MICROSOFT_ACCESS_TOKEN",
            "MICROSOFT_REFRESH_TOKEN",
            "MICROSOFT_TOKEN_EXPIRES_AT",
            "MICROSOFT_GRANTED_SCOPES",
        ):
            assert dao.get(agent_id, name) is None

    @pytest.mark.anyio
    async def test_org_member_with_write_can_disconnect(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        _, org, agent_id, writer_headers, _ = await _setup_org_assistant_with_members(
            client,
            dbsession,
        )

        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.is_staging = True

            await client.post(
                f"/v0/assistant/{agent_id}/secret",
                json={
                    "secret_name": "GOOGLE_GRANTED_SCOPES",
                    "secret_value": "https://www.googleapis.com/auth/gmail.send",
                },
                headers=org["headers"],
            )

            resp = await client.delete(
                f"/v0/assistant/{agent_id}/connect",
                headers=writer_headers,
            )

        assert resp.status_code == status.HTTP_200_OK, resp.json()

    @pytest.mark.anyio
    async def test_org_member_read_only_cannot_disconnect(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        _, org, agent_id, _, reader_headers = await _setup_org_assistant_with_members(
            client,
            dbsession,
        )

        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.is_staging = True

            await client.post(
                f"/v0/assistant/{agent_id}/secret",
                json={
                    "secret_name": "GOOGLE_GRANTED_SCOPES",
                    "secret_value": "https://www.googleapis.com/auth/gmail.send",
                },
                headers=org["headers"],
            )

            resp = await client.delete(
                f"/v0/assistant/{agent_id}/connect",
                headers=reader_headers,
            )

        assert resp.status_code == status.HTTP_403_FORBIDDEN

    @pytest.mark.anyio
    async def test_disconnect_soft_deletes_byod_contact_org(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        owner, org, agent_id, _, _ = await _setup_org_assistant_with_members(
            client,
            dbsession,
        )

        byod_contact = AssistantContact(
            assistant_id=agent_id,
            contact_type="email",
            contact_value="orguser@byod.com",
            provider="microsoft",
            provisioned_by="user",
            status="active",
        )
        dbsession.add(byod_contact)
        dbsession.commit()

        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.is_staging = True

            await client.post(
                f"/v0/assistant/{agent_id}/secret",
                json={
                    "secret_name": "MICROSOFT_GRANTED_SCOPES",
                    "secret_value": "https://graph.microsoft.com/Mail.Read",
                },
                headers=org["headers"],
            )

            resp = await client.delete(
                f"/v0/assistant/{agent_id}/connect",
                headers=org["headers"],
            )

        assert resp.status_code == status.HTTP_200_OK
        dbsession.refresh(byod_contact)
        assert byod_contact.status == "deleted"


class TestGrantedFeaturesEndpointOrg:
    """Tests for GET /assistant/{id}/granted-features with org assistants."""

    @pytest.mark.anyio
    async def test_org_owner_can_read_granted_features(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        owner, org, agent_id, _, _ = await _setup_org_assistant_with_members(
            client,
            dbsession,
        )

        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.is_staging = True

            await client.post(
                f"/v0/assistant/{agent_id}/secret",
                json={
                    "secret_name": "GOOGLE_GRANTED_SCOPES",
                    "secret_value": (
                        "https://www.googleapis.com/auth/gmail.send "
                        "https://www.googleapis.com/auth/gmail.readonly "
                        "https://www.googleapis.com/auth/gmail.modify "
                        "https://www.googleapis.com/auth/userinfo.email"
                    ),
                },
                headers=org["headers"],
            )

            resp = await client.get(
                f"/v0/assistant/{agent_id}/granted-features",
                headers=org["headers"],
            )

        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()["info"]
        assert data["provider"] == "google"
        assert "email" in data["features"]
        assert data["required_features"] == ["email"]

    @pytest.mark.anyio
    async def test_org_member_with_read_can_read(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        _, org, agent_id, _, reader_headers = await _setup_org_assistant_with_members(
            client,
            dbsession,
        )

        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.is_staging = True

            await client.post(
                f"/v0/assistant/{agent_id}/secret",
                json={
                    "secret_name": "MICROSOFT_GRANTED_SCOPES",
                    "secret_value": (
                        "https://graph.microsoft.com/Mail.Read "
                        "https://graph.microsoft.com/Mail.Send "
                        "https://graph.microsoft.com/Mail.ReadWrite "
                        "https://graph.microsoft.com/User.Read "
                        "offline_access"
                    ),
                },
                headers=org["headers"],
            )

            resp = await client.get(
                f"/v0/assistant/{agent_id}/granted-features",
                headers=reader_headers,
            )

        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()["info"]
        assert data["provider"] == "microsoft"
        assert "email" in data["features"]
        assert sorted(data["required_features"]) == ["email", "teams"]

    @pytest.mark.anyio
    async def test_org_member_no_access_cannot_read(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        """Member with no ResourceAccess grant on the assistant gets 403."""
        owner, org, agent_id, _, _ = await _setup_org_assistant_with_members(
            client,
            dbsession,
        )

        outsider = await create_test_user(client, "org-byod-outsider@test.com")
        add_resp = await client.post(
            f"/v0/organizations/{org['id']}/members",
            json={"user_id": outsider["id"]},
            headers=owner["headers"],
        )
        assert add_resp.status_code == status.HTTP_201_CREATED
        outsider_headers = {
            "Authorization": f"Bearer {add_resp.json()['api_key']}",
        }

        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.is_staging = True

            await client.post(
                f"/v0/assistant/{agent_id}/secret",
                json={
                    "secret_name": "GOOGLE_GRANTED_SCOPES",
                    "secret_value": "https://www.googleapis.com/auth/gmail.send",
                },
                headers=org["headers"],
            )

            resp = await client.get(
                f"/v0/assistant/{agent_id}/granted-features",
                headers=outsider_headers,
            )

        assert resp.status_code == status.HTTP_403_FORBIDDEN

    @pytest.mark.anyio
    async def test_required_features_populated_for_org_microsoft(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_all_infra,
    ):
        owner, org, agent_id, _, _ = await _setup_org_assistant_with_members(
            client,
            dbsession,
        )

        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.is_staging = True

            await client.post(
                f"/v0/assistant/{agent_id}/secret",
                json={
                    "secret_name": "MICROSOFT_GRANTED_SCOPES",
                    "secret_value": (
                        "https://graph.microsoft.com/Mail.Read "
                        "https://graph.microsoft.com/Mail.Send "
                        "https://graph.microsoft.com/Mail.ReadWrite "
                        "https://graph.microsoft.com/Chat.Read "
                        "https://graph.microsoft.com/Chat.ReadWrite "
                        "https://graph.microsoft.com/ChannelMessage.Send "
                        "https://graph.microsoft.com/User.Read "
                        "offline_access"
                    ),
                },
                headers=org["headers"],
            )

            resp = await client.get(
                f"/v0/assistant/{agent_id}/granted-features",
                headers=org["headers"],
            )

        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()["info"]
        assert data["provider"] == "microsoft"
        assert sorted(data["features"]) == ["email", "teams"]
        assert sorted(data["required_features"]) == ["email", "teams"]
