"""Scope of the internal-account trial exemption.

``is_internal_account`` is anchored on membership of the literal Unify
organization. It must not key off the ``@unify.ai`` email domain: staff
own customer orgs during white-glove onboarding, and an owner-domain
test silently exempted those customers from the card gate and the daily
burn ceiling.
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
    unify_org, _ = make_org_with_billing(dbsession, UNIFY_ORGANIZATION_NAME, None)
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
    _, ba = make_org_with_billing(dbsession, UNIFY_ORGANIZATION_NAME, None)

    assert is_internal_account(dbsession, ba.id) is True


def test_customer_org_owned_by_staff_is_not_internal(dbsession):
    """The regression: a staff-owned customer org is a customer, not staff."""
    _, customer_ba = _staff_owned_customer_org(dbsession, "Customer Co")

    assert is_internal_account(dbsession, customer_ba.id) is False


def test_staff_personal_account_is_internal(dbsession):
    """Internal environments run off staff personal accounts — keep those."""
    unify_org, _ = make_org_with_billing(dbsession, UNIFY_ORGANIZATION_NAME, None)
    staff, staff_ba = make_user_with_billing(
        dbsession,
        "staff_personal",
        email="dan@unify.ai",
    )
    _add_to_unify_org(dbsession, unify_org, staff)

    assert is_internal_account(dbsession, staff_ba.id) is True


def test_unify_domain_alone_does_not_confer_internal(dbsession):
    """An @unify.ai address with no Unify membership is not internal."""
    make_org_with_billing(dbsession, UNIFY_ORGANIZATION_NAME, None)
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
