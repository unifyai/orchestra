"""Card-gate exemption for orgs on an admin-granted free trial.

``Organization.free_trial`` is toggled by the admin endpoints in
``organization/views.py``. These tests pin the three places the flag has
to be honoured so a comped white-glove account is not gated, throttled,
or frozen out from under the customer.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from orchestra.lib.trial_subscription import (
    has_free_trial_grant,
    has_platform_access,
    trial_gate_fields,
)
from orchestra.routines.card_gate_sweep import freeze_never_paid_accounts
from orchestra.settings import settings
from orchestra.tests.test_billing.conftest import make_org_with_billing


@pytest.fixture
def card_gate_on(monkeypatch):
    """Card gate enabled."""
    monkeypatch.setattr(settings, "require_card_on_file", True)
    return settings


def test_grant_absent_by_default(dbsession):
    org, ba = make_org_with_billing(dbsession, "ft gate default", None)

    assert org.free_trial is False
    assert has_free_trial_grant(dbsession, ba.id) is False


def test_grant_detected_once_flag_set(dbsession):
    org, ba = make_org_with_billing(dbsession, "ft gate flagged", None)

    org.free_trial = True
    dbsession.flush()

    assert has_free_trial_grant(dbsession, ba.id) is True


def test_platform_access_denied_without_grant(dbsession, card_gate_on):
    """A never-paid, unsubscribed org is gated while the flag is off."""
    _, ba = make_org_with_billing(dbsession, "ft gate denied", None)

    assert has_platform_access(dbsession, ba) is False


def test_platform_access_granted_by_free_trial(dbsession, card_gate_on):
    org, ba = make_org_with_billing(dbsession, "ft gate allowed", None)

    org.free_trial = True
    dbsession.flush()

    assert has_platform_access(dbsession, ba) is True


def test_revoking_grant_reapplies_gate(dbsession, card_gate_on):
    """The grant is revocable — turning it off re-gates immediately."""
    org, ba = make_org_with_billing(dbsession, "ft gate revoked", None)

    org.free_trial = True
    dbsession.flush()
    assert has_platform_access(dbsession, ba) is True

    org.free_trial = False
    dbsession.flush()
    assert has_platform_access(dbsession, ba) is False


def test_daily_cap_applies_without_grant(dbsession, card_gate_on, monkeypatch):
    monkeypatch.setattr(settings, "trial_daily_spend_cap", 25.0)
    _, ba = make_org_with_billing(dbsession, "ft cap applied", None)

    fields = trial_gate_fields(dbsession, ba)

    assert fields["trial_daily_cap"] == 25.0


def test_daily_cap_lifted_by_free_trial(dbsession, card_gate_on, monkeypatch):
    """A comped evaluation is not throttled to a $25/day ceiling."""
    monkeypatch.setattr(settings, "trial_daily_spend_cap", 25.0)
    org, ba = make_org_with_billing(dbsession, "ft cap lifted", None)

    org.free_trial = True
    dbsession.flush()

    fields = trial_gate_fields(dbsession, ba)

    assert fields["trial_daily_cap"] is None
    assert fields["trial_daily_spend"] is None


def test_freeze_sweep_skips_free_trial_org(dbsession, card_gate_on):
    comped_org, comped_ba = make_org_with_billing(
        dbsession,
        "ft sweep comped",
        None,
        credits=Decimal("5000"),
    )
    _, plain_ba = make_org_with_billing(dbsession, "ft sweep plain", None)

    comped_org.free_trial = True
    dbsession.flush()

    result = freeze_never_paid_accounts(dbsession, dry_run=True)

    assert comped_ba.id not in result.billing_account_ids
    assert plain_ba.id in result.billing_account_ids


def test_freeze_sweep_leaves_free_trial_account_active(dbsession, card_gate_on):
    comped_org, comped_ba = make_org_with_billing(dbsession, "ft sweep live", None)
    comped_org.free_trial = True
    dbsession.flush()

    freeze_never_paid_accounts(dbsession, dry_run=False)

    dbsession.refresh(comped_ba)
    assert comped_ba.account_status == "ACTIVE"
    assert comped_ba.suspension_reason is None
