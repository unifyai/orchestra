"""Burner-cluster detection for Console-only free credits.

Closing the API to never-paid accounts moves farming rather than ending
it: the raw-API channel an earlier sweep keyed on becomes unreachable,
and what continues does so through the Console, where it looks like
ordinary use.

The whole design rests on refusing to act on single-account signals, so
most of these tests are about what the sweep must *not* freeze.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from orchestra.db.models.enums import RECHARGE_TYPE_PAYMENT
from orchestra.db.models.orchestra_models import (
    CreditTransaction,
    Recharge,
    RechargeStatus,
)
from orchestra.routines.card_gate_sweep import freeze_burner_clusters
from orchestra.settings import settings
from orchestra.tests.test_billing.conftest import (
    make_org_with_billing,
    make_user,
    make_user_with_billing,
)


@pytest.fixture
def small_cluster(monkeypatch):
    """Drop the cluster threshold so fixtures stay legible."""
    monkeypatch.setattr(settings, "burner_cluster_min_accounts", 3)
    return settings


def _spend(dbsession, ba, amount: float, *, assistant_id: int | None = 1):
    """Post an llm debit, leaving the wallet drained."""
    dbsession.add(
        CreditTransaction(
            billing_account_id=ba.id,
            amount=Decimal(str(-amount)),
            category="llm",
            assistant_id=assistant_id,
        ),
    )
    ba.credits = Decimal("0")
    dbsession.flush()


def _make_farmed_account(dbsession, uid: str, *, ip: str, spend: float = 50.0):
    """A never-paid account that drained its grant from a given origin."""
    user, ba = make_user_with_billing(dbsession, uid, credits=0)
    user.signup_ip = ip
    user.created_at = datetime.utcnow()
    dbsession.flush()
    _spend(dbsession, ba, spend)
    return user, ba


def _make_comped_account(dbsession, uid: str, *, ip: str, org_name: str):
    """A drained never-paid account whose org holds a free-trial grant.

    The grant lives on the *organization*, so the account only reads as
    comped once a user points at the org's billing account — which is
    what puts it in the sweep's origin clusters in the first place.
    """
    org, ba = make_org_with_billing(dbsession, org_name, None, credits=Decimal("0"))
    org.free_trial = True
    user = make_user(dbsession, uid, ba)
    user.signup_ip = ip
    user.created_at = datetime.utcnow()
    dbsession.flush()
    _spend(dbsession, ba, 50.0)
    return user, ba


def test_lone_drained_account_is_not_frozen(dbsession, small_cluster):
    """One account draining its grant fast is an evaluator, not a farm.

    This is the false positive that matters most: freezing an
    enthusiastic first session is worse than missing a farmer.
    """
    _make_farmed_account(dbsession, "burner_lone", ip="203.0.113.10")

    result = freeze_burner_clusters(dbsession, dry_run=True)

    assert result.frozen == 0


def test_cluster_below_threshold_is_not_frozen(dbsession, small_cluster):
    for i in range(2):
        _make_farmed_account(dbsession, f"burner_small_{i}", ip="203.0.113.20")

    result = freeze_burner_clusters(dbsession, dry_run=True)

    assert result.frozen == 0


def test_cluster_at_threshold_is_flagged(dbsession, small_cluster):
    made = [
        _make_farmed_account(dbsession, f"burner_ring_{i}", ip="203.0.113.30")
        for i in range(3)
    ]

    result = freeze_burner_clusters(dbsession, dry_run=True)

    assert result.frozen == 3
    assert {ba.id for _u, ba in made} == set(result.billing_account_ids)


def test_dry_run_does_not_suspend(dbsession, small_cluster):
    made = [
        _make_farmed_account(dbsession, f"burner_dry_{i}", ip="203.0.113.40")
        for i in range(3)
    ]

    freeze_burner_clusters(dbsession, dry_run=True)

    assert all(ba.account_status == "ACTIVE" for _u, ba in made)


def test_live_run_suspends_with_the_shared_reason(dbsession, small_cluster):
    made = [
        _make_farmed_account(dbsession, f"burner_live_{i}", ip="203.0.113.50")
        for i in range(3)
    ]

    freeze_burner_clusters(dbsession, dry_run=False)

    for _u, ba in made:
        assert ba.account_status == "SUSPENDED"
        assert ba.suspension_reason == "abuse_fingerprint"


def test_shared_origin_without_drained_grants_is_ignored(
    dbsession,
    small_cluster,
):
    """An office signing up together is a cluster, and entirely innocent.

    Origin alone must never be evidence — only origin plus every member
    having extracted and exhausted a grant.
    """
    for i in range(4):
        user, ba = make_user_with_billing(dbsession, f"office_{i}", credits=100)
        user.signup_ip = "203.0.113.60"
        user.created_at = datetime.utcnow()
        dbsession.flush()

    result = freeze_burner_clusters(dbsession, dry_run=True)

    assert result.frozen == 0


def test_accounts_without_provenance_are_not_clustered(
    dbsession,
    small_cluster,
):
    """Missing data must not become a cluster of its own.

    Every pre-existing account has a NULL signup_ip; grouping on that
    would freeze the entire back catalogue on the first run.
    """
    for i in range(4):
        _make_farmed_account(dbsession, f"noprov_{i}", ip=None)

    result = freeze_burner_clusters(dbsession, dry_run=True)

    assert result.frozen == 0


def test_paid_accounts_are_exempt(dbsession, small_cluster):
    """Payment history clears an account regardless of who it signed up with.

    A real customer whose colleagues also signed up from the office and
    also ran their credits down must never be swept up.
    """
    for i in range(3):
        _user, ba = _make_farmed_account(
            dbsession,
            f"paid_ring_{i}",
            ip="203.0.113.70",
        )
        dbsession.add(
            Recharge(
                billing_account_id=ba.id,
                quantity=Decimal("50"),
                amount_usd=Decimal("50"),
                type=RECHARGE_TYPE_PAYMENT,
                status=RechargeStatus.PAID,
            ),
        )
    dbsession.flush()

    result = freeze_burner_clusters(dbsession, dry_run=True)

    assert result.frozen == 0


def test_comped_account_in_a_cluster_is_not_frozen(dbsession, small_cluster):
    """An admin-granted free trial clears an account, like payment does.

    Comping an org is a deliberate commercial decision, and it outranks
    a correlation that is only ever circumstantial.
    """
    farmed = [
        _make_farmed_account(dbsession, f"comped_ring_{i}", ip="203.0.113.90")
        for i in range(3)
    ]
    _user, comped_ba = _make_comped_account(
        dbsession,
        "comped_ring_member",
        ip="203.0.113.90",
        org_name="bc comped member",
    )

    result = freeze_burner_clusters(dbsession, dry_run=True)

    assert comped_ba.id not in result.billing_account_ids
    assert {ba.id for _u, ba in farmed} == set(result.billing_account_ids)


def test_comped_account_is_not_cluster_evidence(dbsession, small_cluster):
    """The grant withdraws the account as evidence, not just as a target.

    Two farmed accounts plus a comped one is not a ring of three. A
    comped org must never be what tips its neighbours over the
    threshold, or the grant would quietly endanger them.
    """
    for i in range(2):
        _make_farmed_account(dbsession, f"comped_thresh_{i}", ip="203.0.113.100")
    _make_comped_account(
        dbsession,
        "comped_thresh_member",
        ip="203.0.113.100",
        org_name="bc comped threshold",
    )

    result = freeze_burner_clusters(dbsession, dry_run=True)

    assert result.frozen == 0


def test_signups_outside_the_window_are_not_clustered(
    dbsession,
    small_cluster,
    monkeypatch,
):
    monkeypatch.setattr(settings, "burner_cluster_window_days", 1)
    for i in range(3):
        user, ba = _make_farmed_account(dbsession, f"old_ring_{i}", ip="203.0.113.80")
        user.created_at = datetime.utcnow() - timedelta(days=30)
    dbsession.flush()

    result = freeze_burner_clusters(dbsession, dry_run=True)

    assert result.frozen == 0


def test_user_agent_hash_clusters_across_changing_ips(
    dbsession,
    small_cluster,
):
    """Rotating IPs is cheap; rotating the whole browser fingerprint is less so."""
    made = []
    for i in range(3):
        user, ba = _make_farmed_account(
            dbsession,
            f"ua_ring_{i}",
            ip=f"198.51.100.{i}",
        )
        user.signup_user_agent_hash = "shared-ua-digest"
        made.append(ba)
    dbsession.flush()

    result = freeze_burner_clusters(dbsession, dry_run=True)

    assert result.frozen == 3


# ---------------------------------------------------------------------------
# Run diagnostics
# ---------------------------------------------------------------------------
#
# A nightly zero is what this sweep reports almost every night, and the
# only thing anyone reads. These pin that the zero carries enough with it
# to tell "looked, found nothing" apart from "could not look" -- the
# ambiguity that let the previous abuse signature sit dead and unnoticed.


def test_a_quiet_run_reports_how_close_it_came(dbsession, small_cluster):
    """Threshold minus one is a very different zero from nothing at all."""
    for i in range(2):
        _make_farmed_account(dbsession, f"diag_near_{i}", ip="203.0.113.110")

    result = freeze_burner_clusters(dbsession, dry_run=True)

    assert result.frozen == 0
    assert result.considered == 2
    assert result.largest_cluster == 2
    assert result.threshold == 3
    assert result.without_provenance == 0


def test_a_blind_run_says_so_rather_than_reporting_all_clear(
    dbsession,
    small_cluster,
):
    """Unreadable candidates must not read as an absence of farming.

    If provenance capture regresses, every account arrives without an
    origin and the sweep freezes nothing -- indistinguishable from a
    healthy quiet night unless the run says which one it was.
    """
    for i in range(4):
        _make_farmed_account(dbsession, f"diag_blind_{i}", ip=None)

    result = freeze_burner_clusters(dbsession, dry_run=True)

    assert result.frozen == 0
    assert result.considered == 4
    assert result.without_provenance == 4
    assert "blind" in result.note


def test_a_healthy_quiet_run_is_not_labelled_blind(dbsession, small_cluster):
    """The warning has to stay rare or it stops being read."""
    _make_farmed_account(dbsession, "diag_seen", ip="203.0.113.120")

    result = freeze_burner_clusters(dbsession, dry_run=True)

    assert result.frozen == 0
    assert result.note == ""


def test_partial_provenance_still_clusters_what_it_can(dbsession, small_cluster):
    """One unreadable signup must not disarm the run for the others."""
    for i in range(3):
        _make_farmed_account(dbsession, f"diag_partial_{i}", ip="203.0.113.130")
    _make_farmed_account(dbsession, "diag_partial_blank", ip=None)

    result = freeze_burner_clusters(dbsession, dry_run=True)

    assert result.frozen == 3
    assert result.without_provenance == 1
    assert result.note == ""


# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------
#
# The sweep locks people out on circumstantial evidence, unattended, at
# half past five in the morning. Whether anyone hears about it is part of
# the control, not a nicety.


@pytest.fixture
def discord(monkeypatch):
    """Capture what would be posted to the billing webhook."""
    from orchestra.routines import billing_notifications

    monkeypatch.setenv(billing_notifications.WEBHOOK_URL_ENV, "https://discord.test")
    sent: list[dict] = []
    monkeypatch.setattr(
        billing_notifications,
        "_send_webhook",
        lambda url, content="", embeds=None: sent.append(
            {"content": content, "embeds": embeds or []},
        )
        or True,
    )
    return sent


def _notify(result):
    from orchestra.routines.billing_notifications import notify_burner_cluster_sweep

    return notify_burner_cluster_sweep(result)


def test_a_freeze_is_announced(dbsession, small_cluster, discord):
    for i in range(3):
        _make_farmed_account(dbsession, f"alert_ring_{i}", ip="203.0.113.140")

    result = freeze_burner_clusters(dbsession, dry_run=False)
    _notify(result)

    assert len(discord) == 1
    assert "3 account(s) suspended" in discord[0]["content"]


def test_an_ordinary_quiet_run_stays_silent(dbsession, small_cluster, discord):
    """Nightly noise is how an alert stops being read."""
    _make_farmed_account(dbsession, "alert_quiet", ip="203.0.113.150")

    _notify(freeze_burner_clusters(dbsession, dry_run=False))

    assert discord == []


def test_a_dry_run_never_pages(dbsession, small_cluster, discord):
    """Reporting-only means reporting-only; nobody is locked out yet."""
    for i in range(3):
        _make_farmed_account(dbsession, f"alert_dry_{i}", ip="203.0.113.160")

    _notify(freeze_burner_clusters(dbsession, dry_run=True))

    assert discord == []


def test_a_blind_run_is_announced_as_blind(dbsession, small_cluster, discord):
    """A control that has quietly gone deaf must not pass for a calm one."""
    for i in range(3):
        _make_farmed_account(dbsession, f"alert_blind_{i}", ip=None)

    _notify(freeze_burner_clusters(dbsession, dry_run=False))

    assert len(discord) == 1
    assert "blind" in discord[0]["content"].lower()


# ---------------------------------------------------------------------------
# Provenance capture
# ---------------------------------------------------------------------------


def _fake_request(headers: dict, client_host: str | None = None):
    from types import SimpleNamespace

    return SimpleNamespace(
        headers=headers,
        client=SimpleNamespace(host=client_host) if client_host else None,
    )


def test_forwarded_for_wins_over_the_proxy_hop():
    """Behind Cloud Run, request.client.host is the load balancer."""
    from orchestra.web.api.utils.signup_provenance import client_ip

    request = _fake_request(
        {"x-forwarded-for": "198.51.100.7, 10.0.0.1"},
        client_host="10.0.0.1",
    )

    assert client_ip(request) == "198.51.100.7"


def test_missing_origin_is_none_not_a_placeholder():
    """A placeholder would cluster every such signup into fake evidence."""
    from orchestra.web.api.utils.signup_provenance import client_ip

    assert client_ip(_fake_request({})) is None


def test_user_agent_is_hashed_not_stored():
    from orchestra.web.api.utils.signup_provenance import user_agent_hash

    raw = "Mozilla/5.0 (X11; Linux x86_64) HeadlessChrome/120"
    digest = user_agent_hash(_fake_request({"user-agent": raw}))

    assert digest is not None
    assert raw not in digest
    assert len(digest) == 64


def test_identical_user_agents_hash_alike():
    """Equality is the only comparison the sweep makes, so it must hold."""
    from orchestra.web.api.utils.signup_provenance import user_agent_hash

    raw = "Mozilla/5.0 (X11; Linux x86_64) HeadlessChrome/120"
    assert user_agent_hash(_fake_request({"user-agent": raw})) == user_agent_hash(
        _fake_request({"user-agent": raw}),
    )


def test_provenance_always_returns_both_keys():
    """Callers splat this into UserDAO.create unconditionally."""
    from orchestra.web.api.utils.signup_provenance import signup_provenance

    assert signup_provenance(None) == {
        "signup_ip": None,
        "signup_user_agent_hash": None,
    }
    assert set(signup_provenance(_fake_request({})).keys()) == {
        "signup_ip",
        "signup_user_agent_hash",
    }


def test_overlong_forwarded_header_is_bounded():
    """A hostile header must not bloat the stored row."""
    from orchestra.web.api.utils.signup_provenance import client_ip

    request = _fake_request({"x-forwarded-for": "9" * 500})

    assert len(client_ip(request)) <= 45
