"""Console-only free credits.

Free credits are meant to be spent through the Console, where a human is
present and abuse is observable. These tests pin the two halves of that:
:func:`has_api_access` decides *whether* an account may spend off-platform,
and ``ApiKey.kind`` is what makes *where a request came from* knowable at
all — without it the Console and a curl are the same credential.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from orchestra.db.dao.api_key_dao import ApiKeyDAO
from orchestra.db.models.orchestra_models import CONSOLE_KEY_KIND, PROGRAMMATIC_KEY_KIND
from orchestra.lib.trial_subscription import (
    has_api_access,
    has_platform_access,
    trial_gate_fields,
)
from orchestra.settings import settings
from orchestra.tests.test_billing.conftest import (
    make_org_with_billing,
    make_user_with_billing,
)


@pytest.fixture
def api_gate_on(monkeypatch):
    """API payment-history gate enabled."""
    monkeypatch.setattr(settings, "require_api_payment_history", True)
    return settings


# ---------------------------------------------------------------------------
# has_api_access
# ---------------------------------------------------------------------------


def test_open_while_the_gate_is_off(dbsession):
    """Ships dark: nothing changes until the flag is turned on."""
    _, ba = make_org_with_billing(dbsession, "api gate off", None)

    assert has_api_access(dbsession, ba) is True


def test_never_paid_account_is_denied(dbsession, api_gate_on):
    _, ba = make_org_with_billing(dbsession, "api gate denied", None)

    assert has_api_access(dbsession, ba) is False


def test_subscription_opens_the_gate(dbsession, api_gate_on):
    _, ba = make_org_with_billing(dbsession, "api gate subbed", None)

    ba.stripe_subscription_id = "sub_test_123"
    dbsession.flush()

    assert has_api_access(dbsession, ba) is True


def test_free_trial_grant_opens_the_gate(dbsession, api_gate_on):
    """A comped evaluation is a deliberate decision; don't gate it."""
    org, ba = make_org_with_billing(dbsession, "api gate comped", None)

    org.free_trial = True
    dbsession.flush()

    assert has_api_access(dbsession, ba) is True


def test_grandfathered_account_keeps_access(dbsession, api_gate_on):
    """Backfilled for accounts already calling the API when this shipped.

    Without this the gate would cut off live integrations belonging to
    users who never did anything wrong.
    """
    _, ba = make_org_with_billing(dbsession, "api gate grandfathered", None)

    ba.api_access_grandfathered = True
    dbsession.flush()

    assert has_api_access(dbsession, ba) is True


def test_missing_billing_account_is_denied(dbsession, api_gate_on):
    """No account resolved means no payment history to rely on."""
    assert has_api_access(dbsession, None) is False


def test_gate_state_rides_the_spend_payload(dbsession, api_gate_on):
    """The runtime learns the verdict through the existing limit payload."""
    _, ba = make_org_with_billing(dbsession, "api gate payload", None)

    fields = trial_gate_fields(dbsession, ba)

    assert fields["api_access_allowed"] is False

    ba.api_access_grandfathered = True
    dbsession.flush()

    assert trial_gate_fields(dbsession, ba)["api_access_allowed"] is True


def test_balance_does_not_open_the_gate(dbsession, api_gate_on):
    """Gated on payment history, not on credits.

    A burner's advantage is that signup is free, so topping the wallet up
    with granted credits must not buy API access.
    """
    _, ba = make_org_with_billing(dbsession, "api gate balance", None)

    ba.credits = Decimal("500")
    dbsession.flush()

    assert has_api_access(dbsession, ba) is False


# ---------------------------------------------------------------------------
# Console keys are not user-held credentials
# ---------------------------------------------------------------------------


def test_console_key_is_minted_once_and_reused(dbsession):
    user, _ = make_user_with_billing(dbsession, "console_key_reuse")
    test_user_id = user.id
    dao = ApiKeyDAO(dbsession)

    first = dao.get_or_create_console_key(test_user_id)
    second = dao.get_or_create_console_key(test_user_id)

    assert first == second
    assert dao.get_console_key(test_user_id).kind == CONSOLE_KEY_KIND


def test_console_key_is_excluded_from_the_users_own_keys(dbsession):
    """The whole security property in one assertion.

    ``get_personal_keys`` backs both the Profile listing and every internal
    "act as this user" lookup. A Console key appearing in either would make
    it copyable, and a copyable Console key is just a programmatic one.
    """
    user, _ = make_user_with_billing(dbsession, "console_key_hidden")
    test_user_id = user.id
    dao = ApiKeyDAO(dbsession)
    dao.create(key="prog-visible-key", name="default", user_id=test_user_id)
    console_key = dao.get_or_create_console_key(test_user_id)
    dbsession.flush()

    listed = [row[0].key for row in dao.get_personal_keys(test_user_id)]

    assert console_key not in listed
    assert all(
        row[0].kind == PROGRAMMATIC_KEY_KIND
        for row in dao.get_personal_keys(test_user_id)
    )


def test_org_console_keys_are_per_workspace(dbsession):
    """The Console swaps to an org-scoped key inside an org workspace.

    That key has to be Console-kind too, or every org-context request
    would look programmatic and trip the gate.
    """
    org, _ = make_org_with_billing(dbsession, "api gate org keys", None)
    user, _ = make_user_with_billing(dbsession, "console_key_org")
    test_user_id = user.id
    dao = ApiKeyDAO(dbsession)

    personal = dao.get_or_create_console_key(test_user_id)
    scoped = dao.get_or_create_console_key(test_user_id, organization_id=org.id)

    assert personal != scoped
    assert dao.get_console_key(test_user_id, org.id).kind == CONSOLE_KEY_KIND
    listed = [row[0].key for row in dao.get_organization_keys(test_user_id)]
    assert scoped not in listed


# ---------------------------------------------------------------------------
# The runtime-starting endpoints' dependency
# ---------------------------------------------------------------------------


def _request_with(**state):
    """A stand-in Request carrying only what the dependency reads."""
    from types import SimpleNamespace

    return SimpleNamespace(state=SimpleNamespace(**state))


@pytest.fixture
def gate_reads_test_session(dbsession, monkeypatch):
    """Point the dependency's read-only session at the test transaction.

    The dependency opens its own AUTOCOMMIT connection in production, which
    cannot see uncommitted test data. Redirecting it lets these cases
    exercise the real function rather than a copy of its logic.
    """
    from contextlib import contextmanager

    from orchestra.web.api import dependencies

    @contextmanager
    def _session():
        yield dbsession

    monkeypatch.setattr(dependencies, "_ro_session", _session)


def test_dependency_allows_everything_while_the_gate_is_off(
    dbsession,
    gate_reads_test_session,
):
    from orchestra.web.api.dependencies import require_console_origin_for_free_accounts

    user, _ = make_user_with_billing(dbsession, "gate_dep_off")

    require_console_origin_for_free_accounts(
        _request_with(user_id=user.id, organization_id=None, key_kind="programmatic"),
    )


def test_dependency_blocks_a_programmatic_key_on_a_free_account(
    dbsession,
    api_gate_on,
    gate_reads_test_session,
):
    """The second door: starting a runtime is what causes the spend."""
    from fastapi import HTTPException

    from orchestra.web.api.dependencies import require_console_origin_for_free_accounts

    user, _ = make_user_with_billing(dbsession, "gate_dep_blocked")

    with pytest.raises(HTTPException) as ctx:
        require_console_origin_for_free_accounts(
            _request_with(
                user_id=user.id,
                organization_id=None,
                key_kind=PROGRAMMATIC_KEY_KIND,
            ),
        )

    assert ctx.value.status_code == 402
    assert "Unify Console" in ctx.value.detail


def test_dependency_lets_the_console_through_on_the_same_account(
    dbsession,
    api_gate_on,
    gate_reads_test_session,
):
    """Same free account, same endpoint — only the credential differs.

    This is the pair that shows ``kind`` is doing the work. If this test
    and the one above ever agree, the gate has stopped distinguishing
    anything and free users have lost the product.
    """
    from orchestra.web.api.dependencies import require_console_origin_for_free_accounts

    user, _ = make_user_with_billing(dbsession, "gate_dep_console")

    require_console_origin_for_free_accounts(
        _request_with(
            user_id=user.id,
            organization_id=None,
            key_kind=CONSOLE_KEY_KIND,
        ),
    )


def test_dependency_exempts_the_platform_system_key(
    dbsession,
    api_gate_on,
    gate_reads_test_session,
):
    from orchestra.web.api.dependencies import require_console_origin_for_free_accounts

    require_console_origin_for_free_accounts(
        _request_with(
            user_id="__system__",
            organization_id=None,
            key_kind=PROGRAMMATIC_KEY_KIND,
            is_system_api_key=True,
        ),
    )


# ---------------------------------------------------------------------------
# The Console-facing gate response
# ---------------------------------------------------------------------------


def test_access_gate_response_defaults_to_permissive():
    """The Console reads this to decide whether to warn about the API key.

    Defaulting the other way would have an older Orchestra build — which
    omits the field entirely — make the Console announce a restriction
    that is not actually in force.
    """
    from orchestra.web.api.billing.schema import AccessGateResponse

    assert AccessGateResponse(allowed=True).api_access_allowed is True


def test_access_gate_reports_the_api_verdict_independently(dbsession, api_gate_on):
    """Platform access and API access are separate questions.

    With the card gate off and the API gate on, an account is allowed to
    use the platform and denied off-platform — so the two fields must not
    be derived from one another.
    """
    from orchestra.settings import settings
    from orchestra.web.api.billing.schema import AccessGateResponse

    _, ba = make_org_with_billing(dbsession, "gate response split", None)
    settings.require_card_on_file = False

    response = AccessGateResponse(
        allowed=has_platform_access(dbsession, ba),
        api_access_allowed=has_api_access(dbsession, ba),
    )

    assert response.allowed is True
    assert response.api_access_allowed is False
