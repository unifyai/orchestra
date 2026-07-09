"""Tests for managed Computer Use billing and API endpoints."""

from decimal import Decimal

import pytest
from fastapi import status

from orchestra.db.dao.assistant_contact_dao import AssistantContactDAO
from orchestra.db.models.orchestra_models import Assistant, AssistantContactCost
from orchestra.services.managed_desktop_service import (
    MANAGED_DESKTOP_MODES,
    managed_desktop_entitled,
)


@pytest.fixture(autouse=True)
def seed_managed_desktop_costs(dbsession):
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


def test_create_assistant_without_desktop_mode_skips_charge(
    client,
    dbsession,
    billing_user,
):
    user, api_key = billing_user
    response = client.post(
        "/v0/assistant",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "first_name": "No",
            "surname": "Desktop",
            "desktop_mode": None,
            "create_infra": False,
        },
    )
    assert response.status_code == status.HTTP_200_OK
    assistant = response.json()["info"]
    assert assistant["desktop_mode"] is None
    assert assistant.get("managed_desktop_status") is None


def test_enable_managed_desktop_endpoint_charges_and_sets_status(
    client,
    dbsession,
    billing_user,
):
    user, api_key = billing_user
    assistant = Assistant(
        user_id=user.id,
        first_name="Ada",
        surname="Test",
    )
    dbsession.add(assistant)
    dbsession.flush()

    response = client.post(
        f"/v0/assistant/{assistant.agent_id}/managed-desktop",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"desktop_mode": "ubuntu"},
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()["info"]
    assert body["desktop_mode"] == "ubuntu"
    assert body["managed_desktop_status"] == "active"
    assert body["managed_desktop_monthly_cost"] == 50.0

    contact_dao = AssistantContactDAO(dbsession)
    cost = contact_dao.get_contact_monthly_cost("managed_desktop", provider="ubuntu")
    assert cost == Decimal("50.00")
    assert "ubuntu" in MANAGED_DESKTOP_MODES
