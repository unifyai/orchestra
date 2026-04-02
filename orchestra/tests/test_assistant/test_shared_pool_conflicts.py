"""Tests for shared-pool routing and conflict resolution.

Groups:
A. Core conflict detection (DAO)
B. Reassignment logic (DAO)
C. Stale numbers & cold messages
D. Notifications (integration, mock Communication)
E. Org integration (endpoint)
F. Platform agnosticism
G. 24h window + reassignment
H. Admin endpoints (conflict-specific)
I. Model constraints (pool, route, contact sharing)
J. Tier 1 resolve inbound (owner matching, priority)
K. Pool assignment (DAO, first available, sharing, exhaustion)
L. Route management (deletion, idempotent creation)
M. User.whatsapp_number (model + API)
N. Pool Number CRUD endpoints
O. 24h window tracking (DAO + endpoint level)
P. Admin endpoint flows (assign → resolve, route → resolve, delete)
Q. General success paths (happy-path flows without conflicts)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchestra.db.dao.assistant_contact_dao import AssistantContactDAO
from orchestra.db.dao.shared_pool_dao import SharedPoolDAO
from orchestra.db.models.orchestra_models import (
    Assistant,
    AssistantContact,
    BillingAccount,
    ConflictEvent,
    DecommissionedRoute,
    Organization,
    OrganizationMember,
    Role,
    SharedPlatformRoute,
    SharedPoolNumber,
    User,
)
from orchestra.tests.utils import ADMIN_HEADERS, create_test_user

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def seed_pool_numbers(dbsession: Session):
    """Seed pool numbers if not already present."""
    existing = dbsession.query(SharedPoolNumber).count()
    if existing == 0:
        dbsession.add_all(
            [
                SharedPoolNumber(number="+18507877970", platform="whatsapp"),
                SharedPoolNumber(number="+17343611691", platform="whatsapp"),
            ],
        )
        dbsession.flush()
    yield


@pytest.fixture
def pool_numbers(dbsession: Session) -> list[SharedPoolNumber]:
    return (
        dbsession.query(SharedPoolNumber)
        .filter(SharedPoolNumber.platform == "whatsapp")
        .order_by(SharedPoolNumber.id)
        .all()
    )


@pytest.fixture
def dao(dbsession: Session) -> SharedPoolDAO:
    return SharedPoolDAO(dbsession)


def _make_user(
    dbsession: Session,
    email: str,
    whatsapp_number: str | None = None,
    name: str = "Test",
) -> User:
    ba = BillingAccount(credits=100)
    dbsession.add(ba)
    dbsession.flush()
    user = User(
        id=str(uuid.uuid4()),
        email=email,
        name=name,
        whatsapp_number=whatsapp_number,
        billing_account_id=ba.id,
    )
    dbsession.add(user)
    dbsession.flush()
    return user


def _make_assistant(
    dbsession: Session,
    user: User,
    name: str = "Bot",
    org_id: int | None = None,
) -> Assistant:
    assistant = Assistant(
        user_id=user.id,
        first_name=name,
        organization_id=org_id,
    )
    dbsession.add(assistant)
    dbsession.flush()
    return assistant


def _enable_whatsapp(
    dbsession: Session,
    assistant: Assistant,
    pool_number: SharedPoolNumber,
) -> AssistantContact:
    contact = AssistantContact(
        assistant_id=assistant.agent_id,
        contact_type="whatsapp",
        contact_value=pool_number.number,
        status="active",
    )
    dbsession.add(contact)
    dbsession.flush()
    return contact


def _make_org(
    dbsession: Session,
    owner: User,
    name: str = "TestOrg",
) -> Organization:
    org_ba = BillingAccount(credits=100)
    dbsession.add(org_ba)
    dbsession.flush()
    org = Organization(
        owner_id=owner.id,
        name=name,
        billing_account_id=org_ba.id,
    )
    dbsession.add(org)
    dbsession.flush()
    # Ensure a "Member" system role exists (normally seeded by migrations)
    member_role = (
        dbsession.query(Role)
        .filter(Role.name == "Member", Role.organization_id.is_(None))
        .first()
    )
    if not member_role:
        member_role = Role(name="Member", description="Member role")
        dbsession.add(member_role)
        dbsession.flush()
    # Add owner as org member
    dbsession.add(
        OrganizationMember(
            organization_id=org.id,
            user_id=owner.id,
            role_id=member_role.id,
        ),
    )
    dbsession.flush()
    return org


def _add_org_member(dbsession: Session, org: Organization, user: User) -> None:
    member_role = (
        dbsession.query(Role)
        .filter(Role.name == "Member", Role.organization_id.is_(None))
        .first()
    )
    dbsession.add(
        OrganizationMember(
            organization_id=org.id,
            user_id=user.id,
            role_id=member_role.id,
        ),
    )
    dbsession.flush()


# ============================================================================
# Group A: Core Conflict Detection
# ============================================================================


class TestCase1ContactOverlap:
    def test_conflict_detected_same_pool_same_contact(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        """Two assistants on same pool, second tries to route to same contact → conflict resolved."""
        u1 = _make_user(dbsession, "overlap1@test.com", "+15550001111")
        u2 = _make_user(dbsession, "overlap2@test.com", "+15550002222")
        a1 = _make_assistant(dbsession, u1, "Bot1")
        a2 = _make_assistant(dbsession, u2, "Bot2")
        _enable_whatsapp(dbsession, a1, pool_numbers[0])
        _enable_whatsapp(dbsession, a2, pool_numbers[0])

        # a1 routes to external contact
        route1, res1 = dao.get_or_create_route(a1.agent_id, "+15559999999")
        assert res1 is None
        assert route1.pool_number.number == pool_numbers[0].number

        # a2 tries same contact → conflict, resolved inline
        route2, res2 = dao.get_or_create_route(a2.agent_id, "+15559999999")
        assert res2 is not None
        assert res2.conflict_type == "contact_overlap"
        assert a2.agent_id in res2.affected_assistant_ids
        # a2 should now be on a different pool number
        assert route2.pool_number.number == pool_numbers[1].number

    def test_no_conflict_different_pools(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        """Two assistants on different pools routing to same contact → no conflict."""
        u1 = _make_user(dbsession, "diff1@test.com", "+15550003333")
        u2 = _make_user(dbsession, "diff2@test.com", "+15550004444")
        a1 = _make_assistant(dbsession, u1, "Bot1")
        a2 = _make_assistant(dbsession, u2, "Bot2")
        _enable_whatsapp(dbsession, a1, pool_numbers[0])
        _enable_whatsapp(dbsession, a2, pool_numbers[1])

        route1, res1 = dao.get_or_create_route(a1.agent_id, "+15559999999")
        assert res1 is None
        route2, res2 = dao.get_or_create_route(a2.agent_id, "+15559999999")
        assert res2 is None
        assert route1.pool_number.number != route2.pool_number.number

    def test_no_conflict_same_assistant_re_routes(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        """Same assistant routing to same contact twice → returns existing route."""
        u1 = _make_user(dbsession, "reuse@test.com", "+15550005555")
        a1 = _make_assistant(dbsession, u1, "Bot1")
        _enable_whatsapp(dbsession, a1, pool_numbers[0])

        route1, res1 = dao.get_or_create_route(a1.agent_id, "+15559999999")
        route2, res2 = dao.get_or_create_route(a1.agent_id, "+15559999999")
        assert res1 is None
        assert res2 is None
        assert route1.id == route2.id

    def test_conflict_resolved_inline_returns_new_route(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        """After conflict resolution, the route is created on the new pool."""
        u1 = _make_user(dbsession, "inline1@test.com", "+15550006666")
        u2 = _make_user(dbsession, "inline2@test.com", "+15550007777")
        a1 = _make_assistant(dbsession, u1, "Bot1")
        a2 = _make_assistant(dbsession, u2, "Bot2")
        _enable_whatsapp(dbsession, a1, pool_numbers[0])
        _enable_whatsapp(dbsession, a2, pool_numbers[0])

        dao.get_or_create_route(a1.agent_id, "+15558888888")
        route2, res2 = dao.get_or_create_route(a2.agent_id, "+15558888888")

        assert res2 is not None
        # The returned route should be on the new pool
        assert route2.pool_number_id != pool_numbers[0].id
        # The assistant contact should be updated
        dbsession.refresh(a2)
        wa_contact = (
            dbsession.query(AssistantContact)
            .filter(
                AssistantContact.assistant_id == a2.agent_id,
                AssistantContact.contact_type == "whatsapp",
                AssistantContact.status == "active",
            )
            .first()
        )
        assert wa_contact.contact_value == pool_numbers[1].number


class TestCase2UserToUser:
    def test_conflict_detected_target_is_platform_user_same_pool(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        """User A messages User B (both platform users, same pool) → both reassigned."""
        # Need a third pool number for this test
        pool3 = SharedPoolNumber(number="+15550000003", platform="whatsapp")
        dbsession.add(pool3)
        dbsession.flush()

        u_a = _make_user(dbsession, "ua@test.com", "+15550010001", name="UserA")
        u_b = _make_user(dbsession, "ub@test.com", "+15550010002", name="UserB")
        a_a = _make_assistant(dbsession, u_a, "BotA")
        a_b = _make_assistant(dbsession, u_b, "BotB")
        _enable_whatsapp(dbsession, a_a, pool_numbers[0])
        _enable_whatsapp(dbsession, a_b, pool_numbers[0])

        # Create initial route for a_b so there's a (pool, u_b.whatsapp_number) pair
        dbsession.add(
            SharedPlatformRoute(
                pool_number_id=pool_numbers[0].id,
                contact_number=u_b.whatsapp_number,
                assistant_id=a_b.agent_id,
            ),
        )
        dbsession.flush()

        # a_a tries to message u_b (who is a platform user)
        route, res = dao.get_or_create_route(a_a.agent_id, u_b.whatsapp_number)
        assert res is not None
        assert res.conflict_type == "user_to_user"
        assert set(res.affected_assistant_ids) == {a_a.agent_id, a_b.agent_id}
        # Both should be on new unique numbers
        assert res.new_pool_assignments[a_a.agent_id] != pool_numbers[0].number
        assert res.new_pool_assignments[a_b.agent_id] != pool_numbers[0].number
        assert (
            res.new_pool_assignments[a_a.agent_id]
            != res.new_pool_assignments[a_b.agent_id]
        )

    def test_no_conflict_target_is_platform_user_different_pool(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        """User A messages User B, different pools → no conflict."""
        u_a = _make_user(dbsession, "diffpool_a@test.com", "+15550020001")
        u_b = _make_user(dbsession, "diffpool_b@test.com", "+15550020002")
        a_a = _make_assistant(dbsession, u_a, "BotA")
        a_b = _make_assistant(dbsession, u_b, "BotB")
        _enable_whatsapp(dbsession, a_a, pool_numbers[0])
        _enable_whatsapp(dbsession, a_b, pool_numbers[1])

        route, res = dao.get_or_create_route(a_a.agent_id, u_b.whatsapp_number)
        assert res is None

    def test_no_conflict_target_is_external_contact(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        """Messaging an external contact (not platform user) → Case 1, not Case 2."""
        u = _make_user(dbsession, "ext@test.com", "+15550030001")
        a1 = _make_assistant(dbsession, u, "Bot1")
        a2 = _make_assistant(dbsession, u, "Bot2")
        _enable_whatsapp(dbsession, a1, pool_numbers[0])
        _enable_whatsapp(dbsession, a2, pool_numbers[0])

        dao.get_or_create_route(a1.agent_id, "+15559999999")
        _, res = dao.get_or_create_route(a2.agent_id, "+15559999999")
        # External contact → Case 1, not Case 2
        assert res is not None
        assert res.conflict_type == "contact_overlap"


class TestCase3OrgMembership:
    def test_join_creates_conflict_personal_vs_org_assistant(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        """User with personal assistant on pool_1 joins org with assistant on pool_1."""
        owner = _make_user(dbsession, "org_owner@test.com", "+15550040001")
        joiner = _make_user(dbsession, "joiner@test.com", "+15550040002")
        org = _make_org(dbsession, owner, "ConflictOrg")

        # Org assistant on pool_1
        org_assistant = _make_assistant(dbsession, owner, "OrgBot", org.id)
        _enable_whatsapp(dbsession, org_assistant, pool_numbers[0])

        # Personal assistant on pool_1
        personal_assistant = _make_assistant(dbsession, joiner, "PersonalBot")
        _enable_whatsapp(dbsession, personal_assistant, pool_numbers[0])

        # Joiner joins the org
        _add_org_member(dbsession, org, joiner)

        conflicts = dao.detect_membership_conflicts(joiner.id, org.id)
        assert len(conflicts) == 1
        personal_aid, org_aid, pool = conflicts[0]
        assert personal_aid == personal_assistant.agent_id
        assert org_aid == org_assistant.agent_id

    def test_join_no_conflict_different_pools(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        """Personal and org assistants on different pools → no conflict."""
        owner = _make_user(dbsession, "org_owner2@test.com", "+15550050001")
        joiner = _make_user(dbsession, "joiner2@test.com", "+15550050002")
        org = _make_org(dbsession, owner, "NoConflictOrg")

        org_assistant = _make_assistant(dbsession, owner, "OrgBot", org.id)
        _enable_whatsapp(dbsession, org_assistant, pool_numbers[0])

        personal_assistant = _make_assistant(dbsession, joiner, "PersonalBot")
        _enable_whatsapp(dbsession, personal_assistant, pool_numbers[1])

        _add_org_member(dbsession, org, joiner)

        conflicts = dao.detect_membership_conflicts(joiner.id, org.id)
        assert len(conflicts) == 0

    def test_join_multi_org_cross_contamination(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        """User in two orgs, both assistants on same pool → conflict on second join."""
        owner1 = _make_user(dbsession, "owner1@test.com", "+15550060001")
        owner2 = _make_user(dbsession, "owner2@test.com", "+15550060002")
        joiner = _make_user(dbsession, "multi_joiner@test.com", "+15550060003")

        org1 = _make_org(dbsession, owner1, "Org1")
        org2 = _make_org(dbsession, owner2, "Org2")

        org1_assistant = _make_assistant(dbsession, owner1, "Org1Bot", org1.id)
        org2_assistant = _make_assistant(dbsession, owner2, "Org2Bot", org2.id)
        _enable_whatsapp(dbsession, org1_assistant, pool_numbers[0])
        _enable_whatsapp(dbsession, org2_assistant, pool_numbers[0])

        # Personal assistant also on pool_1
        personal = _make_assistant(dbsession, joiner, "PersonalBot")
        _enable_whatsapp(dbsession, personal, pool_numbers[0])

        _add_org_member(dbsession, org1, joiner)
        _add_org_member(dbsession, org2, joiner)

        # Both orgs now conflict with personal assistant
        conflicts1 = dao.detect_membership_conflicts(joiner.id, org1.id)
        conflicts2 = dao.detect_membership_conflicts(joiner.id, org2.id)
        assert len(conflicts1) >= 1
        assert len(conflicts2) >= 1

    def test_leave_no_new_conflict_created(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        """Leaving an org cannot create new conflicts."""
        owner = _make_user(dbsession, "leave_owner@test.com", "+15550070001")
        leaver = _make_user(dbsession, "leaver@test.com", "+15550070002")
        org = _make_org(dbsession, owner, "LeaveOrg")
        _add_org_member(dbsession, org, leaver)

        org_assistant = _make_assistant(dbsession, owner, "OrgBot", org.id)
        _enable_whatsapp(dbsession, org_assistant, pool_numbers[0])

        # After leaving, detect_membership_conflicts should find nothing
        # (the method detects conflicts for joining, not leaving)
        conflicts = dao.detect_membership_conflicts(leaver.id, org.id)
        # No personal assistant → no conflict
        assert len(conflicts) == 0

    def test_leave_cleans_up_orphaned_tier1_routes(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        """Routes for the leaver's WA number pointing to org assistants get cleaned up."""
        owner = _make_user(dbsession, "cleanup_owner@test.com", "+15550080001")
        leaver = _make_user(
            dbsession,
            "cleanup_leaver@test.com",
            "+15550080002",
        )
        org = _make_org(dbsession, owner, "CleanupOrg")
        _add_org_member(dbsession, org, leaver)

        org_assistant = _make_assistant(dbsession, owner, "OrgBot", org.id)
        _enable_whatsapp(dbsession, org_assistant, pool_numbers[0])

        # Simulate a route that was created when the leaver messaged the org assistant
        dbsession.add(
            SharedPlatformRoute(
                pool_number_id=pool_numbers[0].id,
                contact_number=leaver.whatsapp_number,
                assistant_id=org_assistant.agent_id,
            ),
        )
        dbsession.flush()

        count = dao.cleanup_departed_member_routes(leaver.id, org.id)
        assert count == 1


# ============================================================================
# Group B: Reassignment Logic
# ============================================================================


class TestCase1Reassignment:
    def test_initiator_gets_new_pool_number(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        u1 = _make_user(dbsession, "init1@test.com", "+15550100001")
        u2 = _make_user(dbsession, "init2@test.com", "+15550100002")
        a1 = _make_assistant(dbsession, u1, "Inc")
        a2 = _make_assistant(dbsession, u2, "Init")
        _enable_whatsapp(dbsession, a1, pool_numbers[0])
        _enable_whatsapp(dbsession, a2, pool_numbers[0])

        dao.get_or_create_route(a1.agent_id, "+15559000001")
        _, res = dao.get_or_create_route(a2.agent_id, "+15559000001")

        assert res is not None
        assert res.new_pool_assignments[a2.agent_id] == pool_numbers[1].number

    def test_incumbent_keeps_pool_number(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        u1 = _make_user(dbsession, "inc1@test.com", "+15550110001")
        u2 = _make_user(dbsession, "inc2@test.com", "+15550110002")
        a1 = _make_assistant(dbsession, u1, "Inc")
        a2 = _make_assistant(dbsession, u2, "Init")
        _enable_whatsapp(dbsession, a1, pool_numbers[0])
        _enable_whatsapp(dbsession, a2, pool_numbers[0])

        dao.get_or_create_route(a1.agent_id, "+15559000002")
        dao.get_or_create_route(a2.agent_id, "+15559000002")

        # a1 should still be on pool_numbers[0]
        a1_contact = (
            dbsession.query(AssistantContact)
            .filter(
                AssistantContact.assistant_id == a1.agent_id,
                AssistantContact.contact_type == "whatsapp",
                AssistantContact.status == "active",
            )
            .first()
        )
        assert a1_contact.contact_value == pool_numbers[0].number

    def test_existing_routes_migrated_to_new_pool(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        u1 = _make_user(dbsession, "mig1@test.com", "+15550120001")
        u2 = _make_user(dbsession, "mig2@test.com", "+15550120002")
        a1 = _make_assistant(dbsession, u1, "Inc")
        a2 = _make_assistant(dbsession, u2, "Init")
        _enable_whatsapp(dbsession, a1, pool_numbers[0])
        _enable_whatsapp(dbsession, a2, pool_numbers[0])

        # a2 has a pre-existing route
        dao.get_or_create_route(a2.agent_id, "+15559000003")

        # Now conflict triggers
        dao.get_or_create_route(a1.agent_id, "+15559000004")
        dao.get_or_create_route(a2.agent_id, "+15559000004")

        # a2's old route should now be on the new pool
        old_route = (
            dbsession.query(SharedPlatformRoute)
            .filter(
                SharedPlatformRoute.assistant_id == a2.agent_id,
                SharedPlatformRoute.contact_number == "+15559000003",
            )
            .first()
        )
        assert old_route.pool_number.number == pool_numbers[1].number

    def test_decommissioned_route_created_for_old_pairs(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        u1 = _make_user(dbsession, "dec1@test.com", "+15550130001")
        u2 = _make_user(dbsession, "dec2@test.com", "+15550130002")
        a1 = _make_assistant(dbsession, u1, "Inc")
        a2 = _make_assistant(dbsession, u2, "Init")
        _enable_whatsapp(dbsession, a1, pool_numbers[0])
        _enable_whatsapp(dbsession, a2, pool_numbers[0])

        dao.get_or_create_route(a2.agent_id, "+15559000005")
        dao.get_or_create_route(a1.agent_id, "+15559000006")
        dao.get_or_create_route(a2.agent_id, "+15559000006")

        # Should have a decommissioned route for the migrated pair
        decomm = (
            dbsession.query(DecommissionedRoute)
            .filter(
                DecommissionedRoute.old_assistant_id == a2.agent_id,
                DecommissionedRoute.contact_identifier == "+15559000005",
            )
            .first()
        )
        assert decomm is not None
        assert decomm.pool_number_id == pool_numbers[0].id

    def test_reassignment_fails_if_pool_exhausted(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        """Only 2 pool numbers, both occupied → conflict can't resolve."""
        u1 = _make_user(dbsession, "exh1@test.com", "+15550140001")
        u2 = _make_user(dbsession, "exh2@test.com", "+15550140002")
        a1 = _make_assistant(dbsession, u1, "Bot1")
        a2 = _make_assistant(dbsession, u2, "Bot2")
        _enable_whatsapp(dbsession, a1, pool_numbers[0])
        _enable_whatsapp(dbsession, a2, pool_numbers[0])

        # u2 also has another assistant on pool_numbers[1]
        a3 = _make_assistant(dbsession, u2, "Bot3")
        _enable_whatsapp(dbsession, a3, pool_numbers[1])

        dao.get_or_create_route(a1.agent_id, "+15559000007")
        with pytest.raises(ValueError, match="could not be resolved"):
            dao.get_or_create_route(a2.agent_id, "+15559000007")

    def test_reassignment_is_atomic_on_failure(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        """Failed reassignment doesn't leave partial state."""
        u1 = _make_user(dbsession, "atom1@test.com", "+15550150001")
        u2 = _make_user(dbsession, "atom2@test.com", "+15550150002")
        a1 = _make_assistant(dbsession, u1, "Bot1")
        a2 = _make_assistant(dbsession, u2, "Bot2")
        _enable_whatsapp(dbsession, a1, pool_numbers[0])
        _enable_whatsapp(dbsession, a2, pool_numbers[0])

        a3 = _make_assistant(dbsession, u2, "Bot3")
        _enable_whatsapp(dbsession, a3, pool_numbers[1])

        dao.get_or_create_route(a1.agent_id, "+15559000008")

        # Before the failed attempt
        a2_contact_before = (
            dbsession.query(AssistantContact)
            .filter(
                AssistantContact.assistant_id == a2.agent_id,
                AssistantContact.contact_type == "whatsapp",
                AssistantContact.status == "active",
            )
            .first()
        )
        original_value = a2_contact_before.contact_value

        try:
            dao.get_or_create_route(a2.agent_id, "+15559000008")
        except ValueError:
            dbsession.rollback()

        # a2's contact should be unchanged after rollback
        a2_contact_after = (
            dbsession.query(AssistantContact)
            .filter(
                AssistantContact.assistant_id == a2.agent_id,
                AssistantContact.contact_type == "whatsapp",
                AssistantContact.status == "active",
            )
            .first()
        )
        assert a2_contact_after.contact_value == original_value


class TestCase2Reassignment:
    def test_both_assistants_get_new_unique_numbers(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        pool3 = SharedPoolNumber(number="+15550000103", platform="whatsapp")
        dbsession.add(pool3)
        dbsession.flush()

        u_a = _make_user(dbsession, "u2u_a@test.com", "+15550200001", name="A")
        u_b = _make_user(dbsession, "u2u_b@test.com", "+15550200002", name="B")
        a_a = _make_assistant(dbsession, u_a, "BotA")
        a_b = _make_assistant(dbsession, u_b, "BotB")
        _enable_whatsapp(dbsession, a_a, pool_numbers[0])
        _enable_whatsapp(dbsession, a_b, pool_numbers[0])

        dbsession.add(
            SharedPlatformRoute(
                pool_number_id=pool_numbers[0].id,
                contact_number=u_b.whatsapp_number,
                assistant_id=a_b.agent_id,
            ),
        )
        dbsession.flush()

        route, res = dao.get_or_create_route(a_a.agent_id, u_b.whatsapp_number)
        assert res is not None
        new_a = res.new_pool_assignments[a_a.agent_id]
        new_b = res.new_pool_assignments[a_b.agent_id]
        assert new_a != new_b
        assert new_a != pool_numbers[0].number
        assert new_b != pool_numbers[0].number

    def test_fails_if_not_enough_pool_numbers(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        """User-to-user needs 2 new numbers; only 1 available → fails."""
        u_a = _make_user(dbsession, "u2u_fail_a@test.com", "+15550210001", name="A")
        u_b = _make_user(dbsession, "u2u_fail_b@test.com", "+15550210002", name="B")
        a_a = _make_assistant(dbsession, u_a, "BotA")
        a_b = _make_assistant(dbsession, u_b, "BotB")
        _enable_whatsapp(dbsession, a_a, pool_numbers[0])
        _enable_whatsapp(dbsession, a_b, pool_numbers[0])

        dbsession.add(
            SharedPlatformRoute(
                pool_number_id=pool_numbers[0].id,
                contact_number=u_b.whatsapp_number,
                assistant_id=a_b.agent_id,
            ),
        )
        dbsession.flush()

        # Only pool_numbers[1] is free — not enough for 2 reassignments
        with pytest.raises(ValueError, match="could not be resolved"):
            dao.get_or_create_route(a_a.agent_id, u_b.whatsapp_number)


class TestCase3Reassignment:
    def test_personal_assistant_reassigned_over_org_assistant(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        owner = _make_user(dbsession, "org_keep@test.com", "+15550300001")
        joiner = _make_user(dbsession, "pers_move@test.com", "+15550300002")
        org = _make_org(dbsession, owner, "KeepOrg")

        org_assistant = _make_assistant(dbsession, owner, "OrgBot", org.id)
        _enable_whatsapp(dbsession, org_assistant, pool_numbers[0])

        personal = _make_assistant(dbsession, joiner, "PersonalBot")
        _enable_whatsapp(dbsession, personal, pool_numbers[0])

        _add_org_member(dbsession, org, joiner)
        conflicts = dao.detect_membership_conflicts(joiner.id, org.id)
        resolutions = dao.resolve_membership_conflicts(conflicts)

        assert len(resolutions) == 1
        assert personal.agent_id in resolutions[0].affected_assistant_ids
        assert org_assistant.agent_id not in resolutions[0].affected_assistant_ids

        # Personal assistant moved to pool_numbers[1]
        p_contact = (
            dbsession.query(AssistantContact)
            .filter(
                AssistantContact.assistant_id == personal.agent_id,
                AssistantContact.contact_type == "whatsapp",
                AssistantContact.status == "active",
            )
            .first()
        )
        assert p_contact.contact_value == pool_numbers[1].number

    def test_window_timestamps_preserved_after_migration(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        u1 = _make_user(dbsession, "ts1@test.com", "+15550310001")
        u2 = _make_user(dbsession, "ts2@test.com", "+15550310002")
        a1 = _make_assistant(dbsession, u1, "Bot1")
        a2 = _make_assistant(dbsession, u2, "Bot2")
        _enable_whatsapp(dbsession, a1, pool_numbers[0])
        _enable_whatsapp(dbsession, a2, pool_numbers[0])

        # Create a route with a recent inbound timestamp
        recent = datetime.now(timezone.utc) - timedelta(hours=2)
        route = SharedPlatformRoute(
            pool_number_id=pool_numbers[0].id,
            contact_number="+15559000010",
            assistant_id=a2.agent_id,
            last_inbound_at=recent,
        )
        dbsession.add(route)
        dbsession.flush()

        # Trigger conflict to migrate a2
        dao.get_or_create_route(a1.agent_id, "+15559000011")
        dao.get_or_create_route(a2.agent_id, "+15559000011")

        # Migrated route should preserve timestamp
        migrated = (
            dbsession.query(SharedPlatformRoute)
            .filter(
                SharedPlatformRoute.assistant_id == a2.agent_id,
                SharedPlatformRoute.contact_number == "+15559000010",
            )
            .first()
        )
        assert migrated.last_inbound_at == recent


# ============================================================================
# Group C: Stale Numbers & Cold Messages
# ============================================================================


class TestDecommissionedRoutes:
    def test_inbound_to_decommissioned_returns_auto_reply(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        u = _make_user(dbsession, "decomm@test.com")
        a = _make_assistant(dbsession, u, "OldBot")
        dbsession.add(
            DecommissionedRoute(
                platform="whatsapp",
                pool_number_id=pool_numbers[0].id,
                contact_identifier="+15550400001",
                old_assistant_id=a.agent_id,
                new_pool_number_id=pool_numbers[1].id,
            ),
        )
        dbsession.flush()

        result = dao.resolve_inbound(pool_numbers[0].number, "+15550400001")
        assert result is not None
        assert result["action"] == "auto_reply"

    def test_inbound_to_active_route_still_works(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        u = _make_user(dbsession, "active@test.com", "+15550410001")
        a = _make_assistant(dbsession, u, "Bot")
        _enable_whatsapp(dbsession, a, pool_numbers[0])

        dbsession.add(
            SharedPlatformRoute(
                pool_number_id=pool_numbers[0].id,
                contact_number="+15550410002",
                assistant_id=a.agent_id,
            ),
        )
        dbsession.flush()

        result = dao.resolve_inbound(pool_numbers[0].number, "+15550410002")
        assert result["assistant_id"] == a.agent_id
        assert result["role"] == "contact"

    def test_decommissioned_does_not_affect_other_contacts(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        u = _make_user(dbsession, "other@test.com", "+15550420001")
        a = _make_assistant(dbsession, u, "Bot")
        _enable_whatsapp(dbsession, a, pool_numbers[0])

        dbsession.add(
            DecommissionedRoute(
                platform="whatsapp",
                pool_number_id=pool_numbers[0].id,
                contact_identifier="+15550420002",
                old_assistant_id=a.agent_id,
            ),
        )
        dbsession.add(
            SharedPlatformRoute(
                pool_number_id=pool_numbers[0].id,
                contact_number="+15550420003",
                assistant_id=a.agent_id,
            ),
        )
        dbsession.flush()

        # Different contact on same pool should still route normally
        result = dao.resolve_inbound(pool_numbers[0].number, "+15550420003")
        assert result["assistant_id"] == a.agent_id


class TestColdMessages:
    def test_unknown_sender_shared_pool_returns_reject_cold(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        """Unknown sender on pool shared by >1 assistant → reject_cold."""
        u1 = _make_user(dbsession, "cold1@test.com", "+15550500001")
        u2 = _make_user(dbsession, "cold2@test.com", "+15550500002")
        a1 = _make_assistant(dbsession, u1, "Bot1")
        a2 = _make_assistant(dbsession, u2, "Bot2")
        _enable_whatsapp(dbsession, a1, pool_numbers[0])
        _enable_whatsapp(dbsession, a2, pool_numbers[0])

        result = dao.resolve_inbound(pool_numbers[0].number, "+15550509999")
        assert result is not None
        assert result["action"] == "reject_cold"

    def test_unknown_sender_dedicated_pool_returns_none(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        """Unknown sender on pool used by exactly 1 assistant → None."""
        u = _make_user(dbsession, "ded@test.com", "+15550510001")
        a = _make_assistant(dbsession, u, "Bot")
        _enable_whatsapp(dbsession, a, pool_numbers[0])

        result = dao.resolve_inbound(pool_numbers[0].number, "+15550519999")
        assert result is None

    def test_known_sender_shared_pool_routes_normally(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        """Known sender on shared pool routes to their assistant."""
        u1 = _make_user(dbsession, "known1@test.com", "+15550520001")
        u2 = _make_user(dbsession, "known2@test.com", "+15550520002")
        a1 = _make_assistant(dbsession, u1, "Bot1")
        a2 = _make_assistant(dbsession, u2, "Bot2")
        _enable_whatsapp(dbsession, a1, pool_numbers[0])
        _enable_whatsapp(dbsession, a2, pool_numbers[0])

        dbsession.add(
            SharedPlatformRoute(
                pool_number_id=pool_numbers[0].id,
                contact_number="+15550529999",
                assistant_id=a1.agent_id,
            ),
        )
        dbsession.flush()

        result = dao.resolve_inbound(pool_numbers[0].number, "+15550529999")
        assert result["assistant_id"] == a1.agent_id
        assert result["role"] == "contact"


# ============================================================================
# Group D: Notifications
# ============================================================================


class TestNotifications:
    def test_case1_notifies_initiator_user(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        u1 = _make_user(dbsession, "notif1@test.com", "+15550600001", name="Alice")
        u2 = _make_user(dbsession, "notif2@test.com", "+15550600002", name="Bob")
        a1 = _make_assistant(dbsession, u1, "Bot1")
        a2 = _make_assistant(dbsession, u2, "BotInit")
        _enable_whatsapp(dbsession, a1, pool_numbers[0])
        _enable_whatsapp(dbsession, a2, pool_numbers[0])

        dao.get_or_create_route(a1.agent_id, "+15559000020")
        _, res = dao.get_or_create_route(a2.agent_id, "+15559000020")

        assert res is not None
        recipients = res.notification_recipients
        recipient_numbers = {r["to"] for r in recipients}
        assert u2.whatsapp_number in recipient_numbers

    def test_case1_notifies_org_members_if_org_assistant(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        owner = _make_user(
            dbsession,
            "org_notif_owner@test.com",
            "+15550610001",
            name="Owner",
        )
        member = _make_user(
            dbsession,
            "org_notif_member@test.com",
            "+15550610002",
            name="Member",
        )
        outsider = _make_user(
            dbsession,
            "org_notif_out@test.com",
            "+15550610003",
            name="Out",
        )
        org = _make_org(dbsession, owner, "NotifOrg")
        _add_org_member(dbsession, org, member)

        a_org = _make_assistant(dbsession, owner, "OrgBot", org.id)
        a_out = _make_assistant(dbsession, outsider, "OutBot")
        _enable_whatsapp(dbsession, a_org, pool_numbers[0])
        _enable_whatsapp(dbsession, a_out, pool_numbers[0])

        # outsider's assistant triggers conflict by routing to same contact
        dao.get_or_create_route(a_org.agent_id, "+15559000021")
        _, res = dao.get_or_create_route(a_out.agent_id, "+15559000021")

        assert res is not None
        recipient_numbers = {r["to"] for r in res.notification_recipients}
        assert outsider.whatsapp_number in recipient_numbers

    def test_notification_skips_users_without_whatsapp_number(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        u1 = _make_user(dbsession, "skip1@test.com", "+15550620001", name="Has")
        u_no_wa = _make_user(dbsession, "skip2@test.com", None, name="NoWA")
        a1 = _make_assistant(dbsession, u1, "Bot1")
        a2 = _make_assistant(dbsession, u_no_wa, "Bot2")
        _enable_whatsapp(dbsession, a1, pool_numbers[0])
        _enable_whatsapp(dbsession, a2, pool_numbers[0])

        dao.get_or_create_route(a1.agent_id, "+15559000022")
        _, res = dao.get_or_create_route(a2.agent_id, "+15559000022")

        # u_no_wa has no whatsapp_number → should not be in recipients
        if res:
            recipient_numbers = {r["to"] for r in res.notification_recipients}
            assert u_no_wa.whatsapp_number not in recipient_numbers

    def test_notification_recipient_gets_correct_user_name_and_agent_name(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        u1 = _make_user(dbsession, "names1@test.com", "+15550630001", name="Alice")
        u2 = _make_user(dbsession, "names2@test.com", "+15550630002", name="Bob")
        a1 = _make_assistant(dbsession, u1, "AliceBot")
        a2 = _make_assistant(dbsession, u2, "BobBot")
        _enable_whatsapp(dbsession, a1, pool_numbers[0])
        _enable_whatsapp(dbsession, a2, pool_numbers[0])

        dao.get_or_create_route(a1.agent_id, "+15559000023")
        _, res = dao.get_or_create_route(a2.agent_id, "+15559000023")

        assert res is not None
        bob_notif = [
            r for r in res.notification_recipients if r["to"] == u2.whatsapp_number
        ]
        assert len(bob_notif) == 1
        assert bob_notif[0]["user_name"] == "Bob"
        assert bob_notif[0]["agent_name"] == "BobBot"

    def test_conflict_event_created(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        u1 = _make_user(dbsession, "evt1@test.com", "+15550640001")
        u2 = _make_user(dbsession, "evt2@test.com", "+15550640002")
        a1 = _make_assistant(dbsession, u1, "Bot1")
        a2 = _make_assistant(dbsession, u2, "Bot2")
        _enable_whatsapp(dbsession, a1, pool_numbers[0])
        _enable_whatsapp(dbsession, a2, pool_numbers[0])

        dao.get_or_create_route(a1.agent_id, "+15559000024")
        _, res = dao.get_or_create_route(a2.agent_id, "+15559000024")

        assert res.conflict_event_id is not None
        event = dbsession.query(ConflictEvent).get(res.conflict_event_id)
        assert event.status == "notifying"
        assert event.conflict_type == "contact_overlap"

    def test_notification_status_update(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        u1 = _make_user(dbsession, "nsu1@test.com", "+15550650001")
        u2 = _make_user(dbsession, "nsu2@test.com", "+15550650002")
        a1 = _make_assistant(dbsession, u1, "Bot1")
        a2 = _make_assistant(dbsession, u2, "Bot2")
        _enable_whatsapp(dbsession, a1, pool_numbers[0])
        _enable_whatsapp(dbsession, a2, pool_numbers[0])

        dao.get_or_create_route(a1.agent_id, "+15559000025")
        _, res = dao.get_or_create_route(a2.agent_id, "+15559000025")

        event = dao.update_notification_status(
            res.conflict_event_id,
            u2.whatsapp_number,
            "SM_test_sid",
            "delivered",
        )
        assert event.notification_status[u2.whatsapp_number]["status"] == "delivered"
        assert event.status == "resolved"


# ============================================================================
# Group E: Org Integration (tested via DAO, endpoint hooks tested elsewhere)
# ============================================================================


class TestOrgMembershipHooks:
    def test_resolve_membership_conflicts_returns_resolutions(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        owner = _make_user(dbsession, "hook_owner@test.com", "+15550700001")
        joiner = _make_user(dbsession, "hook_joiner@test.com", "+15550700002")
        org = _make_org(dbsession, owner, "HookOrg")

        org_a = _make_assistant(dbsession, owner, "OrgBot", org.id)
        _enable_whatsapp(dbsession, org_a, pool_numbers[0])

        personal = _make_assistant(dbsession, joiner, "PBot")
        _enable_whatsapp(dbsession, personal, pool_numbers[0])

        _add_org_member(dbsession, org, joiner)

        conflicts = dao.detect_membership_conflicts(joiner.id, org.id)
        resolutions = dao.resolve_membership_conflicts(conflicts)

        assert len(resolutions) == 1
        assert resolutions[0].conflict_type == "org_membership"

    def test_accept_invite_with_conflict_still_succeeds(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        """Membership conflicts don't prevent joining — they just trigger reassignment."""
        owner = _make_user(dbsession, "inv_owner@test.com", "+15550710001")
        joiner = _make_user(dbsession, "inv_joiner@test.com", "+15550710002")
        org = _make_org(dbsession, owner, "InvOrg")

        org_a = _make_assistant(dbsession, owner, "OrgBot", org.id)
        _enable_whatsapp(dbsession, org_a, pool_numbers[0])

        personal = _make_assistant(dbsession, joiner, "PBot")
        _enable_whatsapp(dbsession, personal, pool_numbers[0])

        _add_org_member(dbsession, org, joiner)

        conflicts = dao.detect_membership_conflicts(joiner.id, org.id)
        resolutions = dao.resolve_membership_conflicts(conflicts)

        # Should succeed without raising
        assert len(resolutions) == 1
        # Personal assistant should be on a different pool now
        p_contact = (
            dbsession.query(AssistantContact)
            .filter(
                AssistantContact.assistant_id == personal.agent_id,
                AssistantContact.contact_type == "whatsapp",
                AssistantContact.status == "active",
            )
            .first()
        )
        assert p_contact.contact_value == pool_numbers[1].number


# ============================================================================
# Group F: Platform Agnosticism
# ============================================================================


class TestPlatformAgnostic:
    def test_whatsapp_and_instagram_pools_independent(
        self,
        dbsession: Session,
        pool_numbers,
    ):
        """Different platform pools don't interfere with each other."""
        dbsession.add(
            SharedPoolNumber(number="@bot_1", platform="instagram"),
        )
        dbsession.flush()

        wa_dao = SharedPoolDAO(dbsession, platform="whatsapp")
        ig_dao = SharedPoolDAO(dbsession, platform="instagram")

        wa_pool = wa_dao.list_pool_numbers()
        ig_pool = ig_dao.list_pool_numbers()

        assert len(wa_pool) == 2
        assert len(ig_pool) == 1
        assert all(p.platform == "whatsapp" for p in wa_pool)
        assert all(p.platform == "instagram" for p in ig_pool)

    def test_pool_exhaustion_is_per_platform(
        self,
        dbsession: Session,
        pool_numbers,
    ):
        """Exhausting WhatsApp pool doesn't affect Instagram pool."""
        dbsession.add(
            SharedPoolNumber(number="@ig_bot", platform="instagram"),
        )
        dbsession.flush()

        u = _make_user(dbsession, "cross@test.com", "+15550800001")
        a1 = _make_assistant(dbsession, u, "Bot1")
        a2 = _make_assistant(dbsession, u, "Bot2")
        a3 = _make_assistant(dbsession, u, "Bot3")
        _enable_whatsapp(dbsession, a1, pool_numbers[0])
        _enable_whatsapp(dbsession, a2, pool_numbers[1])

        wa_dao = SharedPoolDAO(dbsession, platform="whatsapp")
        ig_dao = SharedPoolDAO(dbsession, platform="instagram")

        # WhatsApp pool exhausted for this user
        eligible_wa = wa_dao.find_eligible_pool_numbers(a3.agent_id, [u.id])
        assert len(eligible_wa) == 0

        # Instagram pool still available
        eligible_ig = ig_dao.find_eligible_pool_numbers(a3.agent_id, [u.id])
        assert len(eligible_ig) == 1


# ============================================================================
# Group G: 24h Window + Reassignment
# ============================================================================


class TestWindowPreservation:
    def test_window_timestamp_preserved_after_migration(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        u1 = _make_user(dbsession, "win1@test.com", "+15550900001")
        u2 = _make_user(dbsession, "win2@test.com", "+15550900002")
        a1 = _make_assistant(dbsession, u1, "Bot1")
        a2 = _make_assistant(dbsession, u2, "Bot2")
        _enable_whatsapp(dbsession, a1, pool_numbers[0])
        _enable_whatsapp(dbsession, a2, pool_numbers[0])

        ts = datetime.now(timezone.utc) - timedelta(hours=1)
        dbsession.add(
            SharedPlatformRoute(
                pool_number_id=pool_numbers[0].id,
                contact_number="+15559000030",
                assistant_id=a2.agent_id,
                last_inbound_at=ts,
            ),
        )
        dbsession.flush()

        dao.get_or_create_route(a1.agent_id, "+15559000031")
        dao.get_or_create_route(a2.agent_id, "+15559000031")

        migrated = (
            dbsession.query(SharedPlatformRoute)
            .filter(
                SharedPlatformRoute.assistant_id == a2.agent_id,
                SharedPlatformRoute.contact_number == "+15559000030",
            )
            .first()
        )
        assert migrated.last_inbound_at == ts

    def test_fresh_route_after_migration_has_no_window(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        u1 = _make_user(dbsession, "fresh1@test.com", "+15550910001")
        u2 = _make_user(dbsession, "fresh2@test.com", "+15550910002")
        a1 = _make_assistant(dbsession, u1, "Bot1")
        a2 = _make_assistant(dbsession, u2, "Bot2")
        _enable_whatsapp(dbsession, a1, pool_numbers[0])
        _enable_whatsapp(dbsession, a2, pool_numbers[0])

        dao.get_or_create_route(a1.agent_id, "+15559000032")
        route2, _ = dao.get_or_create_route(a2.agent_id, "+15559000032")

        # The new route (created after migration) should have no window
        assert route2.last_inbound_at is None


# ============================================================================
# Group H: Admin Endpoints
# ============================================================================


class TestAdminEndpoints:
    @pytest.fixture
    async def test_user(self, client: AsyncClient):
        return await create_test_user(client, "conflict_api_test@test.com")

    @pytest.fixture
    async def test_assistant(self, test_user, dbsession: Session):
        assistant = Assistant(user_id=test_user["id"], first_name="ConflictBot")
        dbsession.add(assistant)
        dbsession.commit()
        return {"agent_id": assistant.agent_id}

    async def test_route_returns_success_after_inline_conflict_resolution(
        self,
        client: AsyncClient,
        dbsession: Session,
        pool_numbers,
    ):
        """POST /whatsapp/route resolves conflicts inline and returns 200."""
        ba1 = BillingAccount(credits=100)
        ba2 = BillingAccount(credits=100)
        dbsession.add_all([ba1, ba2])
        dbsession.flush()
        u1 = User(
            id=str(uuid.uuid4()),
            email="api_c1@test.com",
            whatsapp_number="+15551100001",
            billing_account_id=ba1.id,
        )
        u2 = User(
            id=str(uuid.uuid4()),
            email="api_c2@test.com",
            whatsapp_number="+15551100002",
            billing_account_id=ba2.id,
        )
        dbsession.add_all([u1, u2])
        dbsession.flush()

        a1 = Assistant(user_id=u1.id, first_name="A1")
        a2 = Assistant(user_id=u2.id, first_name="A2")
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
        dbsession.commit()

        # a1 creates route
        await client.post(
            "/v0/admin/whatsapp/route",
            json={"assistant_id": a1.agent_id, "contact_number": "+15559000040"},
            headers=ADMIN_HEADERS,
        )

        # a2 creates conflicting route → should succeed with conflict_resolved=True
        resp = await client.post(
            "/v0/admin/whatsapp/route",
            json={"assistant_id": a2.agent_id, "contact_number": "+15559000040"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["conflict_resolved"] is True
        assert data["conflict_event_id"] is not None
        assert data["pool_number"] == pool_numbers[1].number

    async def test_resolve_returns_auto_reply_for_decommissioned(
        self,
        client: AsyncClient,
        dbsession: Session,
        pool_numbers,
    ):
        ba = BillingAccount(credits=100)
        dbsession.add(ba)
        dbsession.flush()
        u = User(
            id=str(uuid.uuid4()),
            email="decomm_api@test.com",
            billing_account_id=ba.id,
        )
        dbsession.add(u)
        dbsession.flush()
        a = Assistant(user_id=u.id, first_name="D")
        dbsession.add(a)
        dbsession.flush()

        dbsession.add(
            DecommissionedRoute(
                platform="whatsapp",
                pool_number_id=pool_numbers[0].id,
                contact_identifier="+15551200001",
                old_assistant_id=a.agent_id,
            ),
        )
        dbsession.commit()

        resp = await client.get(
            "/v0/admin/whatsapp/resolve",
            params={
                "pool_number": pool_numbers[0].number,
                "sender": "+15551200001",
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["action"] == "auto_reply"

    async def test_resolve_returns_reject_cold_for_shared_unknown(
        self,
        client: AsyncClient,
        dbsession: Session,
        pool_numbers,
    ):
        ba1 = BillingAccount(credits=100)
        ba2 = BillingAccount(credits=100)
        dbsession.add_all([ba1, ba2])
        dbsession.flush()
        u1 = User(
            id=str(uuid.uuid4()),
            email="cold_api1@test.com",
            whatsapp_number="+15551300001",
            billing_account_id=ba1.id,
        )
        u2 = User(
            id=str(uuid.uuid4()),
            email="cold_api2@test.com",
            whatsapp_number="+15551300002",
            billing_account_id=ba2.id,
        )
        dbsession.add_all([u1, u2])
        dbsession.flush()

        a1 = Assistant(user_id=u1.id, first_name="C1")
        a2 = Assistant(user_id=u2.id, first_name="C2")
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
        dbsession.commit()

        resp = await client.get(
            "/v0/admin/whatsapp/resolve",
            params={
                "pool_number": pool_numbers[0].number,
                "sender": "+15551399999",
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["action"] == "reject_cold"

    async def test_conflict_events_list_endpoint(
        self,
        client: AsyncClient,
        dbsession: Session,
        pool_numbers,
    ):
        dbsession.add(
            ConflictEvent(
                platform="whatsapp",
                conflict_type="contact_overlap",
                trigger_assistant_id=None,
                affected_assistant_ids=[1, 2],
                old_pool_assignments={"1": "+18507877970"},
                new_pool_assignments={"1": "+17343611691"},
                status="resolved",
            ),
        )
        dbsession.commit()

        resp = await client.get(
            "/v0/admin/whatsapp/conflicts",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert len(data) >= 1
        assert data[0]["conflict_type"] == "contact_overlap"

    async def test_notification_status_endpoint(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        event = ConflictEvent(
            platform="whatsapp",
            conflict_type="contact_overlap",
            trigger_assistant_id=None,
            affected_assistant_ids=[1],
            old_pool_assignments={"1": "+18507877970"},
            new_pool_assignments={"1": "+17343611691"},
            notification_recipients=[{"to": "+15551400001"}],
            status="notifying",
        )
        dbsession.add(event)
        dbsession.commit()

        resp = await client.post(
            "/v0/admin/whatsapp/notification-status",
            json={
                "conflict_event_id": event.id,
                "recipient_number": "+15551400001",
                "message_sid": "SM_test",
                "status": "delivered",
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["status"] == "resolved"


# ============================================================================
# Group I: Model Constraints
# ============================================================================


class TestPoolNumberModelConstraints:
    def test_pool_numbers_seeded(self, pool_numbers):
        assert len(pool_numbers) == 2
        numbers = {p.number for p in pool_numbers}
        assert "+18507877970" in numbers
        assert "+17343611691" in numbers

    def test_pool_number_unique(self, dbsession: Session):
        dbsession.add(SharedPoolNumber(number="+18507877970", platform="whatsapp"))
        with pytest.raises(IntegrityError):
            dbsession.flush()
        dbsession.rollback()

    def test_pool_number_status_constraint(self, dbsession: Session):
        dbsession.add(
            SharedPoolNumber(
                number="+10000000000",
                status="bogus",
                platform="whatsapp",
            ),
        )
        with pytest.raises(IntegrityError):
            dbsession.flush()
        dbsession.rollback()


class TestRouteModelConstraints:
    def test_uq_pool_contact(self, dbsession: Session, pool_numbers):
        """Same (pool_number_id, contact_number) pair cannot exist twice."""
        user = _make_user(dbsession, "uq_route@test.com", "+15550091111")
        assistant = _make_assistant(dbsession, user, "UQBot")

        r1 = SharedPlatformRoute(
            pool_number_id=pool_numbers[0].id,
            contact_number="+15559999999",
            assistant_id=assistant.agent_id,
        )
        dbsession.add(r1)
        dbsession.flush()

        r2 = SharedPlatformRoute(
            pool_number_id=pool_numbers[0].id,
            contact_number="+15559999999",
            assistant_id=assistant.agent_id,
        )
        dbsession.add(r2)
        with pytest.raises(IntegrityError):
            dbsession.flush()
        dbsession.rollback()


class TestContactValueSharing:
    def test_whatsapp_contacts_can_share_contact_value(
        self,
        dbsession: Session,
        pool_numbers,
    ):
        """Two assistants can share the same WhatsApp pool number."""
        user = _make_user(dbsession, "share_cv@test.com", "+15550092222")
        a1 = _make_assistant(dbsession, user, "Bot1")
        a2 = _make_assistant(dbsession, user, "Bot2")

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
        assert c1.id is not None
        assert c2.id is not None

    def test_phone_contacts_still_unique(self, dbsession: Session):
        """Phone contacts must have unique contact_value."""
        user = _make_user(dbsession, "phone_uq@test.com")
        a1 = _make_assistant(dbsession, user, "Bot1")
        a2 = _make_assistant(dbsession, user, "Bot2")

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
# Group J: Tier 1 Resolve Inbound (Owner Matching / Priority)
# ============================================================================


class TestTier1ResolveInbound:
    def test_tier1_user_match(self, dbsession: Session, dao, pool_numbers):
        """Tier 1: sender matches user.whatsapp_number → route to assistant."""
        user = _make_user(dbsession, "t1_owner@test.com", "+15550101010")
        assistant = _make_assistant(dbsession, user, "T1Bot")
        _enable_whatsapp(dbsession, assistant, pool_numbers[0])

        result = dao.resolve_inbound(pool_numbers[0].number, "+15550101010")
        assert result is not None
        assert result["assistant_id"] == assistant.agent_id
        assert result["role"] == "owner"

    def test_tier2_route_match(self, dbsession: Session, dao, pool_numbers):
        """Tier 2: sender is an external contact with a route entry."""
        user = _make_user(dbsession, "t2_ext@test.com", "+15550102020")
        assistant = _make_assistant(dbsession, user, "T2Bot")
        _enable_whatsapp(dbsession, assistant, pool_numbers[0])

        dbsession.add(
            SharedPlatformRoute(
                pool_number_id=pool_numbers[0].id,
                contact_number="+15558888888",
                assistant_id=assistant.agent_id,
            ),
        )
        dbsession.flush()

        result = dao.resolve_inbound(pool_numbers[0].number, "+15558888888")
        assert result is not None
        assert result["assistant_id"] == assistant.agent_id
        assert result["role"] == "contact"

    def test_no_match_returns_none_for_dedicated(
        self,
        dbsession: Session,
        dao,
        pool_numbers,
    ):
        """No match on dedicated pool (single assistant) → None."""
        user = _make_user(dbsession, "dedicated@test.com", "+15550103030")
        assistant = _make_assistant(dbsession, user, "DedBot")
        _enable_whatsapp(dbsession, assistant, pool_numbers[0])

        result = dao.resolve_inbound(pool_numbers[0].number, "+15550000000")
        assert result is None

    def test_tier1_takes_priority_over_tier2(
        self,
        dbsession: Session,
        dao,
        pool_numbers,
    ):
        """Tier 1 (user lookup) takes priority over Tier 2 (route table)."""
        user = _make_user(dbsession, "prio@test.com", "+15550104040")
        a1 = _make_assistant(dbsession, user, "Bot1")
        a2 = _make_assistant(dbsession, user, "Bot2")

        _enable_whatsapp(dbsession, a1, pool_numbers[0])

        # Route table points to a2
        dbsession.add(
            SharedPlatformRoute(
                pool_number_id=pool_numbers[0].id,
                contact_number="+15550104040",
                assistant_id=a2.agent_id,
            ),
        )
        dbsession.flush()

        result = dao.resolve_inbound(pool_numbers[0].number, "+15550104040")
        assert result["assistant_id"] == a1.agent_id
        assert result["role"] == "owner"

    def test_whatsapp_number_priority_over_phone(
        self,
        dbsession: Session,
        dao,
        pool_numbers,
    ):
        """Inbound routing matches whatsapp_number, not phone_number."""
        shared = "+15550505050"
        u_wa = _make_user(dbsession, "wa_prio@test.com", whatsapp_number=shared)
        ba2 = BillingAccount(credits=100)
        dbsession.add(ba2)
        dbsession.flush()
        u_ph = User(
            id=str(uuid.uuid4()),
            email="ph_prio@test.com",
            phone_number=shared,
            billing_account_id=ba2.id,
        )
        dbsession.add(u_ph)
        dbsession.flush()

        a_wa = _make_assistant(dbsession, u_wa, "WaBot")
        a_ph = _make_assistant(dbsession, u_ph, "PhBot")
        _enable_whatsapp(dbsession, a_wa, pool_numbers[0])
        _enable_whatsapp(dbsession, a_ph, pool_numbers[0])

        result = dao.resolve_inbound(pool_numbers[0].number, shared)
        assert result is not None
        assert result["assistant_id"] == a_wa.agent_id
        assert result["role"] == "owner"

    def test_phone_only_user_does_not_match_tier1(
        self,
        dbsession: Session,
        dao,
        pool_numbers,
    ):
        """User with only phone_number (no whatsapp_number) doesn't match Tier 1."""
        ba = BillingAccount(credits=100)
        dbsession.add(ba)
        dbsession.flush()
        u = User(
            id=str(uuid.uuid4()),
            email="phoneonly@test.com",
            phone_number="+15550707070",
            billing_account_id=ba.id,
        )
        dbsession.add(u)
        dbsession.flush()

        a = _make_assistant(dbsession, u, "PhOnly")
        _enable_whatsapp(dbsession, a, pool_numbers[0])

        result = dao.resolve_inbound(pool_numbers[0].number, "+15550707070")
        assert result is None


# ============================================================================
# Group K: Pool Assignment (DAO)
# ============================================================================


class TestPoolAssignment:
    def test_assign_first_available(self, dbsession: Session, dao, pool_numbers):
        user = _make_user(dbsession, "assign1@test.com", "+15550201010")
        assistant = _make_assistant(dbsession, user, "AssignBot")

        pool = dao.assign_pool_number(assistant.agent_id, [user.id])
        assert pool.number in {p.number for p in pool_numbers}

    def test_conflict_avoidance_same_user(self, dbsession: Session, dao, pool_numbers):
        """If user already has assistant A on pool1, assistant B avoids pool1."""
        user = _make_user(dbsession, "conflict_avoid@test.com", "+15550202020")
        a1 = _make_assistant(dbsession, user, "Bot1")
        a2 = _make_assistant(dbsession, user, "Bot2")

        _enable_whatsapp(dbsession, a1, pool_numbers[0])

        pool = dao.assign_pool_number(a2.agent_id, [user.id])
        assert pool.number == pool_numbers[1].number

    def test_different_users_can_share_pool(
        self,
        dbsession: Session,
        dao,
        pool_numbers,
    ):
        """Different users' assistants can share the same pool number."""
        u1 = _make_user(dbsession, "share1@test.com", "+15550203030")
        u2 = _make_user(dbsession, "share2@test.com", "+15550204040")
        a1 = _make_assistant(dbsession, u1, "A1")
        a2 = _make_assistant(dbsession, u2, "A2")

        p1 = dao.assign_pool_number(a1.agent_id, [u1.id])
        _enable_whatsapp(dbsession, a1, p1)

        p2 = dao.assign_pool_number(a2.agent_id, [u2.id])
        assert p2.number == pool_numbers[0].number


# ============================================================================
# Group L: Route Management
# ============================================================================


class TestRouteManagement:
    def test_delete_routes_for_assistant(self, dbsession: Session, dao, pool_numbers):
        user = _make_user(dbsession, "del_routes@test.com", "+15550301010")
        assistant = _make_assistant(dbsession, user, "DelBot")
        _enable_whatsapp(dbsession, assistant, pool_numbers[0])

        dao.get_or_create_route(assistant.agent_id, "+15551000001")
        dao.get_or_create_route(assistant.agent_id, "+15551000002")
        dbsession.flush()

        count = dao.delete_routes_for_assistant(assistant.agent_id)
        assert count == 2
        assert len(dao.get_routes_for_assistant(assistant.agent_id)) == 0

    def test_get_or_create_route_idempotent(
        self,
        dbsession: Session,
        dao,
        pool_numbers,
    ):
        """Second call with same assistant + contact returns existing route."""
        user = _make_user(dbsession, "idempotent@test.com", "+15550302020")
        assistant = _make_assistant(dbsession, user, "IdemBot")
        _enable_whatsapp(dbsession, assistant, pool_numbers[0])

        route1, _ = dao.get_or_create_route(assistant.agent_id, "+15557777777")
        route2, _ = dao.get_or_create_route(assistant.agent_id, "+15557777777")
        assert route2.id == route1.id


# ============================================================================
# Group M: User.whatsapp_number Model + API
# ============================================================================


class TestUserWhatsappNumber:
    def test_unique_partial_index(self, dbsession: Session):
        """Two users can't have the same non-null whatsapp_number."""
        u1 = _make_user(dbsession, "dup1@test.com", whatsapp_number="+15553333333")
        _make_user(dbsession, "dup2_placeholder@test.com")

        ba2 = BillingAccount(credits=0)
        dbsession.add(ba2)
        dbsession.flush()
        u2 = User(
            id=str(uuid.uuid4()),
            email="dup2@test.com",
            whatsapp_number="+15553333333",
            billing_account_id=ba2.id,
        )
        dbsession.add(u2)
        with pytest.raises(IntegrityError):
            dbsession.flush()
        dbsession.rollback()

    def test_null_whatsapp_number_allowed(self, dbsession: Session):
        """Multiple users can have NULL whatsapp_number."""
        u1 = _make_user(dbsession, "null1@test.com")
        u2 = _make_user(dbsession, "null2@test.com")
        assert u1.whatsapp_number is None
        assert u2.whatsapp_number is None


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

    async def test_update_user_whatsapp(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        from orchestra.db.models.orchestra_models import PhoneVerification

        user = await create_test_user(client, "wa_update@test.com")

        dbsession.add(
            PhoneVerification(
                user_id=user["id"],
                phone_number="+16502530002",
                phone_type="whatsapp",
                code_hash="irrelevant",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
                verified_at=datetime.now(timezone.utc),
            ),
        )
        dbsession.flush()

        resp = await client.put(
            "/v0/admin/user",
            json={
                "user_id": user["id"],
                "whatsapp_number": "+16502530002",
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK, resp.json()

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
# Group N: Pool Number CRUD Endpoints
# ============================================================================


class TestPoolNumberCRUD:
    async def test_pool_endpoint_list(self, client: AsyncClient):
        response = await client.get(
            "/v0/admin/whatsapp/pool",
            headers=ADMIN_HEADERS,
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data) >= 2
        numbers = {p["number"] for p in data}
        assert "+18507877970" in numbers
        assert "+17343611691" in numbers

    async def test_add_pool_number(self, client: AsyncClient):
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

    async def test_add_duplicate_pool_number(self, client: AsyncClient, pool_numbers):
        resp = await client.post(
            "/v0/admin/whatsapp/pool",
            json={"number": pool_numbers[0].number},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_409_CONFLICT
        assert "already exists" in resp.json()["detail"]

    async def test_add_pool_number_with_sid(self, client: AsyncClient):
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

    async def test_update_pool_number_status(self, client: AsyncClient, pool_numbers):
        pool_id = pool_numbers[0].id
        resp = await client.patch(
            f"/v0/admin/whatsapp/pool/{pool_id}",
            json={"status": "inactive"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["status"] == "inactive"

    async def test_update_pool_number_sid(self, client: AsyncClient, pool_numbers):
        pool_id = pool_numbers[1].id
        resp = await client.patch(
            f"/v0/admin/whatsapp/pool/{pool_id}",
            json={"twilio_sender_sid": "MG_updated_456"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["twilio_sender_sid"] == "MG_updated_456"

    async def test_update_nonexistent_pool_number(self, client: AsyncClient):
        resp = await client.patch(
            "/v0/admin/whatsapp/pool/999999",
            json={"status": "inactive"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    async def test_delete_unused_pool_number(self, client: AsyncClient):
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

    async def test_delete_pool_number_in_use(
        self,
        client: AsyncClient,
        dbsession: Session,
        pool_numbers,
    ):
        """DELETE on a number with active contacts returns 400."""
        user = _make_user(dbsession, "inuse_del@test.com", "+15550401010")
        assistant = _make_assistant(dbsession, user, "InUse")
        _enable_whatsapp(dbsession, assistant, pool_numbers[0])
        dbsession.commit()

        resp = await client.delete(
            f"/v0/admin/whatsapp/pool/{pool_numbers[0].id}",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert "active assistant" in resp.json()["detail"]


# ============================================================================
# Group O: 24h Window Tracking (DAO + Endpoint)
# ============================================================================


class TestLastInboundAt:
    def test_tier2_resolve_sets_last_inbound_at(
        self,
        dbsession: Session,
        dao,
        pool_numbers,
    ):
        user = _make_user(dbsession, "t2_ts@test.com", "+15550501010")
        assistant = _make_assistant(dbsession, user, "T2Ts")
        _enable_whatsapp(dbsession, assistant, pool_numbers[0])

        route = SharedPlatformRoute(
            pool_number_id=pool_numbers[0].id,
            contact_number="+15558888888",
            assistant_id=assistant.agent_id,
        )
        dbsession.add(route)
        dbsession.flush()
        assert route.last_inbound_at is None

        dao.resolve_inbound(pool_numbers[0].number, "+15558888888")
        dbsession.refresh(route)
        assert route.last_inbound_at is not None
        assert (datetime.now(timezone.utc) - route.last_inbound_at) < timedelta(
            seconds=5,
        )

    def test_tier1_resolve_creates_route_with_last_inbound_at(
        self,
        dbsession: Session,
        dao,
        pool_numbers,
    ):
        user = _make_user(dbsession, "t1_ts@test.com", "+15550502020")
        assistant = _make_assistant(dbsession, user, "T1Ts")
        _enable_whatsapp(dbsession, assistant, pool_numbers[0])

        existing = (
            dbsession.query(SharedPlatformRoute)
            .filter(SharedPlatformRoute.contact_number == "+15550502020")
            .first()
        )
        assert existing is None

        result = dao.resolve_inbound(pool_numbers[0].number, "+15550502020")
        assert result is not None
        assert result["role"] == "owner"

        route = (
            dbsession.query(SharedPlatformRoute)
            .filter(SharedPlatformRoute.contact_number == "+15550502020")
            .first()
        )
        assert route is not None
        assert route.last_inbound_at is not None
        assert route.assistant_id == assistant.agent_id

    def test_repeated_inbound_updates_timestamp(
        self,
        dbsession: Session,
        dao,
        pool_numbers,
    ):
        user = _make_user(dbsession, "repeat_ts@test.com", "+15550503030")
        assistant = _make_assistant(dbsession, user, "RepeatTs")

        route = SharedPlatformRoute(
            pool_number_id=pool_numbers[0].id,
            contact_number="+15551111111",
            assistant_id=assistant.agent_id,
            last_inbound_at=datetime.now(timezone.utc) - timedelta(hours=48),
        )
        dbsession.add(route)
        dbsession.flush()

        old_ts = route.last_inbound_at
        dao.resolve_inbound(pool_numbers[0].number, "+15551111111")

        dbsession.refresh(route)
        assert route.last_inbound_at > old_ts


class TestWindowOpenEndpoint:
    @pytest.fixture
    async def _test_user(self, client: AsyncClient):
        return await create_test_user(client, "window_api@test.com")

    @pytest.fixture
    async def _test_assistant(self, _test_user, dbsession: Session):
        assistant = Assistant(user_id=_test_user["id"], first_name="WindowBot")
        dbsession.add(assistant)
        dbsession.commit()
        return {"agent_id": assistant.agent_id}

    async def test_window_closed_no_inbound(
        self,
        client: AsyncClient,
        _test_assistant,
        dbsession: Session,
    ):
        assistant_id = _test_assistant["agent_id"]

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

        route_resp = await client.post(
            "/v0/admin/whatsapp/route",
            json={"assistant_id": assistant_id, "contact_number": "+15552222222"},
            headers=ADMIN_HEADERS,
        )
        assert route_resp.status_code == status.HTTP_200_OK
        assert route_resp.json()["window_open"] is False

    async def test_window_open_after_inbound(
        self,
        client: AsyncClient,
        _test_assistant,
        dbsession: Session,
    ):
        assistant_id = _test_assistant["agent_id"]

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

        external = "+15553333333"

        await client.post(
            "/v0/admin/whatsapp/route",
            json={"assistant_id": assistant_id, "contact_number": external},
            headers=ADMIN_HEADERS,
        )

        resolve_resp = await client.get(
            "/v0/admin/whatsapp/resolve",
            params={"pool_number": pool_number, "sender": external},
            headers=ADMIN_HEADERS,
        )
        assert resolve_resp.status_code == status.HTTP_200_OK

        route_resp = await client.post(
            "/v0/admin/whatsapp/route",
            json={"assistant_id": assistant_id, "contact_number": external},
            headers=ADMIN_HEADERS,
        )
        assert route_resp.status_code == status.HTTP_200_OK
        assert route_resp.json()["window_open"] is True

    async def test_window_closed_after_24h(
        self,
        client: AsyncClient,
        _test_assistant,
        dbsession: Session,
    ):
        assistant_id = _test_assistant["agent_id"]

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

        external = "+15554444444"

        await client.post(
            "/v0/admin/whatsapp/route",
            json={"assistant_id": assistant_id, "contact_number": external},
            headers=ADMIN_HEADERS,
        )

        route = (
            dbsession.query(SharedPlatformRoute)
            .filter(SharedPlatformRoute.contact_number == external)
            .first()
        )
        route.last_inbound_at = datetime.now(timezone.utc) - timedelta(hours=25)
        dbsession.commit()

        route_resp = await client.post(
            "/v0/admin/whatsapp/route",
            json={"assistant_id": assistant_id, "contact_number": external},
            headers=ADMIN_HEADERS,
        )
        assert route_resp.status_code == status.HTTP_200_OK
        assert route_resp.json()["window_open"] is False


# ============================================================================
# Group P: Admin Endpoint Flows (assign → resolve, route → resolve, delete)
# ============================================================================


class TestAdminEndpointFlows:
    @pytest.fixture
    async def _test_user(self, client: AsyncClient):
        return await create_test_user(client, "flow_api@test.com")

    @pytest.fixture
    async def _test_assistant(self, _test_user, dbsession: Session):
        assistant = Assistant(user_id=_test_user["id"], first_name="FlowBot")
        dbsession.add(assistant)
        dbsession.commit()
        return {"agent_id": assistant.agent_id}

    async def test_assign_and_resolve_flow(
        self,
        client: AsyncClient,
        _test_user,
        _test_assistant,
        dbsession: Session,
    ):
        """Full flow: assign pool number → set user's whatsapp → resolve."""
        assistant_id = _test_assistant["agent_id"]

        assign_resp = await client.post(
            "/v0/admin/whatsapp/assign",
            json={"assistant_id": assistant_id},
            headers=ADMIN_HEADERS,
        )
        assert assign_resp.status_code == status.HTTP_200_OK, assign_resp.json()
        pool_number = assign_resp.json()["pool_number"]

        contact_dao = AssistantContactDAO(dbsession)
        contact_dao.upsert_assistant_contact(
            assistant_id=assistant_id,
            contact_type="whatsapp",
            contact_value=pool_number,
        )
        dbsession.commit()

        from orchestra.db.models.orchestra_models import PhoneVerification

        user_wa = "+16505551234"
        dbsession.add(
            PhoneVerification(
                user_id=_test_user["id"],
                phone_number=user_wa,
                phone_type="whatsapp",
                code_hash="irrelevant",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
                verified_at=datetime.now(timezone.utc),
            ),
        )
        dbsession.flush()

        update_resp = await client.put(
            "/v0/admin/user",
            json={
                "user_id": _test_user["id"],
                "whatsapp_number": user_wa,
            },
            headers=ADMIN_HEADERS,
        )
        assert update_resp.status_code == status.HTTP_200_OK, update_resp.text

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
        _test_assistant,
        dbsession: Session,
    ):
        """Create an outbound route → resolve inbound reply."""
        assistant_id = _test_assistant["agent_id"]

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

        external = "+15554444444"
        route_resp = await client.post(
            "/v0/admin/whatsapp/route",
            json={"assistant_id": assistant_id, "contact_number": external},
            headers=ADMIN_HEADERS,
        )
        assert route_resp.status_code == status.HTTP_200_OK
        assert route_resp.json()["pool_number"] == pool_number
        assert route_resp.json()["window_open"] is False

        resolve_resp = await client.get(
            "/v0/admin/whatsapp/resolve",
            params={"pool_number": pool_number, "sender": external},
            headers=ADMIN_HEADERS,
        )
        assert resolve_resp.status_code == status.HTTP_200_OK
        assert resolve_resp.json()["assistant_id"] == assistant_id
        assert resolve_resp.json()["role"] == "contact"

    async def test_delete_routes_then_resolve_404(
        self,
        client: AsyncClient,
        _test_assistant,
        dbsession: Session,
    ):
        """Delete routes → resolve returns 404."""
        assistant_id = _test_assistant["agent_id"]

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

        external = "+15553333333"
        await client.post(
            "/v0/admin/whatsapp/route",
            json={"assistant_id": assistant_id, "contact_number": external},
            headers=ADMIN_HEADERS,
        )

        del_resp = await client.delete(
            f"/v0/admin/whatsapp/routes?assistant_id={assistant_id}",
            headers=ADMIN_HEADERS,
        )
        assert del_resp.status_code == status.HTTP_200_OK
        assert del_resp.json()["deleted"] == 1

        resolve_resp = await client.get(
            "/v0/admin/whatsapp/resolve",
            params={"pool_number": pool_number, "sender": external},
            headers=ADMIN_HEADERS,
        )
        assert resolve_resp.status_code == status.HTTP_404_NOT_FOUND

    async def test_resolve_unknown_sender_404(self, client: AsyncClient):
        response = await client.get(
            "/v0/admin/whatsapp/resolve",
            params={"pool_number": "+18507877970", "sender": "+15550000000"},
            headers=ADMIN_HEADERS,
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND


# ============================================================================
# Group Q: General Success Paths (happy-path flows without conflicts)
# ============================================================================


class TestGeneralSuccessPaths:
    """Positive-path tests that verify the system works under normal operation."""

    def test_outbound_creates_route_and_inbound_resolves(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        """DAO round-trip: create outbound route → inbound from that contact resolves."""
        user = _make_user(dbsession, "roundtrip@test.com", "+15550700001")
        assistant = _make_assistant(dbsession, user, "RoundTrip")
        _enable_whatsapp(dbsession, assistant, pool_numbers[0])

        external = "+15550799999"
        route, resolution = dao.get_or_create_route(assistant.agent_id, external)
        assert resolution is None

        result = dao.resolve_inbound(pool_numbers[0].number, external)
        assert result is not None
        assert result["assistant_id"] == assistant.agent_id
        assert result["role"] == "contact"

    def test_one_assistant_multiple_contacts(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        """One assistant routes to several contacts; each inbound resolves correctly."""
        user = _make_user(dbsession, "multi@test.com", "+15550710001")
        assistant = _make_assistant(dbsession, user, "MultiBot")
        _enable_whatsapp(dbsession, assistant, pool_numbers[0])

        contacts = ["+15550711111", "+15550722222", "+15550733333"]
        for c in contacts:
            route, res = dao.get_or_create_route(assistant.agent_id, c)
            assert res is None
            assert route.contact_number == c
            assert route.assistant_id == assistant.agent_id

        for c in contacts:
            result = dao.resolve_inbound(pool_numbers[0].number, c)
            assert result["assistant_id"] == assistant.agent_id
            assert result["role"] == "contact"

    def test_org_member_inbound_routes_to_org_assistant(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        """Org member messages the pool → Tier 1 routes to org assistant."""
        owner = _make_user(dbsession, "orgowner@test.com", "+15550720001")
        member = _make_user(dbsession, "orgmember@test.com", "+15550720002")
        org = _make_org(dbsession, owner, "HappyOrg")
        _add_org_member(dbsession, org, member)

        assistant = _make_assistant(dbsession, owner, "OrgBot", org_id=org.id)
        _enable_whatsapp(dbsession, assistant, pool_numbers[0])

        result = dao.resolve_inbound(pool_numbers[0].number, "+15550720002")
        assert result is not None
        assert result["assistant_id"] == assistant.agent_id
        assert result["role"] == "owner"

    def test_two_users_shared_pool_route_isolation(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        """Two users share a pool, each routes to a different contact — no conflict, correct isolation."""
        u1 = _make_user(dbsession, "iso1@test.com", "+15550730001")
        u2 = _make_user(dbsession, "iso2@test.com", "+15550730002")
        a1 = _make_assistant(dbsession, u1, "Bot1")
        a2 = _make_assistant(dbsession, u2, "Bot2")
        _enable_whatsapp(dbsession, a1, pool_numbers[0])
        _enable_whatsapp(dbsession, a2, pool_numbers[0])

        contact_a = "+15550731111"
        contact_b = "+15550732222"

        r1, res1 = dao.get_or_create_route(a1.agent_id, contact_a)
        r2, res2 = dao.get_or_create_route(a2.agent_id, contact_b)
        assert res1 is None
        assert res2 is None

        result_a = dao.resolve_inbound(pool_numbers[0].number, contact_a)
        assert result_a["assistant_id"] == a1.agent_id
        assert result_a["role"] == "contact"

        result_b = dao.resolve_inbound(pool_numbers[0].number, contact_b)
        assert result_b["assistant_id"] == a2.agent_id
        assert result_b["role"] == "contact"

    def test_get_or_create_route_returns_complete_route(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        """Successful route creation returns a fully populated route object."""
        user = _make_user(dbsession, "complete@test.com", "+15550740001")
        assistant = _make_assistant(dbsession, user, "CompleteBot")
        _enable_whatsapp(dbsession, assistant, pool_numbers[0])

        external = "+15550749999"
        route, resolution = dao.get_or_create_route(assistant.agent_id, external)

        assert resolution is None
        assert route.id is not None
        assert route.pool_number_id == pool_numbers[0].id
        assert route.contact_number == external
        assert route.assistant_id == assistant.agent_id
        assert route.pool_number.number == pool_numbers[0].number


class TestFullLifecycleAPI:
    """API-level end-to-end: pool provisioning → message exchange → window check."""

    @pytest.fixture
    async def _test_user(self, client: AsyncClient):
        return await create_test_user(client, "lifecycle@test.com")

    @pytest.fixture
    async def _test_assistant(self, _test_user, dbsession: Session):
        assistant = Assistant(user_id=_test_user["id"], first_name="LifecycleBot")
        dbsession.add(assistant)
        dbsession.commit()
        return {"agent_id": assistant.agent_id}

    async def test_full_lifecycle_pool_to_window(
        self,
        client: AsyncClient,
        _test_assistant,
        dbsession: Session,
    ):
        """Add pool → assign → create route → resolve inbound → verify window opens."""
        assistant_id = _test_assistant["agent_id"]

        assign_resp = await client.post(
            "/v0/admin/whatsapp/assign",
            json={"assistant_id": assistant_id},
            headers=ADMIN_HEADERS,
        )
        assert assign_resp.status_code == status.HTTP_200_OK
        pool_number = assign_resp.json()["pool_number"]

        contact_dao = AssistantContactDAO(dbsession)
        contact_dao.upsert_assistant_contact(
            assistant_id=assistant_id,
            contact_type="whatsapp",
            contact_value=pool_number,
        )
        dbsession.commit()

        external = "+15550750001"
        route_resp = await client.post(
            "/v0/admin/whatsapp/route",
            json={"assistant_id": assistant_id, "contact_number": external},
            headers=ADMIN_HEADERS,
        )
        assert route_resp.status_code == status.HTTP_200_OK
        assert route_resp.json()["window_open"] is False
        assert route_resp.json()["conflict_resolved"] is False

        resolve_resp = await client.get(
            "/v0/admin/whatsapp/resolve",
            params={"pool_number": pool_number, "sender": external},
            headers=ADMIN_HEADERS,
        )
        assert resolve_resp.status_code == status.HTTP_200_OK
        assert resolve_resp.json()["assistant_id"] == assistant_id
        assert resolve_resp.json()["role"] == "contact"

        route_resp2 = await client.post(
            "/v0/admin/whatsapp/route",
            json={"assistant_id": assistant_id, "contact_number": external},
            headers=ADMIN_HEADERS,
        )
        assert route_resp2.status_code == status.HTTP_200_OK
        assert route_resp2.json()["window_open"] is True


# ============================================================================
# Group Q: Identity-Claim Route Cleanup
# ============================================================================


class TestRouteCleanupOnIdentityClaim:
    """When an external contact number is claimed as a platform user identity,
    all existing Tier 2 routes targeting that number must be removed.

    Numbers passed to UserDAO.create/update must pass libphonenumber validation,
    so we use valid US numbers (650 area code) for the whatsapp_number parameter.
    Route contact_numbers bypass validation (direct DB), so they can be anything.
    We use the same number for both to test the cleanup path.
    """

    def test_create_user_cleans_stale_routes(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        """user_dao.create() with whatsapp_number deletes routes for that number."""
        from orchestra.db.dao.user_dao import UserDAO

        owner = _make_user(dbsession, "ic_owner@test.com", "+15550901111")
        assistant = _make_assistant(dbsession, owner, "ICBot")
        _enable_whatsapp(dbsession, assistant, pool_numbers[0])

        external = "+16502530101"
        dao.get_or_create_route(assistant.agent_id, external)
        dbsession.flush()

        assert len(dao.get_routes_for_assistant(assistant.agent_id)) == 1

        user_dao = UserDAO(dbsession)
        user_dao.create(email="newcomer@test.com", whatsapp_number=external)
        dbsession.flush()

        assert len(dao.get_routes_for_assistant(assistant.agent_id)) == 0

    def test_update_user_cleans_stale_routes(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        """user_dao.update() with a new whatsapp_number deletes routes for that number."""
        from orchestra.db.dao.user_dao import UserDAO

        owner = _make_user(dbsession, "ic_owner2@test.com", "+15550903333")
        assistant = _make_assistant(dbsession, owner, "ICBot2")
        _enable_whatsapp(dbsession, assistant, pool_numbers[0])

        external = "+16502530102"
        dao.get_or_create_route(assistant.agent_id, external)
        dbsession.flush()

        bystander = _make_user(dbsession, "bystander@test.com")
        assert bystander.whatsapp_number is None

        user_dao = UserDAO(dbsession)
        user_dao.update(bystander.id, whatsapp_number=external)

        assert len(dao.get_routes_for_assistant(assistant.agent_id)) == 0

    def test_cleanup_across_multiple_pools(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        """Routes on different pools are all cleaned when a number is claimed."""
        from orchestra.db.dao.user_dao import UserDAO

        u1 = _make_user(dbsession, "ic_mp1@test.com", "+15550905555")
        u2 = _make_user(dbsession, "ic_mp2@test.com", "+15550906666")
        a1 = _make_assistant(dbsession, u1, "MP1")
        a2 = _make_assistant(dbsession, u2, "MP2")
        _enable_whatsapp(dbsession, a1, pool_numbers[0])
        _enable_whatsapp(dbsession, a2, pool_numbers[1])

        external = "+16502530103"
        dao.get_or_create_route(a1.agent_id, external)
        dao.get_or_create_route(a2.agent_id, external)
        dbsession.flush()

        assert len(dao.get_routes_for_assistant(a1.agent_id)) == 1
        assert len(dao.get_routes_for_assistant(a2.agent_id)) == 1

        user_dao = UserDAO(dbsession)
        user_dao.create(email="claimer@test.com", whatsapp_number=external)
        dbsession.flush()

        assert len(dao.get_routes_for_assistant(a1.agent_id)) == 0
        assert len(dao.get_routes_for_assistant(a2.agent_id)) == 0

    def test_resolve_inbound_after_identity_claim(
        self,
        dbsession: Session,
        dao: SharedPoolDAO,
        pool_numbers,
    ):
        """After identity claim, inbound no longer routes to the old assistant."""
        from orchestra.db.dao.user_dao import UserDAO

        owner = _make_user(dbsession, "ic_resolve@test.com", "+15550908888")
        assistant = _make_assistant(dbsession, owner, "ResBot")
        _enable_whatsapp(dbsession, assistant, pool_numbers[0])

        external = "+16502530104"
        dao.get_or_create_route(assistant.agent_id, external)
        dbsession.flush()

        result_before = dao.resolve_inbound(pool_numbers[0].number, external)
        assert result_before is not None
        assert result_before["assistant_id"] == assistant.agent_id

        user_dao = UserDAO(dbsession)
        user_dao.create(email="claimed@test.com", whatsapp_number=external)
        dbsession.flush()

        result_after = dao.resolve_inbound(pool_numbers[0].number, external)
        assert result_after is None

    def test_cleanup_noop_when_no_routes(self, dbsession: Session):
        """Setting whatsapp_number with no existing routes doesn't error."""
        from orchestra.db.dao.user_dao import UserDAO

        user_dao = UserDAO(dbsession)
        user = user_dao.create(
            email="noop_claim@test.com",
            whatsapp_number="+16502530105",
        )
        dbsession.flush()
        assert user.whatsapp_number == "+16502530105"
