"""Tests for the platform-mailbox teardown worker.

Covers the planning logic and the ``execute_plan`` happy / failure paths
without making real HTTP calls to the Communication service.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    Assistant,
    AssistantContact,
    AssistantSecret,
    BillingAccount,
    User,
)
from orchestra.workers.teardown_platform_mailboxes import (
    DOMAIN_TO_PROVIDER,
    TeardownPlan,
    _provider_for_domain,
    execute_plan,
    plan_teardown,
    select_target_rows,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_user(dbsession: Session) -> User:
    ba = BillingAccount(credits=Decimal("100"), account_status="ACTIVE")
    dbsession.add(ba)
    dbsession.flush()
    user = User(
        id=uuid.uuid4().hex,
        email=f"{uuid.uuid4().hex}@test.local",
        billing_account_id=ba.id,
    )
    dbsession.add(user)
    dbsession.flush()
    return user


def _make_assistant(dbsession: Session, user: User) -> Assistant:
    a = Assistant(user_id=user.id, first_name="TD", surname="Test")
    dbsession.add(a)
    dbsession.flush()
    return a


def _make_email_contact(
    dbsession: Session,
    *,
    assistant_id: int,
    contact_value: str,
    provider: str,
    provisioned_by: str,
    status: str = "active",
) -> AssistantContact:
    c = AssistantContact(
        assistant_id=assistant_id,
        contact_type="email",
        contact_value=contact_value,
        provider=provider,
        provisioned_by=provisioned_by,
        status=status,
    )
    dbsession.add(c)
    dbsession.flush()
    return c


def _make_secret(
    dbsession: Session,
    *,
    user_id: str,
    agent_id: int,
    name: str,
    value: str = "tok",
) -> AssistantSecret:
    s = AssistantSecret(
        user_id=user_id,
        agent_id=agent_id,
        secret_name=name,
        secret_value=value,
    )
    dbsession.add(s)
    dbsession.flush()
    return s


# ---------------------------------------------------------------------------
# _provider_for_domain
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("alice@unify.ai", "google_workspace"),
        ("bob@UnifyAILtd123.OnMicrosoft.COM", "microsoft_365"),
        ("user@gmail.com", None),
        ("malformed-no-at-sign", None),
        ("", None),
    ],
)
def test_provider_for_domain(value, expected):
    assert _provider_for_domain(value) == expected


def test_known_domains_table_is_complete():
    # If this list grows, the script will need a new entry in
    # DOMAIN_TO_PROVIDER, otherwise the deprovision call will fall back
    # to the (possibly mis-labelled) stored provider.
    assert set(DOMAIN_TO_PROVIDER) == {
        "unify.ai",
        "unifyailtd123.onmicrosoft.com",
    }


# ---------------------------------------------------------------------------
# select_target_rows
# ---------------------------------------------------------------------------


def test_select_target_rows_all_returns_only_platform_email(dbsession: Session):
    user = _make_user(dbsession)
    a1 = _make_assistant(dbsession, user)
    a2 = _make_assistant(dbsession, user)
    a3 = _make_assistant(dbsession, user)

    platform_gw = _make_email_contact(
        dbsession,
        assistant_id=a1.agent_id,
        contact_value="bot@unify.ai",
        provider="google_workspace",
        provisioned_by="platform",
    )
    platform_ms = _make_email_contact(
        dbsession,
        assistant_id=a2.agent_id,
        contact_value="bot@unifyailtd123.onmicrosoft.com",
        provider="microsoft_365",
        provisioned_by="platform",
    )
    _make_email_contact(
        dbsession,
        assistant_id=a3.agent_id,
        contact_value="user@gmail.com",
        provider="google_workspace",
        provisioned_by="user",  # BYOD — must be excluded
    )
    _make_email_contact(
        dbsession,
        assistant_id=a1.agent_id,
        contact_value="old@unify.ai",
        provider="google_workspace",
        provisioned_by="platform",
        status="deleted",  # already deleted — must be excluded
    )
    dbsession.commit()

    rows = select_target_rows(dbsession, contact_id=None, include_all=True)
    ids = {r.id for r in rows}
    assert ids == {platform_gw.id, platform_ms.id}


def test_select_target_rows_targeted_refuses_byod(dbsession: Session):
    user = _make_user(dbsession)
    a = _make_assistant(dbsession, user)
    byod = _make_email_contact(
        dbsession,
        assistant_id=a.agent_id,
        contact_value="me@gmail.com",
        provider="google_workspace",
        provisioned_by="user",
    )
    dbsession.commit()

    rows = select_target_rows(
        dbsession,
        contact_id=int(byod.id),
        include_all=False,
    )
    assert rows == []


def test_select_target_rows_requires_target(dbsession: Session):
    # Missing both --contact-id and --all should raise SystemExit
    # before any DB work happens.
    with pytest.raises(SystemExit):
        select_target_rows(dbsession, contact_id=None, include_all=False)


# ---------------------------------------------------------------------------
# plan_teardown
# ---------------------------------------------------------------------------


def test_plan_routes_by_domain_when_provider_mismatched(dbsession: Session):
    """A platform mailbox in the MS365 tenant but stored as
    ``google_workspace`` (the historical bug we found on staging) should
    be routed to the MS365 deprovision API based on its email domain."""
    user = _make_user(dbsession)
    a = _make_assistant(dbsession, user)
    bad = _make_email_contact(
        dbsession,
        assistant_id=a.agent_id,
        contact_value="ved@unifyailtd123.onmicrosoft.com",
        provider="google_workspace",  # wrong, but this is real data
        provisioned_by="platform",
    )
    dbsession.commit()

    plans = plan_teardown(dbsession, [bad], skip_secret_cleanup=False)
    assert len(plans) == 1
    plan = plans[0]
    assert plan.effective_provider == "microsoft_365"
    assert plan.provider_mismatch is True
    assert plan.stored_provider == "google_workspace"


def test_plan_cleans_microsoft_secrets_when_no_other_email(dbsession: Session):
    user = _make_user(dbsession)
    a = _make_assistant(dbsession, user)
    contact = _make_email_contact(
        dbsession,
        assistant_id=a.agent_id,
        contact_value="x@unifyailtd123.onmicrosoft.com",
        provider="microsoft_365",
        provisioned_by="platform",
    )
    for name in (
        "MICROSOFT_ACCESS_TOKEN",
        "MICROSOFT_REFRESH_TOKEN",
        "MICROSOFT_GRANTED_SCOPES",
        "GOOGLE_REFRESH_TOKEN",  # must be left alone
    ):
        _make_secret(dbsession, user_id=user.id, agent_id=a.agent_id, name=name)
    dbsession.commit()

    plans = plan_teardown(dbsession, [contact], skip_secret_cleanup=False)
    assert plans[0].will_clean_secrets is True
    assert plans[0].secrets_to_delete == [
        "MICROSOFT_ACCESS_TOKEN",
        "MICROSOFT_GRANTED_SCOPES",
        "MICROSOFT_REFRESH_TOKEN",
    ]


def test_plan_ignores_already_deleted_same_assistant_email(dbsession: Session):
    """A previously-deleted email contact on the same assistant must not
    block secret cleanup.

    (The DB's ``uq_assistant_contact_type_active`` partial unique index
    forbids two simultaneously-active email contacts on the same
    assistant, so the production-realistic version of the BYOD-overlap
    case is: an old soft-deleted row alongside the active platform
    mailbox.  That should not interfere with cleanup.)
    """
    user = _make_user(dbsession)
    a = _make_assistant(dbsession, user)
    _make_email_contact(
        dbsession,
        assistant_id=a.agent_id,
        contact_value="old@gmail.com",
        provider="google_workspace",
        provisioned_by="user",
        status="deleted",
    )
    contact = _make_email_contact(
        dbsession,
        assistant_id=a.agent_id,
        contact_value="x@unifyailtd123.onmicrosoft.com",
        provider="microsoft_365",
        provisioned_by="platform",
    )
    _make_secret(
        dbsession,
        user_id=user.id,
        agent_id=a.agent_id,
        name="MICROSOFT_ACCESS_TOKEN",
    )
    dbsession.commit()

    plans = plan_teardown(dbsession, [contact], skip_secret_cleanup=False)
    assert plans[0].will_clean_secrets is True
    assert plans[0].secrets_to_delete == ["MICROSOFT_ACCESS_TOKEN"]


def test_plan_skips_secret_cleanup_when_flag_set(dbsession: Session):
    user = _make_user(dbsession)
    a = _make_assistant(dbsession, user)
    contact = _make_email_contact(
        dbsession,
        assistant_id=a.agent_id,
        contact_value="x@unifyailtd123.onmicrosoft.com",
        provider="microsoft_365",
        provisioned_by="platform",
    )
    _make_secret(
        dbsession,
        user_id=user.id,
        agent_id=a.agent_id,
        name="MICROSOFT_ACCESS_TOKEN",
    )
    dbsession.commit()

    plans = plan_teardown(dbsession, [contact], skip_secret_cleanup=True)
    assert plans[0].will_clean_secrets is False
    assert plans[0].secrets_to_delete == []


def test_plan_never_proposes_google_workspace_secret_cleanup(dbsession: Session):
    """Google Workspace platform mailboxes use service-account
    delegation; there is no per-mailbox OAuth secret to clean up. Even
    if GOOGLE_* secrets exist on the assistant (they would be BYOD),
    the planner must never include them.
    """
    user = _make_user(dbsession)
    a = _make_assistant(dbsession, user)
    contact = _make_email_contact(
        dbsession,
        assistant_id=a.agent_id,
        contact_value="bot@unify.ai",
        provider="google_workspace",
        provisioned_by="platform",
    )
    _make_secret(
        dbsession,
        user_id=user.id,
        agent_id=a.agent_id,
        name="GOOGLE_REFRESH_TOKEN",
    )
    dbsession.commit()

    plans = plan_teardown(dbsession, [contact], skip_secret_cleanup=False)
    assert plans[0].will_clean_secrets is False
    assert plans[0].secrets_to_delete == []


# ---------------------------------------------------------------------------
# execute_plan
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_execute_plan_happy_path_microsoft(dbsession: Session):
    user = _make_user(dbsession)
    a = _make_assistant(dbsession, user)
    contact = _make_email_contact(
        dbsession,
        assistant_id=a.agent_id,
        contact_value="x@unifyailtd123.onmicrosoft.com",
        provider="microsoft_365",
        provisioned_by="platform",
    )
    _make_secret(
        dbsession,
        user_id=user.id,
        agent_id=a.agent_id,
        name="MICROSOFT_ACCESS_TOKEN",
    )
    dbsession.commit()

    plan = TeardownPlan(
        contact_id=int(contact.id),
        assistant_id=int(a.agent_id),
        contact_value=contact.contact_value,
        stored_provider="microsoft_365",
        effective_provider="microsoft_365",
        provider_mismatch=False,
        will_clean_secrets=True,
        secrets_to_delete=["MICROSOFT_ACCESS_TOKEN"],
    )

    with patch(
        "orchestra.workers.teardown_platform_mailboxes.delete_outlook_email",
        new_callable=AsyncMock,
        return_value={"success": True, "deleted": True},
    ) as mock_outlook, patch(
        "orchestra.workers.teardown_platform_mailboxes.delete_email",
        new_callable=AsyncMock,
    ) as mock_gmail:
        result = await execute_plan(dbsession, plan, deploy_env=None)

    mock_outlook.assert_awaited_once_with(
        "x@unifyailtd123.onmicrosoft.com",
        deploy_env=None,
    )
    mock_gmail.assert_not_called()
    assert result.deprovision_status == "ok"
    assert result.soft_delete_status == "ok"
    assert result.secrets_deleted == 1
    assert result.error is None

    dbsession.expire_all()
    refreshed = dbsession.get(AssistantContact, contact.id)
    assert refreshed.status == "deleted"
    assert refreshed.deleted_at is not None
    assert refreshed.contact_value == "x@unifyailtd123.onmicrosoft.com"

    remaining = (
        dbsession.query(AssistantSecret)
        .filter(AssistantSecret.agent_id == a.agent_id)
        .count()
    )
    assert remaining == 0


@pytest.mark.anyio
async def test_execute_plan_routes_mismatched_row_to_outlook(dbsession: Session):
    user = _make_user(dbsession)
    a = _make_assistant(dbsession, user)
    contact = _make_email_contact(
        dbsession,
        assistant_id=a.agent_id,
        contact_value="ved@unifyailtd123.onmicrosoft.com",
        provider="google_workspace",  # the historical mis-label
        provisioned_by="platform",
    )
    dbsession.commit()

    plans = plan_teardown(dbsession, [contact], skip_secret_cleanup=True)
    plan = plans[0]
    assert plan.effective_provider == "microsoft_365"

    with patch(
        "orchestra.workers.teardown_platform_mailboxes.delete_outlook_email",
        new_callable=AsyncMock,
        return_value={"success": True, "deleted": True},
    ) as mock_outlook, patch(
        "orchestra.workers.teardown_platform_mailboxes.delete_email",
        new_callable=AsyncMock,
    ) as mock_gmail:
        result = await execute_plan(dbsession, plan, deploy_env=None)

    mock_outlook.assert_awaited_once()
    mock_gmail.assert_not_called()
    assert result.deprovision_status == "ok"


@pytest.mark.anyio
async def test_execute_plan_skips_db_changes_when_deprovision_fails(
    dbsession: Session,
):
    user = _make_user(dbsession)
    a = _make_assistant(dbsession, user)
    contact = _make_email_contact(
        dbsession,
        assistant_id=a.agent_id,
        contact_value="bot@unify.ai",
        provider="google_workspace",
        provisioned_by="platform",
    )
    dbsession.commit()
    plan = plan_teardown(dbsession, [contact], skip_secret_cleanup=True)[0]

    with patch(
        "orchestra.workers.teardown_platform_mailboxes.delete_email",
        new_callable=AsyncMock,
        side_effect=RuntimeError("comms 502"),
    ):
        result = await execute_plan(dbsession, plan, deploy_env=None)

    assert result.deprovision_status == "error"
    assert "comms 502" in (result.error or "")
    assert result.soft_delete_status == "pending"

    dbsession.expire_all()
    refreshed = dbsession.get(AssistantContact, contact.id)
    assert refreshed.status == "active"  # unchanged
    assert refreshed.deleted_at is None
