"""The workflow request dispatch endpoint.

- POST /v0/admin/workflows/requests/dispatch

Console has already written the durable request row by the time it calls this,
so the only question here is whether the wake is attempted and — more
importantly — that a wake which cannot be delivered is reported honestly
instead of failing the caller. Getting that backwards would make Console show
an error for a change that is going to apply anyway.
"""

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.tests.utils import ADMIN_HEADERS

ENDPOINT = "/v0/admin/workflows/requests/dispatch"

BODY = {
    "assistant_id": 7,
    "request_id": "req-abc123",
    "slug": "daily_briefing",
    "action": "install",
    "destination": "personal",
}


@pytest.mark.anyio
async def test_a_wake_that_cannot_be_delivered_is_reported_not_raised(
    client: AsyncClient,
    dbsession: Session,
):
    """No Adapters URL in tests, so the post fails — which is the interesting
    path. The request is durable and the assistant's boot sweep drains the same
    queue, so the honest answer is "recorded, not woken" with a 200."""
    response = await client.post(ENDPOINT, json=BODY, headers=ADMIN_HEADERS)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["request_id"] == "req-abc123"
    assert body["dispatched"] is False
    assert "next time the assistant wakes" in body["detail"]


@pytest.mark.anyio
async def test_an_unknown_action_is_refused_before_any_wake(
    client: AsyncClient,
    dbsession: Session,
):
    """A typo must 422 naming the allowed actions rather than waking an
    assistant to settle a request it cannot carry out."""
    response = await client.post(
        ENDPOINT,
        json={**BODY, "action": "frobnicate"},
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert "install" in response.text


@pytest.mark.anyio
async def test_identifiers_are_required(client: AsyncClient, dbsession: Session):
    """The row is addressed by request_id alone, so an empty one is unusable."""
    response = await client.post(
        ENDPOINT,
        json={**BODY, "request_id": ""},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


@pytest.mark.anyio
async def test_the_endpoint_is_admin_only(client: AsyncClient, dbsession: Session):
    """Waking an arbitrary assistant is not something a user key may do."""
    response = await client.post(ENDPOINT, json=BODY)
    assert response.status_code in (
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
    )
