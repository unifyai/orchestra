"""Tests for managed Computer Use billing and API endpoints."""

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.db.dao.assistant_contact_dao import AssistantContactDAO
from orchestra.db.dao.billing_account_dao import BillingAccountDAO
from orchestra.db.models.orchestra_models import (
    Assistant,
    AssistantContactCost,
    AssistantExternalIP,
    AssistantExternalIPHistory,
    User,
)
from orchestra.services.managed_desktop_service import (
    MANAGED_DESKTOP_MODES,
    managed_desktop_entitled,
)
from orchestra.tests.utils import create_test_user, get_credits


@pytest.fixture(autouse=True)
def seed_managed_desktop_costs(dbsession: Session):
    existing = (
        dbsession.query(AssistantContactCost)
        .filter(AssistantContactCost.contact_type == "managed_desktop")
        .count()
    )
    if existing == 0:
        dbsession.add_all(
            [
                AssistantContactCost(
                    contact_type="managed_desktop",
                    provider="ubuntu",
                    country_code=None,
                    monthly_cost=Decimal("50.00"),
                    one_time_cost=Decimal("0.00"),
                ),
                AssistantContactCost(
                    contact_type="managed_desktop",
                    provider="windows",
                    country_code=None,
                    monthly_cost=Decimal("75.00"),
                    one_time_cost=Decimal("0.00"),
                ),
            ],
        )
        dbsession.flush()
    yield


@pytest.fixture(autouse=True)
def enable_billing_charges(monkeypatch):
    """Exercise the credit pre-check / deduction path in CI."""
    monkeypatch.setenv("MANUAL_TOPUP", "1")


@pytest.fixture(autouse=True)
def mock_managed_desktop_reawaken():
    with patch(
        "orchestra.web.api.utils.assistant_infra.reawaken_assistant",
        new_callable=AsyncMock,
    ) as mock_reawaken, patch(
        "orchestra.web.api.assistant.views.wake_up_assistant",
        new_callable=AsyncMock,
    ) as mock_wake_up:
        mock_reawaken.return_value = None
        mock_wake_up.return_value = type("Resp", (), {"status_code": 200})()
        yield mock_reawaken


def _fund_user(dbsession: Session, user_id: str, amount: Decimal) -> None:
    user = dbsession.get(User, user_id)
    assert user is not None and user.billing_account_id is not None
    BillingAccountDAO(dbsession).add_credits(
        user.billing_account_id,
        float(amount),
        category="test_fund",
        description="Test funding for managed desktop",
    )
    dbsession.commit()


def test_managed_desktop_entitled_requires_active_status():
    assistant = Assistant(
        agent_id=1,
        user_id="u1",
        desktop_mode="ubuntu",
        managed_desktop_status="active",
    )
    assert managed_desktop_entitled(assistant) is True

    assistant.managed_desktop_status = "grace_period"
    assert managed_desktop_entitled(assistant) is False

    assistant.managed_desktop_status = "active"
    assistant.desktop_mode = None
    assert managed_desktop_entitled(assistant) is False


@pytest.mark.anyio
async def test_create_assistant_without_desktop_mode_skips_charge(
    client: AsyncClient,
):
    user = await create_test_user(client, "no-desktop@example.com")
    response = await client.post(
        "/v0/assistant",
        headers=user["headers"],
        json={
            "first_name": "No",
            "surname": "Desktop",
            "desktop_mode": None,
            "create_infra": False,
            "is_local": True,
        },
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    assistant = response.json()["info"]
    assert assistant["desktop_mode"] is None
    assert assistant.get("managed_desktop_status") is None


@pytest.mark.anyio
async def test_create_assistant_with_desktop_mode_charges_and_activates(
    client: AsyncClient,
    dbsession: Session,
):
    user = await create_test_user(client, "hire-desktop@example.com")
    _fund_user(dbsession, user["id"], Decimal("100.00"))
    credits_before = await get_credits(client, user_headers=user["headers"])

    response = await client.post(
        "/v0/assistant",
        headers=user["headers"],
        json={
            "first_name": "Hire",
            "surname": "Desktop",
            "desktop_mode": "ubuntu",
            "create_infra": False,
            "is_local": True,
        },
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    assistant = response.json()["info"]
    assert assistant["desktop_mode"] == "ubuntu"
    assert assistant["managed_desktop_status"] == "active"
    assert assistant["managed_desktop_monthly_cost"] == 50.0

    credits_after = await get_credits(client, user_headers=user["headers"])
    assert credits_after == credits_before - 50.0


@pytest.mark.anyio
async def test_enable_managed_desktop_endpoint_charges_and_sets_status(
    client: AsyncClient,
    dbsession: Session,
):
    user = await create_test_user(client, "enable-desktop@example.com")
    _fund_user(dbsession, user["id"], Decimal("100.00"))

    create_resp = await client.post(
        "/v0/assistant",
        headers=user["headers"],
        json={
            "first_name": "Ada",
            "surname": "Test",
            "desktop_mode": None,
            "create_infra": False,
            "is_local": True,
        },
    )
    assert create_resp.status_code == status.HTTP_200_OK, create_resp.text
    agent_id = create_resp.json()["info"]["agent_id"]
    credits_before = await get_credits(client, user_headers=user["headers"])

    response = await client.post(
        f"/v0/assistant/{agent_id}/managed-desktop",
        headers=user["headers"],
        json={"desktop_mode": "ubuntu"},
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()["info"]
    assert body["desktop_mode"] == "ubuntu"
    assert body["managed_desktop_status"] == "active"
    assert body["managed_desktop_monthly_cost"] == 50.0

    credits_after = await get_credits(client, user_headers=user["headers"])
    assert credits_after == credits_before - 50.0

    contact_dao = AssistantContactDAO(dbsession)
    cost = contact_dao.get_contact_monthly_cost("managed_desktop", provider="ubuntu")
    assert cost == Decimal("50.00")
    assert "ubuntu" in MANAGED_DESKTOP_MODES

    status_response = await client.get(
        f"/v0/assistant/{agent_id}/managed-desktop",
        headers=user["headers"],
    )
    assert status_response.status_code == status.HTTP_200_OK, status_response.text
    network_identity = status_response.json()["info"]["network_identity"]
    assert network_identity == {
        "gcp_address_name": None,
        "address": None,
        "region": None,
        "hostname": None,
        "state": "pending",
        "active_operation": "reserve",
    }

    disable_response = await client.delete(
        f"/v0/assistant/{agent_id}/managed-desktop",
        headers=user["headers"],
    )
    assert disable_response.status_code == status.HTTP_200_OK, disable_response.text
    external_ip = (
        dbsession.query(AssistantExternalIP)
        .filter(AssistantExternalIP.assistant_id == agent_id)
        .one()
    )
    assert external_ip.state == "retained"
    assert external_ip.active_operation is None
    assert (
        dbsession.query(AssistantExternalIPHistory)
        .filter(AssistantExternalIPHistory.external_ip_id == external_ip.id)
        .count()
        == 2
    )

    _fund_user(dbsession, user["id"], Decimal("50.00"))
    reenable_response = await client.post(
        f"/v0/assistant/{agent_id}/managed-desktop",
        headers=user["headers"],
        json={"desktop_mode": "ubuntu"},
    )
    assert reenable_response.status_code == status.HTTP_200_OK, reenable_response.text
    dbsession.expire_all()
    reused_external_ip = (
        dbsession.query(AssistantExternalIP)
        .filter(AssistantExternalIP.assistant_id == agent_id)
        .one()
    )
    assert reused_external_ip.id == external_ip.id
    assert reused_external_ip.state == "pending"
    assert reused_external_ip.active_operation == "reserve"
