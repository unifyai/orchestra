"""
Auth rate limiting — what the limit is keyed on, and when it is recorded.

Both properties here were absent once and the same two bugs followed.

The address arrives from Console because Orchestra cannot see it: every
auth call reaches it through Console's server, so the address on the
request is Console's egress, identical for every person on the platform.
Keyed on that, the signup velocity limits stopped being a limit on a
caller and became a limit on everyone — thirty signups a day between
them.

And an attempt is counted before it is recorded. Recording first let a
rejected attempt extend the window it was then measured against, so once
the limit closed, the retries it provoked held it closed. Signup was down
for a day.
"""

from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from orchestra.web.api.utils.auth_rate_limiting import (
    enforce_auth_rate_limit,
    subnet_of,
)

_SETTINGS_PATH = "orchestra.web.api.utils.auth_rate_limiting.settings"


@pytest.fixture
def enforced():
    """Run the limits for real — they are skipped on staging and dev."""
    with patch(_SETTINGS_PATH) as settings:
        settings.is_staging = False
        settings.environment = "production"
        yield


def attempt(session: Session, client_ip, category="auth_register", **kwargs):
    enforce_auth_rate_limit(
        session,
        client_ip,
        category,
        max_attempts=kwargs.pop("max_attempts", 3),
        **kwargs,
    )


# ═══════════════════════════════════════════════════════════════════════════
# What the limit is keyed on
# ═══════════════════════════════════════════════════════════════════════════


def test_addresses_get_independent_budgets(dbsession: Session, enforced):
    """One caller exhausting a limit must not spend anyone else's."""
    for _ in range(3):
        attempt(dbsession, "198.51.100.7")

    with pytest.raises(HTTPException) as exhausted:
        attempt(dbsession, "198.51.100.7")
    assert exhausted.value.status_code == 429

    # A different signer, mid-outage, is unaffected.
    attempt(dbsession, "203.0.113.9")


def test_subnet_limit_holds_across_a_rotating_allocation(
    dbsession: Session,
    enforced,
):
    """Addresses within one /24 share a budget; a neighbouring /24 does not."""
    for octet in range(1, 4):
        attempt(dbsession, f"198.51.100.{octet}", use_subnet=True)

    with pytest.raises(HTTPException):
        attempt(dbsession, "198.51.100.4", use_subnet=True)

    attempt(dbsession, "203.0.113.4", use_subnet=True)


def test_identifier_still_separates_callers_without_an_address(
    dbsession: Session,
    enforced,
):
    """A missing address must not collapse two people onto one counter."""
    for _ in range(3):
        attempt(dbsession, None, identifier="someone@example.com")

    with pytest.raises(HTTPException):
        attempt(dbsession, None, identifier="someone@example.com")

    attempt(dbsession, None, identifier="somebody-else@example.com")


def test_velocity_limit_is_skipped_when_nothing_can_key_it(
    dbsession: Session,
    enforced,
):
    """
    With no address and no identifier there is nothing to key on, and a
    shared key would put the whole platform behind one counter. Console
    always sends an address, so this is a bug there; Orchestra logs it
    rather than throttling everyone on each other's behalf.
    """
    for _ in range(10):
        attempt(dbsession, None)


# ═══════════════════════════════════════════════════════════════════════════
# When the attempt is recorded
# ═══════════════════════════════════════════════════════════════════════════


def test_rejected_attempts_do_not_extend_the_window(dbsession: Session, enforced):
    """
    A caller retrying against a closed limit must not push its window
    forward. Counting after recording meant the limit stayed shut for as
    long as anyone kept trying, which is how a day-long window never
    reopened.
    """
    for _ in range(3):
        attempt(dbsession, "198.51.100.7")

    for _ in range(20):
        with pytest.raises(HTTPException):
            attempt(dbsession, "198.51.100.7")

    # The rejections left no trace, so the stored count is the three
    # attempts that were actually served.
    from sqlalchemy import func, select

    from orchestra.db.models.orchestra_models import AuthRateLimitEntry

    total = dbsession.execute(
        select(func.coalesce(func.sum(AuthRateLimitEntry.attempt_count), 0)).where(
            AuthRateLimitEntry.key == "198.51.100.7",
            AuthRateLimitEntry.endpoint_category == "auth_register",
        ),
    ).scalar()
    assert total == 3


def test_budget_is_exactly_max_attempts(dbsession: Session, enforced):
    for _ in range(3):
        attempt(dbsession, "198.51.100.7")

    with pytest.raises(HTTPException):
        attempt(dbsession, "198.51.100.7")


# ═══════════════════════════════════════════════════════════════════════════
# Subnet collapsing
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("ip", "expected"),
    [
        ("198.51.100.7", "198.51.100.0/24"),
        ("2001:db8:abcd:1234::1", "2001:db8:abcd::/48"),
    ],
)
def test_subnet_of(ip, expected):
    assert subnet_of(ip) == expected
