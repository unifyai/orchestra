"""Tests for WhatsApp pool routing: models, DAO, and admin endpoints.

Covers:
1. WhatsAppPoolNumber seeding and model constraints
2. WhatsAppRoute model constraints (uq_pool_contact)
3. WhatsAppRouteDAO: resolve_inbound (Tier 1 + Tier 2), assign, route, delete
4. Modified uq_active_contact_value (allows WhatsApp sharing)
5. Admin endpoints: resolve, assign, route, delete, pool
6. User.whatsapp_number field (unique partial index)
7. Multi-user / multi-assistant conflict resolution
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchestra.db.dao.assistant_contact_dao import AssistantContactDAO
from orchestra.db.dao.whatsapp_route_dao import WhatsAppRouteDAO
from orchestra.db.models.orchestra_models import (
    Assistant,
    AssistantContact,
    BillingAccount,
    User,
    WhatsAppPoolNumber,
    WhatsAppRoute,
)
from orchestra.tests.utils import ADMIN_HEADERS, create_test_user

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def seed_pool_numbers(dbsession: Session):
    """Seed pool numbers if not already present (migration seeds them)."""
    existing = dbsession.query(WhatsAppPoolNumber).count()
    if existing == 0:
        dbsession.add_all(
            [
                WhatsAppPoolNumber(number="+18507877970"),
                WhatsAppPoolNumber(number="+17343611691"),
            ],
        )
        dbsession.flush()
    yield


@pytest.fixture
def pool_numbers(dbsession: Session) -> list[WhatsAppPoolNumber]:
    return dbsession.query(WhatsAppPoolNumber).order_by(WhatsAppPoolNumber.id).all()


@pytest.fixture
def user_with_whatsapp(dbsession: Session) -> User:
    ba = BillingAccount(credits=100)
    dbsession.add(ba)
    dbsession.flush()
    user = User(
        id=str(uuid.uuid4()),
        email="wa_owner@test.com",
        name="WA",
        last_name="Owner",
        phone_number="+15551234567",
        whatsapp_number="+15551234567",
        billing_account_id=ba.id,
    )
    dbsession.add(user)
    dbsession.flush()
    return user


@pytest.fixture
def assistant_for_user(dbsession: Session, user_with_whatsapp: User) -> Assistant:
    assistant = Assistant(
        user_id=user_with_whatsapp.id,
        first_name="TestBot",
    )
    dbsession.add(assistant)
    dbsession.flush()
    return assistant


@pytest.fixture
def whatsapp_dao(dbsession: Session) -> WhatsAppRouteDAO:
    return WhatsAppRouteDAO(dbsession)


@pytest.fixture
def user_with_phone_only(dbsession: Session) -> User:
    """User who has phone_number set but NOT whatsapp_number."""
    ba = BillingAccount(credits=100)
    dbsession.add(ba)
    dbsession.flush()
    user = User(
        id=str(uuid.uuid4()),
        email="phone_only@test.com",
        name="Phone",
        last_name="Only",
        phone_number="+15559990000",
        whatsapp_number=None,
        billing_account_id=ba.id,
    )
    dbsession.add(user)
    dbsession.flush()
    return user


# ============================================================================
# 1. Pool number seeding
# ============================================================================


class TestPoolNumberModel:
    def test_pool_numbers_seeded(self, pool_numbers):
        assert len(pool_numbers) == 2
        numbers = {p.number for p in pool_numbers}
        assert "+18507877970" in numbers
        assert "+17343611691" in numbers

    def test_pool_number_unique(self, dbsession: Session):
        dbsession.add(WhatsAppPoolNumber(number="+18507877970"))
        with pytest.raises(IntegrityError):
            dbsession.flush()
        dbsession.rollback()

    def test_pool_number_status_constraint(self, dbsession: Session):
        dbsession.add(WhatsAppPoolNumber(number="+10000000000", status="bogus"))
        with pytest.raises(IntegrityError):
            dbsession.flush()
        dbsession.rollback()


# ============================================================================
# 2. WhatsAppRoute model constraints
# ============================================================================


class TestRouteModel:
    def test_uq_pool_contact(
        self,
        dbsession: Session,
        pool_numbers,
        assistant_for_user,
    ):
        """Same (pool_number_id, contact_number) pair cannot exist twice."""
        r1 = WhatsAppRoute(
            pool_number_id=pool_numbers[0].id,
            contact_number="+15559999999",
            assistant_id=assistant_for_user.agent_id,
        )
        dbsession.add(r1)
        dbsession.flush()

        r2 = WhatsAppRoute(
            pool_number_id=pool_numbers[0].id,
            contact_number="+15559999999",
            assistant_id=assistant_for_user.agent_id,
        )
        dbsession.add(r2)
        with pytest.raises(IntegrityError):
            dbsession.flush()
        dbsession.rollback()


# ============================================================================
# 3. WhatsApp contact_value sharing (modified unique constraint)
# ============================================================================


class TestContactValueSharing:
    def test_whatsapp_contacts_can_share_contact_value(
        self,
        dbsession: Session,
        pool_numbers,
        user_with_whatsapp,
    ):
        """Two assistants can have the same contact_value for WhatsApp."""
        a1 = Assistant(user_id=user_with_whatsapp.id, first_name="Bot1")
        a2 = Assistant(user_id=user_with_whatsapp.id, first_name="Bot2")
        dbsession.add_all([a1, a2])
        dbsession.flush()

        c1 = AssistantContact(
            assistant_id=a1.agent_id,
            contact_type="whatsapp",
            contact_value=pool_numbers[0].number,
            status="active",
        )
        c2 = AssistantContact(
            assistant_id=a2.agent_id,
            contact_type="whatsapp",
            contact_value=pool_numbers[0].number,
            status="active",
        )
        dbsession.add_all([c1, c2])
        dbsession.flush()

        # Both should persist without IntegrityError
        assert c1.id is not None
        assert c2.id is not None

    def test_phone_contacts_still_unique(
        self,
        dbsession: Session,
        user_with_whatsapp,
    ):
        """Phone contacts must still have unique contact_value."""
        a1 = Assistant(user_id=user_with_whatsapp.id, first_name="Bot1")
        a2 = Assistant(user_id=user_with_whatsapp.id, first_name="Bot2")
        dbsession.add_all([a1, a2])
        dbsession.flush()

        c1 = AssistantContact(
            assistant_id=a1.agent_id,
            contact_type="phone",
            contact_value="+15550000001",
            status="active",
        )
        dbsession.add(c1)
        dbsession.flush()

        c2 = AssistantContact(
            assistant_id=a2.agent_id,
            contact_type="phone",
            contact_value="+15550000001",
            status="active",
        )
        dbsession.add(c2)
        with pytest.raises(IntegrityError):
            dbsession.flush()
        dbsession.rollback()


# ============================================================================
# 4. WhatsAppRouteDAO: resolve_inbound
# ============================================================================


class TestResolveInbound:
    def test_tier1_user_match(
        self,
        dbsession: Session,
        whatsapp_dao,
        user_with_whatsapp,
        assistant_for_user,
        pool_numbers,
    ):
        """Tier 1: sender matches user.whatsapp_number → route to assistant."""
        # Enable WhatsApp on assistant
        contact = AssistantContact(
            assistant_id=assistant_for_user.agent_id,
            contact_type="whatsapp",
            contact_value=pool_numbers[0].number,
            status="active",
        )
        dbsession.add(contact)
        dbsession.flush()

        result = whatsapp_dao.resolve_inbound(
            pool_numbers[0].number,
            user_with_whatsapp.whatsapp_number,
        )
        assert result is not None
        assert result["assistant_id"] == assistant_for_user.agent_id
        assert result["role"] == "owner"

    def test_tier2_route_match(
        self,
        dbsession: Session,
        whatsapp_dao,
        assistant_for_user,
        pool_numbers,
    ):
        """Tier 2: sender is external contact with a route entry."""
        route = WhatsAppRoute(
            pool_number_id=pool_numbers[0].id,
            contact_number="+15558888888",
            assistant_id=assistant_for_user.agent_id,
        )
        dbsession.add(route)
        dbsession.flush()

        result = whatsapp_dao.resolve_inbound(
            pool_numbers[0].number,
            "+15558888888",
        )
        assert result is not None
        assert result["assistant_id"] == assistant_for_user.agent_id
        assert result["role"] == "contact"

    def test_no_match(self, whatsapp_dao, pool_numbers):
        """Neither tier matches → returns None."""
        result = whatsapp_dao.resolve_inbound(
            pool_numbers[0].number,
            "+15550000000",
        )
        assert result is None

    def test_tier1_takes_priority(
        self,
        dbsession: Session,
        whatsapp_dao,
        user_with_whatsapp,
        pool_numbers,
    ):
        """Tier 1 (user lookup) takes priority over Tier 2 (route table)."""
        a1 = Assistant(user_id=user_with_whatsapp.id, first_name="Bot1")
        a2 = Assistant(user_id=user_with_whatsapp.id, first_name="Bot2")
        dbsession.add_all([a1, a2])
        dbsession.flush()

        # Enable WhatsApp on a1
        dbsession.add(
            AssistantContact(
                assistant_id=a1.agent_id,
                contact_type="whatsapp",
                contact_value=pool_numbers[0].number,
                status="active",
            ),
        )
        # Create route pointing to a2
        dbsession.add(
            WhatsAppRoute(
                pool_number_id=pool_numbers[0].id,
                contact_number=user_with_whatsapp.whatsapp_number,
                assistant_id=a2.agent_id,
            ),
        )
        dbsession.flush()

        result = whatsapp_dao.resolve_inbound(
            pool_numbers[0].number,
            user_with_whatsapp.whatsapp_number,
        )
        # Tier 1 should win → route to a1 (the one with WhatsApp enabled)
        assert result["assistant_id"] == a1.agent_id
        assert result["role"] == "owner"

    def test_tier1b_phone_number_fallback(
        self,
        dbsession: Session,
        whatsapp_dao,
        user_with_phone_only,
        pool_numbers,
    ):
        """Tier 1b: sender matches user.phone_number (no whatsapp_number set)."""
        assistant = Assistant(user_id=user_with_phone_only.id, first_name="PhoneBot")
        dbsession.add(assistant)
        dbsession.flush()

        dbsession.add(
            AssistantContact(
                assistant_id=assistant.agent_id,
                contact_type="whatsapp",
                contact_value=pool_numbers[0].number,
                status="active",
            ),
        )
        dbsession.flush()

        result = whatsapp_dao.resolve_inbound(
            pool_numbers[0].number,
            user_with_phone_only.phone_number,
        )
        assert result is not None
        assert result["assistant_id"] == assistant.agent_id
        assert result["role"] == "owner"

    def test_whatsapp_number_priority_over_phone(
        self,
        dbsession: Session,
        whatsapp_dao,
        pool_numbers,
    ):
        """Tier 1a (whatsapp_number) wins over Tier 1b (phone_number) on a different user."""
        ba1 = BillingAccount(credits=100)
        ba2 = BillingAccount(credits=100)
        dbsession.add_all([ba1, ba2])
        dbsession.flush()

        shared_number = "+15550505050"

        u_wa = User(
            id=str(uuid.uuid4()),
            email="wa_prio@test.com",
            whatsapp_number=shared_number,
            billing_account_id=ba1.id,
        )
        u_ph = User(
            id=str(uuid.uuid4()),
            email="ph_prio@test.com",
            phone_number=shared_number,
            billing_account_id=ba2.id,
        )
        dbsession.add_all([u_wa, u_ph])
        dbsession.flush()

        a_wa = Assistant(user_id=u_wa.id, first_name="WaBot")
        a_ph = Assistant(user_id=u_ph.id, first_name="PhBot")
        dbsession.add_all([a_wa, a_ph])
        dbsession.flush()

        for a in [a_wa, a_ph]:
            dbsession.add(
                AssistantContact(
                    assistant_id=a.agent_id,
                    contact_type="whatsapp",
                    contact_value=pool_numbers[0].number,
                    status="active",
                ),
            )
        dbsession.flush()

        result = whatsapp_dao.resolve_inbound(pool_numbers[0].number, shared_number)
        assert result is not None
        assert result["assistant_id"] == a_wa.agent_id
        assert result["role"] == "owner"

    def test_ambiguous_phone_number_skips_tier1b(
        self,
        dbsession: Session,
        whatsapp_dao,
        pool_numbers,
    ):
        """Two users share the same phone_number → Tier 1b is skipped (ambiguous)."""
        ba1 = BillingAccount(credits=100)
        ba2 = BillingAccount(credits=100)
        dbsession.add_all([ba1, ba2])
        dbsession.flush()

        shared_phone = "+15550707070"
        u1 = User(
            id=str(uuid.uuid4()),
            email="ambig1@test.com",
            phone_number=shared_phone,
            billing_account_id=ba1.id,
        )
        u2 = User(
            id=str(uuid.uuid4()),
            email="ambig2@test.com",
            phone_number=shared_phone,
            billing_account_id=ba2.id,
        )
        dbsession.add_all([u1, u2])
        dbsession.flush()

        a1 = Assistant(user_id=u1.id, first_name="Amb1")
        dbsession.add(a1)
        dbsession.flush()

        dbsession.add(
            AssistantContact(
                assistant_id=a1.agent_id,
                contact_type="whatsapp",
                contact_value=pool_numbers[0].number,
                status="active",
            ),
        )
        dbsession.flush()

        result = whatsapp_dao.resolve_inbound(pool_numbers[0].number, shared_phone)
        assert result is None


# ============================================================================
# 5. WhatsAppRouteDAO: pool assignment
# ============================================================================


class TestPoolAssignment:
    def test_assign_first_available(
        self,
        dbsession: Session,
        whatsapp_dao,
        user_with_whatsapp,
        assistant_for_user,
        pool_numbers,
    ):
        """First eligible pool number is assigned."""
        pool = whatsapp_dao.assign_pool_number(
            assistant_for_user.agent_id,
            [user_with_whatsapp.id],
        )
        assert pool.number in {p.number for p in pool_numbers}

    def test_conflict_avoidance(
        self,
        dbsession: Session,
        whatsapp_dao,
        user_with_whatsapp,
        pool_numbers,
    ):
        """If user already has assistant A on pool1, assistant B avoids pool1."""
        a1 = Assistant(user_id=user_with_whatsapp.id, first_name="Bot1")
        a2 = Assistant(user_id=user_with_whatsapp.id, first_name="Bot2")
        dbsession.add_all([a1, a2])
        dbsession.flush()

        # Enable WhatsApp on a1 using pool1
        dbsession.add(
            AssistantContact(
                assistant_id=a1.agent_id,
                contact_type="whatsapp",
                contact_value=pool_numbers[0].number,
                status="active",
            ),
        )
        dbsession.flush()

        # Assign a2 — should get pool2
        pool = whatsapp_dao.assign_pool_number(
            a2.agent_id,
            [user_with_whatsapp.id],
        )
        assert pool.number == pool_numbers[1].number

    def test_pool_exhaustion(
        self,
        dbsession: Session,
        whatsapp_dao,
        user_with_whatsapp,
        pool_numbers,
    ):
        """When both numbers are taken, raises ValueError."""
        a1 = Assistant(user_id=user_with_whatsapp.id, first_name="Bot1")
        a2 = Assistant(user_id=user_with_whatsapp.id, first_name="Bot2")
        a3 = Assistant(user_id=user_with_whatsapp.id, first_name="Bot3")
        dbsession.add_all([a1, a2, a3])
        dbsession.flush()

        # Assign both pool numbers
        for assistant, pool in zip([a1, a2], pool_numbers):
            dbsession.add(
                AssistantContact(
                    assistant_id=assistant.agent_id,
                    contact_type="whatsapp",
                    contact_value=pool.number,
                    status="active",
                ),
            )
        dbsession.flush()

        with pytest.raises(ValueError, match="currently assigned"):
            whatsapp_dao.assign_pool_number(
                a3.agent_id,
                [user_with_whatsapp.id],
            )

    def test_different_users_can_share_pool(
        self,
        dbsession: Session,
        whatsapp_dao,
        pool_numbers,
    ):
        """Different users' assistants can share the same pool number."""
        ba1 = BillingAccount(credits=100)
        ba2 = BillingAccount(credits=100)
        dbsession.add_all([ba1, ba2])
        dbsession.flush()

        u1 = User(
            id=str(uuid.uuid4()),
            email="u1@test.com",
            whatsapp_number="+15551111111",
            billing_account_id=ba1.id,
        )
        u2 = User(
            id=str(uuid.uuid4()),
            email="u2@test.com",
            whatsapp_number="+15552222222",
            billing_account_id=ba2.id,
        )
        dbsession.add_all([u1, u2])
        dbsession.flush()

        a1 = Assistant(user_id=u1.id, first_name="A1")
        a2 = Assistant(user_id=u2.id, first_name="A2")
        dbsession.add_all([a1, a2])
        dbsession.flush()

        # Both can get pool1
        p1 = whatsapp_dao.assign_pool_number(a1.agent_id, [u1.id])
        assert p1.number == pool_numbers[0].number

        # Enable WhatsApp on a1
        dbsession.add(
            AssistantContact(
                assistant_id=a1.agent_id,
                contact_type="whatsapp",
                contact_value=p1.number,
                status="active",
            ),
        )
        dbsession.flush()

        # u2's assistant should also get pool1 (no conflict)
        p2 = whatsapp_dao.assign_pool_number(a2.agent_id, [u2.id])
        assert p2.number == pool_numbers[0].number


# ============================================================================
# 6. WhatsAppRouteDAO: external contact routes
# ============================================================================


class TestExternalRoutes:
    def test_get_or_create_route(
        self,
        dbsession: Session,
        whatsapp_dao,
        assistant_for_user,
        pool_numbers,
    ):
        """Creates a route for outbound and retrieves it on second call."""
        dbsession.add(
            AssistantContact(
                assistant_id=assistant_for_user.agent_id,
                contact_type="whatsapp",
                contact_value=pool_numbers[0].number,
                status="active",
            ),
        )
        dbsession.flush()

        route = whatsapp_dao.get_or_create_route(
            assistant_for_user.agent_id,
            "+15557777777",
        )
        assert route.contact_number == "+15557777777"
        assert route.pool_number.number == pool_numbers[0].number

        # Second call returns the same route
        route2 = whatsapp_dao.get_or_create_route(
            assistant_for_user.agent_id,
            "+15557777777",
        )
        assert route2.id == route.id

    def test_route_conflict(
        self,
        dbsession: Session,
        whatsapp_dao,
        user_with_whatsapp,
        pool_numbers,
    ):
        """Two assistants on same pool can't route to the same contact."""
        a1 = Assistant(user_id=user_with_whatsapp.id, first_name="Bot1")
        a2 = Assistant(user_id=user_with_whatsapp.id, first_name="Bot2")
        dbsession.add_all([a1, a2])
        dbsession.flush()

        for a in [a1, a2]:
            dbsession.add(
                AssistantContact(
                    assistant_id=a.agent_id,
                    contact_type="whatsapp",
                    contact_value=pool_numbers[0].number,
                    status="active",
                ),
            )
        dbsession.flush()

        whatsapp_dao.get_or_create_route(a1.agent_id, "+15556666666")

        with pytest.raises(ValueError, match="already routed"):
            whatsapp_dao.get_or_create_route(a2.agent_id, "+15556666666")

    def test_delete_routes_for_assistant(
        self,
        dbsession: Session,
        whatsapp_dao,
        assistant_for_user,
        pool_numbers,
    ):
        """Bulk-deleting routes frees all pairs."""
        dbsession.add(
            AssistantContact(
                assistant_id=assistant_for_user.agent_id,
                contact_type="whatsapp",
                contact_value=pool_numbers[0].number,
                status="active",
            ),
        )
        dbsession.flush()

        whatsapp_dao.get_or_create_route(assistant_for_user.agent_id, "+15551000001")
        whatsapp_dao.get_or_create_route(assistant_for_user.agent_id, "+15551000002")
        dbsession.flush()

        count = whatsapp_dao.delete_routes_for_assistant(
            assistant_for_user.agent_id,
        )
        assert count == 2

        remaining = whatsapp_dao.get_routes_for_assistant(
            assistant_for_user.agent_id,
        )
        assert len(remaining) == 0


# ============================================================================
# 7. User.whatsapp_number
# ============================================================================


class TestUserWhatsappNumber:
    def test_unique_partial_index(self, dbsession: Session):
        """Two users can't have the same non-null whatsapp_number."""
        ba1 = BillingAccount(credits=0)
        ba2 = BillingAccount(credits=0)
        dbsession.add_all([ba1, ba2])
        dbsession.flush()

        u1 = User(
            id=str(uuid.uuid4()),
            email="dup1@test.com",
            whatsapp_number="+15553333333",
            billing_account_id=ba1.id,
        )
        u2 = User(
            id=str(uuid.uuid4()),
            email="dup2@test.com",
            whatsapp_number="+15553333333",
            billing_account_id=ba2.id,
        )
        dbsession.add_all([u1, u2])
        with pytest.raises(IntegrityError):
            dbsession.flush()
        dbsession.rollback()

    def test_null_whatsapp_number_allowed(self, dbsession: Session):
        """Multiple users can have NULL whatsapp_number."""
        ba1 = BillingAccount(credits=0)
        ba2 = BillingAccount(credits=0)
        dbsession.add_all([ba1, ba2])
        dbsession.flush()

        u1 = User(
            id=str(uuid.uuid4()),
            email="null1@test.com",
            whatsapp_number=None,
            billing_account_id=ba1.id,
        )
        u2 = User(
            id=str(uuid.uuid4()),
            email="null2@test.com",
            whatsapp_number=None,
            billing_account_id=ba2.id,
        )
        dbsession.add_all([u1, u2])
        dbsession.flush()
        assert u1.id is not None
        assert u2.id is not None


# ============================================================================
# 8. Admin endpoints
# ============================================================================


class TestAdminEndpoints:
    @pytest.fixture
    async def test_user(self, client: AsyncClient):
        return await create_test_user(client, "wa_api_test@test.com")

    @pytest.fixture
    async def test_assistant(
        self,
        test_user,
        dbsession: Session,
    ):
        """Create an assistant directly in the DB (avoids unmocked infra calls)."""
        assistant = Assistant(user_id=test_user["id"], first_name="WA")
        dbsession.add(assistant)
        dbsession.commit()
        return {"agent_id": assistant.agent_id}

    async def test_pool_endpoint(self, client: AsyncClient):
        response = await client.get(
            "/v0/admin/whatsapp/pool",
            headers=ADMIN_HEADERS,
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data) == 2
        numbers = {p["number"] for p in data}
        assert "+18507877970" in numbers
        assert "+17343611691" in numbers

    async def test_resolve_unknown_sender(self, client: AsyncClient):
        response = await client.get(
            "/v0/admin/whatsapp/resolve",
            params={"pool_number": "+18507877970", "sender": "+15550000000"},
            headers=ADMIN_HEADERS,
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND

    async def test_assign_and_resolve_flow(
        self,
        client: AsyncClient,
        test_user,
        test_assistant,
        dbsession: Session,
    ):
        """Full flow: assign pool number → set user's whatsapp → resolve."""
        assistant_id = test_assistant["agent_id"]

        # Assign pool number
        assign_resp = await client.post(
            "/v0/admin/whatsapp/assign",
            json={"assistant_id": assistant_id},
            headers=ADMIN_HEADERS,
        )
        assert assign_resp.status_code == status.HTTP_200_OK, assign_resp.json()
        pool_number = assign_resp.json()["pool_number"]

        # Create the WhatsApp contact row (simulating what the full endpoint does)
        contact_dao = AssistantContactDAO(dbsession)
        contact_dao.upsert_assistant_contact(
            assistant_id=assistant_id,
            contact_type="whatsapp",
            contact_value=pool_number,
        )
        dbsession.commit()

        # Set user's whatsapp_number
        user_wa = "+16505551234"
        update_resp = await client.put(
            "/v0/admin/user",
            json={
                "user_id": test_user["id"],
                "whatsapp_number": user_wa,
            },
            headers=ADMIN_HEADERS,
        )
        assert update_resp.status_code == status.HTTP_200_OK, update_resp.text

        # Resolve inbound
        resolve_resp = await client.get(
            "/v0/admin/whatsapp/resolve",
            params={"pool_number": pool_number, "sender": user_wa},
            headers=ADMIN_HEADERS,
        )
        assert resolve_resp.status_code == status.HTTP_200_OK
        assert resolve_resp.json()["assistant_id"] == assistant_id
        assert resolve_resp.json()["role"] == "owner"

    async def test_route_and_resolve_external(
        self,
        client: AsyncClient,
        test_assistant,
        dbsession: Session,
    ):
        """Create an outbound route → resolve inbound reply."""
        assistant_id = test_assistant["agent_id"]

        # First assign a pool number and create the WhatsApp contact
        assign_resp = await client.post(
            "/v0/admin/whatsapp/assign",
            json={"assistant_id": assistant_id},
            headers=ADMIN_HEADERS,
        )
        pool_number = assign_resp.json()["pool_number"]

        contact_dao = AssistantContactDAO(dbsession)
        contact_dao.upsert_assistant_contact(
            assistant_id=assistant_id,
            contact_type="whatsapp",
            contact_value=pool_number,
        )
        dbsession.commit()

        # Create outbound route
        external = "+15554444444"
        route_resp = await client.post(
            "/v0/admin/whatsapp/route",
            json={
                "assistant_id": assistant_id,
                "contact_number": external,
            },
            headers=ADMIN_HEADERS,
        )
        assert route_resp.status_code == status.HTTP_200_OK
        assert route_resp.json()["pool_number"] == pool_number

        # Resolve inbound reply
        resolve_resp = await client.get(
            "/v0/admin/whatsapp/resolve",
            params={"pool_number": pool_number, "sender": external},
            headers=ADMIN_HEADERS,
        )
        assert resolve_resp.status_code == status.HTTP_200_OK
        assert resolve_resp.json()["assistant_id"] == assistant_id
        assert resolve_resp.json()["role"] == "contact"

    async def test_delete_routes_endpoint(
        self,
        client: AsyncClient,
        test_assistant,
        dbsession: Session,
    ):
        """Delete routes → resolve returns 404."""
        assistant_id = test_assistant["agent_id"]

        # Assign and create contact
        assign_resp = await client.post(
            "/v0/admin/whatsapp/assign",
            json={"assistant_id": assistant_id},
            headers=ADMIN_HEADERS,
        )
        pool_number = assign_resp.json()["pool_number"]

        contact_dao = AssistantContactDAO(dbsession)
        contact_dao.upsert_assistant_contact(
            assistant_id=assistant_id,
            contact_type="whatsapp",
            contact_value=pool_number,
        )
        dbsession.commit()

        # Create route
        external = "+15553333333"
        await client.post(
            "/v0/admin/whatsapp/route",
            json={"assistant_id": assistant_id, "contact_number": external},
            headers=ADMIN_HEADERS,
        )

        # Delete routes
        del_resp = await client.delete(
            f"/v0/admin/whatsapp/routes?assistant_id={assistant_id}",
            headers=ADMIN_HEADERS,
        )
        assert del_resp.status_code == status.HTTP_200_OK
        assert del_resp.json()["deleted"] == 1

        # Resolve should now 404
        resolve_resp = await client.get(
            "/v0/admin/whatsapp/resolve",
            params={"pool_number": pool_number, "sender": external},
            headers=ADMIN_HEADERS,
        )
        assert resolve_resp.status_code == status.HTTP_404_NOT_FOUND


# ============================================================================
# 9. User whatsapp_number via API
# ============================================================================


class TestUserWhatsappAPI:
    async def test_create_user_with_whatsapp(self, client: AsyncClient):
        resp = await client.post(
            "/v0/admin/user",
            json={
                "email": "wa_create@test.com",
                "name": "Test",
                "whatsapp_number": "+16502530001",
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        assert resp.json()["whatsapp_number"] == "+16502530001"

    async def test_update_user_whatsapp(self, client: AsyncClient):
        user = await create_test_user(client, "wa_update@test.com")
        resp = await client.put(
            "/v0/admin/user",
            json={
                "user_id": user["id"],
                "whatsapp_number": "+16502530002",
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK, resp.json()

        # Verify via basic-info
        info_resp = await client.get(
            "/v0/user/basic-info",
            headers=user["headers"],
        )
        assert info_resp.status_code == status.HTTP_200_OK
        assert info_resp.json()["whatsapp_number"] == "+16502530002"

    async def test_whatsapp_number_in_user_lookup(self, client: AsyncClient):
        resp = await client.post(
            "/v0/admin/user",
            json={
                "email": "wa_lookup@test.com",
                "name": "Lookup",
                "whatsapp_number": "+16502530003",
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        user_id = resp.json()["id"]

        detail_resp = await client.get(
            f"/v0/admin/user/by-user-id?user_id={user_id}",
            headers=ADMIN_HEADERS,
        )
        assert detail_resp.status_code == status.HTTP_200_OK
        assert detail_resp.json()["whatsapp_number"] == "+16502530003"


# ============================================================================
# 10. Pool number CRUD via admin endpoints
# ============================================================================


class TestPoolNumberCRUD:
    async def test_add_pool_number(self, client: AsyncClient):
        """POST /admin/whatsapp/pool creates a new pool number."""
        resp = await client.post(
            "/v0/admin/whatsapp/pool",
            json={"number": "+15550001111"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["number"] == "+15550001111"
        assert data["status"] == "active"
        assert data["id"] is not None

        # Verify it shows up in the pool list
        pool_resp = await client.get(
            "/v0/admin/whatsapp/pool",
            headers=ADMIN_HEADERS,
        )
        numbers = {p["number"] for p in pool_resp.json()}
        assert "+15550001111" in numbers

    async def test_add_duplicate_pool_number(self, client: AsyncClient, pool_numbers):
        """POST with an existing number returns 409."""
        resp = await client.post(
            "/v0/admin/whatsapp/pool",
            json={"number": pool_numbers[0].number},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_409_CONFLICT
        assert "already exists" in resp.json()["detail"]

    async def test_add_pool_number_with_sid(self, client: AsyncClient):
        """POST with twilio_sender_sid stores it."""
        resp = await client.post(
            "/v0/admin/whatsapp/pool",
            json={
                "number": "+15550002222",
                "twilio_sender_sid": "MG_test_sid_123",
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["twilio_sender_sid"] == "MG_test_sid_123"

    async def test_update_pool_number_status(
        self,
        client: AsyncClient,
        pool_numbers,
    ):
        """PATCH updates the status field."""
        pool_id = pool_numbers[0].id
        resp = await client.patch(
            f"/v0/admin/whatsapp/pool/{pool_id}",
            json={"status": "inactive"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["status"] == "inactive"

    async def test_update_pool_number_sid(
        self,
        client: AsyncClient,
        pool_numbers,
    ):
        """PATCH updates the twilio_sender_sid field."""
        pool_id = pool_numbers[1].id
        resp = await client.patch(
            f"/v0/admin/whatsapp/pool/{pool_id}",
            json={"twilio_sender_sid": "MG_updated_456"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["twilio_sender_sid"] == "MG_updated_456"

    async def test_update_nonexistent_pool_number(self, client: AsyncClient):
        """PATCH on invalid ID returns 404."""
        resp = await client.patch(
            "/v0/admin/whatsapp/pool/999999",
            json={"status": "inactive"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    async def test_delete_unused_pool_number(
        self,
        client: AsyncClient,
    ):
        """DELETE removes a pool number not in use by any assistant."""
        # Add a number specifically for deletion
        add_resp = await client.post(
            "/v0/admin/whatsapp/pool",
            json={"number": "+15550003333"},
            headers=ADMIN_HEADERS,
        )
        pool_id = add_resp.json()["id"]

        del_resp = await client.delete(
            f"/v0/admin/whatsapp/pool/{pool_id}",
            headers=ADMIN_HEADERS,
        )
        assert del_resp.status_code == status.HTTP_200_OK
        assert del_resp.json()["deleted_routes"] == 0

        # Verify it's gone
        pool_resp = await client.get(
            "/v0/admin/whatsapp/pool",
            headers=ADMIN_HEADERS,
        )
        ids = {p["id"] for p in pool_resp.json()}
        assert pool_id not in ids

    async def test_delete_pool_number_in_use(
        self,
        client: AsyncClient,
        dbsession: Session,
        pool_numbers,
        user_with_whatsapp,
    ):
        """DELETE on a number with active contacts returns 400."""
        assistant = Assistant(user_id=user_with_whatsapp.id, first_name="InUse")
        dbsession.add(assistant)
        dbsession.flush()

        dbsession.add(
            AssistantContact(
                assistant_id=assistant.agent_id,
                contact_type="whatsapp",
                contact_value=pool_numbers[0].number,
                status="active",
            ),
        )
        dbsession.commit()

        resp = await client.delete(
            f"/v0/admin/whatsapp/pool/{pool_numbers[0].id}",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert "active assistant" in resp.json()["detail"]
