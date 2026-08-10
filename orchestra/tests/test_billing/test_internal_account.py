"""Scope of the internal-account trial exemption.

``is_internal_account`` requires *two* signals: membership of the literal
Unify organization **and** a verified ``@unify.ai`` mailbox on the
owner/member in question. Neither alone is sufficient:

- The org *name* is user-choosable, so a customer who names their own org
  "Unify" must not thereby become internal — hence the domain check.
- The *domain* alone is not enough either: staff own customer orgs during
  white-glove onboarding, and an owner-domain-only test silently exempted
  those customers from the card gate and the daily burn ceiling — hence
  the membership check.
"""

from __future__ import annotations

import pytest

from orchestra.lib.trial_subscription import has_platform_access, is_internal_account
from orchestra.services.personal_workspace_service import UNIFY_ORGANIZATION_NAME
from orchestra.settings import settings
from orchestra.tests.test_billing.conftest import (
    make_billing_account,
    make_org,
    make_org_with_billing,
    make_user_with_billing,
)


@pytest.fixture
def card_gate_on(monkeypatch):
    monkeypatch.setattr(settings, "require_card_on_file", True)
    return settings


def _unify_org(dbsession, *, owner_email):
    """Create the literal Unify org owned by a user with *owner_email*."""
    return make_org_with_billing(
        dbsession,
        UNIFY_ORGANIZATION_NAME,
        None,
        owner_email=owner_email,
    )


def _add_to_unify_org(dbsession, unify_org, user):
    from orchestra.db.dao.role_dao import RoleDAO
    from orchestra.db.models.orchestra_models import OrganizationMember

    role = RoleDAO(dbsession).get_by_name("Member", organization_id=None)
    dbsession.add(
        OrganizationMember(
            organization_id=unify_org.id,
            user_id=user.id,
            role_id=role.id,
        ),
    )
    dbsession.flush()


def _staff_owned_customer_org(dbsession, name):
    """A customer org whose owner is a member of the Unify org."""
    unify_org, _ = _unify_org(dbsession, owner_email="founder@unify.ai")
    staff, _ = make_user_with_billing(
        dbsession,
        f"staff_{name.replace(' ', '_').lower()}",
        email="dan@unify.ai",
    )
    _add_to_unify_org(dbsession, unify_org, staff)

    customer_ba = make_billing_account(dbsession)
    customer_org = make_org(dbsession, staff, customer_ba, name=name)
    return customer_org, customer_ba


def test_unify_org_account_is_internal(dbsession):
    """The genuine Unify org — staff-owned mailbox — is internal."""
    _, ba = _unify_org(dbsession, owner_email="founder@unify.ai")

    assert is_internal_account(dbsession, ba.id) is True


def test_squatted_unify_org_name_is_not_internal(dbsession):
    """An org merely *named* "Unify" by a customer is not internal.

    This is the anti-squatting guard: the name is user-choosable, so the
    owner must also hold a unify.ai mailbox for the org to count.
    """
    _, ba = _unify_org(dbsession, owner_email="squatter@example.com")

    assert is_internal_account(dbsession, ba.id) is False


def test_customer_org_owned_by_staff_is_not_internal(dbsession):
    """The regression: a staff-owned customer org is a customer, not staff."""
    _, customer_ba = _staff_owned_customer_org(dbsession, "Customer Co")

    assert is_internal_account(dbsession, customer_ba.id) is False


def test_staff_personal_account_is_internal(dbsession):
    """Internal environments run off staff personal accounts — keep those."""
    unify_org, _ = _unify_org(dbsession, owner_email="founder@unify.ai")
    staff, staff_ba = make_user_with_billing(
        dbsession,
        "staff_personal",
        email="dan@unify.ai",
    )
    _add_to_unify_org(dbsession, unify_org, staff)

    assert is_internal_account(dbsession, staff_ba.id) is True


def test_non_staff_member_of_unify_org_is_not_internal(dbsession):
    """A member of the Unify org without a unify.ai mailbox is not internal.

    Guards the squatting variant where an outsider is added as a member of
    an org named "Unify": membership without the domain confers nothing.
    """
    unify_org, _ = _unify_org(dbsession, owner_email="founder@unify.ai")
    outsider, outsider_ba = make_user_with_billing(
        dbsession,
        "member_outsider",
        email="outsider@example.com",
    )
    _add_to_unify_org(dbsession, unify_org, outsider)

    assert is_internal_account(dbsession, outsider_ba.id) is False


def test_unify_domain_alone_does_not_confer_internal(dbsession):
    """An @unify.ai address with no Unify membership is not internal."""
    _unify_org(dbsession, owner_email="founder@unify.ai")
    _, outsider_ba = make_user_with_billing(
        dbsession,
        "domain_only",
        email="contractor@unify.ai",
    )

    assert is_internal_account(dbsession, outsider_ba.id) is False


def test_no_unify_org_means_nothing_is_internal(dbsession):
    _, ba = make_user_with_billing(dbsession, "no_unify_org", email="x@unify.ai")

    assert is_internal_account(dbsession, ba.id) is False


def test_staff_owned_customer_org_is_card_gated(dbsession, card_gate_on):
    """End-to-end: the accidental exemption no longer grants access, and the
    explicit free-trial grant is what restores it."""
    customer_org, customer_ba = _staff_owned_customer_org(dbsession, "Gated Customer")

    assert has_platform_access(dbsession, customer_ba) is False

    customer_org.free_trial = True
    dbsession.flush()

    assert has_platform_access(dbsession, customer_ba) is True
