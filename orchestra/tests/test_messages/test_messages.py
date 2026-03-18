from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.tests.utils import ADMIN_HEADERS, HEADERS, create_test_user


@pytest.fixture(autouse=True)
def mock_assistant_infra_calls(request):
    """Mock assistant infrastructure to prevent real network calls."""
    with patch(
        "orchestra.web.api.assistant.views.wake_up_assistant",
        new_callable=AsyncMock,
    ) as mock_wake_up, patch(
        "orchestra.web.api.assistant.views.reawaken_assistant",
        new_callable=AsyncMock,
    ) as mock_reawaken:
        mock_wake_up.return_value = MagicMock(status_code=200)
        mock_reawaken.return_value = MagicMock(status_code=200, json=lambda: {})
        yield


@pytest.fixture(autouse=True)
def mock_adapter_dispatch():
    """Mock the adapter dispatch to prevent real HTTP calls to Communication."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    with patch(
        "orchestra.web.api.messages.views._dispatch_to_adapters",
        new_callable=AsyncMock,
    ) as mock_dispatch:
        yield mock_dispatch


@pytest.fixture
async def assistant_id(client: AsyncClient) -> int:
    """Create a test assistant and return its ID."""
    payload = {
        "first_name": "TestBot",
        "surname": "API",
        "create_infra": False,
    }
    resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert resp.status_code == status.HTTP_200_OK
    return int(resp.json()["info"]["agent_id"])


# ─── POST /v0/messages ───


@pytest.mark.anyio
async def test_send_message_success(client: AsyncClient, assistant_id: int):
    resp = await client.post(
        "/v0/messages",
        json={"assistant_id": assistant_id, "message": "Hello assistant"},
        headers=HEADERS,
    )
    assert resp.status_code == status.HTTP_201_CREATED
    body = resp.json()["info"]
    assert len(body["message_id"]) == 36  # UUID
    assert body["assistant_id"] == assistant_id
    assert body["message"] == "Hello assistant"
    assert body["status"] == "processing"
    assert body["response"] is None
    assert body["created_at"] is not None
    assert body["completed_at"] is None


@pytest.mark.anyio
async def test_send_message_dispatches_to_adapter(
    client: AsyncClient,
    assistant_id: int,
    mock_adapter_dispatch: AsyncMock,
):
    resp = await client.post(
        "/v0/messages",
        json={"assistant_id": assistant_id, "message": "Check dispatch"},
        headers=HEADERS,
    )
    assert resp.status_code == status.HTTP_201_CREATED
    mock_adapter_dispatch.assert_called_once_with(
        assistant_id=assistant_id,
        api_message_id=resp.json()["info"]["message_id"],
        body="Check dispatch",
        deploy_env="production",
        attachments=[],
        tags=[],
    )


@pytest.mark.anyio
async def test_send_message_nonexistent_assistant(client: AsyncClient):
    resp = await client.post(
        "/v0/messages",
        json={"assistant_id": 999999, "message": "Hello"},
        headers=HEADERS,
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert "Assistant not found" in resp.json()["detail"]


@pytest.mark.anyio
async def test_send_message_empty_message(client: AsyncClient, assistant_id: int):
    resp = await client.post(
        "/v0/messages",
        json={"assistant_id": assistant_id, "message": ""},
        headers=HEADERS,
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


@pytest.mark.anyio
async def test_send_message_missing_fields(client: AsyncClient):
    resp = await client.post(
        "/v0/messages",
        json={},
        headers=HEADERS,
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


@pytest.mark.anyio
async def test_send_message_no_auth(client: AsyncClient, assistant_id: int):
    resp = await client.post(
        "/v0/messages",
        json={"assistant_id": assistant_id, "message": "Hello"},
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN


# ─── GET /v0/messages/{message_id} ───


@pytest.mark.anyio
async def test_poll_message_processing(client: AsyncClient, assistant_id: int):
    send_resp = await client.post(
        "/v0/messages",
        json={"assistant_id": assistant_id, "message": "What's up?"},
        headers=HEADERS,
    )
    message_id = send_resp.json()["info"]["message_id"]

    poll_resp = await client.get(
        f"/v0/messages/{message_id}",
        headers=HEADERS,
    )
    assert poll_resp.status_code == status.HTTP_200_OK
    body = poll_resp.json()["info"]
    assert body["message_id"] == message_id
    assert body["assistant_id"] == assistant_id
    assert body["message"] == "What's up?"
    assert body["status"] == "processing"
    assert body["response"] is None
    assert body["created_at"] is not None
    assert body["completed_at"] is None


@pytest.mark.anyio
async def test_poll_nonexistent_message(client: AsyncClient):
    resp = await client.get(
        "/v0/messages/00000000-0000-0000-0000-000000000000",
        headers=HEADERS,
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.anyio
async def test_poll_message_no_auth(client: AsyncClient, assistant_id: int):
    send_resp = await client.post(
        "/v0/messages",
        json={"assistant_id": assistant_id, "message": "Hello"},
        headers=HEADERS,
    )
    message_id = send_resp.json()["info"]["message_id"]

    resp = await client.get(f"/v0/messages/{message_id}")
    assert resp.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.anyio
async def test_poll_message_wrong_user(client: AsyncClient, assistant_id: int):
    """A different user cannot poll for another user's message."""
    send_resp = await client.post(
        "/v0/messages",
        json={"assistant_id": assistant_id, "message": "Secret msg"},
        headers=HEADERS,
    )
    message_id = send_resp.json()["info"]["message_id"]

    other_user = await create_test_user(client, "other_api_user@test.com")
    poll_resp = await client.get(
        f"/v0/messages/{message_id}",
        headers=other_user["headers"],
    )
    assert poll_resp.status_code == status.HTTP_404_NOT_FOUND


# ─── PUT /v0/admin/messages/{message_id}/complete ───


@pytest.mark.anyio
async def test_complete_message_with_response(
    client: AsyncClient,
    assistant_id: int,
):
    send_resp = await client.post(
        "/v0/messages",
        json={"assistant_id": assistant_id, "message": "Add milk to shopping list"},
        headers=HEADERS,
    )
    message_id = send_resp.json()["info"]["message_id"]

    complete_resp = await client.put(
        f"/v0/admin/messages/{message_id}/complete",
        json={"response": "Done! I've added milk to your shopping list."},
        headers=ADMIN_HEADERS,
    )
    assert complete_resp.status_code == status.HTTP_200_OK
    body = complete_resp.json()["info"]
    assert body["status"] == "completed"
    assert body["response"] == "Done! I've added milk to your shopping list."
    assert body["completed_at"] is not None

    # Polling should now return the completed state
    poll_resp = await client.get(
        f"/v0/messages/{message_id}",
        headers=HEADERS,
    )
    poll_body = poll_resp.json()["info"]
    assert poll_body["status"] == "completed"
    assert poll_body["response"] == "Done! I've added milk to your shopping list."


@pytest.mark.anyio
async def test_complete_message_without_response(
    client: AsyncClient,
    assistant_id: int,
):
    """Assistant processes the message but chooses not to respond on this channel."""
    send_resp = await client.post(
        "/v0/messages",
        json={"assistant_id": assistant_id, "message": "Note this for later"},
        headers=HEADERS,
    )
    message_id = send_resp.json()["info"]["message_id"]

    complete_resp = await client.put(
        f"/v0/admin/messages/{message_id}/complete",
        json={"response": None},
        headers=ADMIN_HEADERS,
    )
    assert complete_resp.status_code == status.HTTP_200_OK
    body = complete_resp.json()["info"]
    assert body["status"] == "completed"
    assert body["response"] is None
    assert body["completed_at"] is not None


@pytest.mark.anyio
async def test_complete_nonexistent_message(client: AsyncClient):
    resp = await client.put(
        "/v0/admin/messages/00000000-0000-0000-0000-000000000000/complete",
        json={"response": "Hello"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.anyio
async def test_complete_message_no_admin_auth(
    client: AsyncClient,
    assistant_id: int,
):
    """Non-admin users cannot complete messages."""
    send_resp = await client.post(
        "/v0/messages",
        json={"assistant_id": assistant_id, "message": "Hello"},
        headers=HEADERS,
    )
    message_id = send_resp.json()["info"]["message_id"]

    resp = await client.put(
        f"/v0/admin/messages/{message_id}/complete",
        json={"response": "Hacked!"},
        headers=HEADERS,
    )
    # Non-admin users should be rejected
    assert resp.status_code in (
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
    )


# ─── Full lifecycle ───


@pytest.mark.anyio
async def test_full_lifecycle(client: AsyncClient, assistant_id: int):
    """Test the complete send → poll → complete → poll lifecycle."""
    # 1. Send
    send_resp = await client.post(
        "/v0/messages",
        json={"assistant_id": assistant_id, "message": "What is 2+2?"},
        headers=HEADERS,
    )
    assert send_resp.status_code == status.HTTP_201_CREATED
    message_id = send_resp.json()["info"]["message_id"]

    # 2. Poll (processing)
    poll_resp = await client.get(f"/v0/messages/{message_id}", headers=HEADERS)
    assert poll_resp.json()["info"]["status"] == "processing"
    assert poll_resp.json()["info"]["response"] is None

    # 3. Complete (admin)
    complete_resp = await client.put(
        f"/v0/admin/messages/{message_id}/complete",
        json={"response": "4"},
        headers=ADMIN_HEADERS,
    )
    assert complete_resp.status_code == status.HTTP_200_OK

    # 4. Poll (completed)
    poll_resp = await client.get(f"/v0/messages/{message_id}", headers=HEADERS)
    info = poll_resp.json()["info"]
    assert info["status"] == "completed"
    assert info["response"] == "4"
    assert info["completed_at"] is not None
    assert info["message"] == "What is 2+2?"
    assert info["assistant_id"] == assistant_id


@pytest.mark.anyio
async def test_multiple_messages_independent(
    client: AsyncClient,
    assistant_id: int,
):
    """Multiple messages to the same assistant are independent."""
    ids = []
    for msg in ["First", "Second", "Third"]:
        resp = await client.post(
            "/v0/messages",
            json={"assistant_id": assistant_id, "message": msg},
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_201_CREATED
        ids.append(resp.json()["info"]["message_id"])

    # All unique
    assert len(set(ids)) == 3

    # Complete only the second one
    await client.put(
        f"/v0/admin/messages/{ids[1]}/complete",
        json={"response": "Ack second"},
        headers=ADMIN_HEADERS,
    )

    # First and third are still processing
    for idx in [0, 2]:
        resp = await client.get(f"/v0/messages/{ids[idx]}", headers=HEADERS)
        assert resp.json()["info"]["status"] == "processing"

    # Second is completed
    resp = await client.get(f"/v0/messages/{ids[1]}", headers=HEADERS)
    assert resp.json()["info"]["status"] == "completed"
    assert resp.json()["info"]["response"] == "Ack second"


# ─── Tags and Attachments ───


SAMPLE_ATTACHMENT = {
    "id": "att-uuid-001",
    "filename": "report.pdf",
    "gs_url": "gs://bucket/assistant/att-uuid-001_report.pdf",
    "content_type": "application/pdf",
    "size_bytes": 12345,
}


@pytest.mark.anyio
async def test_send_message_with_tags(client: AsyncClient, assistant_id: int):
    resp = await client.post(
        "/v0/messages",
        json={
            "assistant_id": assistant_id,
            "message": "Tagged message",
            "tags": ["source:slack", "channel:#general"],
        },
        headers=HEADERS,
    )
    assert resp.status_code == status.HTTP_201_CREATED
    body = resp.json()["info"]
    assert body["tags"] == ["source:slack", "channel:#general"]
    assert body["attachments"] == []

    poll_resp = await client.get(f"/v0/messages/{body['message_id']}", headers=HEADERS)
    assert poll_resp.json()["info"]["tags"] == ["source:slack", "channel:#general"]


@pytest.mark.anyio
async def test_send_message_with_attachments(client: AsyncClient, assistant_id: int):
    resp = await client.post(
        "/v0/messages",
        json={
            "assistant_id": assistant_id,
            "message": "See attached",
            "attachments": [SAMPLE_ATTACHMENT],
        },
        headers=HEADERS,
    )
    assert resp.status_code == status.HTTP_201_CREATED
    body = resp.json()["info"]
    assert len(body["attachments"]) == 1
    assert body["attachments"][0]["filename"] == "report.pdf"
    assert body["attachments"][0]["gs_url"] == SAMPLE_ATTACHMENT["gs_url"]


@pytest.mark.anyio
async def test_send_message_dispatches_tags_and_attachments(
    client: AsyncClient,
    assistant_id: int,
    mock_adapter_dispatch: AsyncMock,
):
    resp = await client.post(
        "/v0/messages",
        json={
            "assistant_id": assistant_id,
            "message": "With extras",
            "tags": ["env:prod"],
            "attachments": [SAMPLE_ATTACHMENT],
        },
        headers=HEADERS,
    )
    assert resp.status_code == status.HTTP_201_CREATED
    call_kwargs = mock_adapter_dispatch.call_args.kwargs
    assert call_kwargs["tags"] == ["env:prod"]
    assert len(call_kwargs["attachments"]) == 1
    assert call_kwargs["attachments"][0]["filename"] == "report.pdf"


@pytest.mark.anyio
async def test_send_message_too_many_attachments(
    client: AsyncClient,
    assistant_id: int,
):
    attachments = [
        {
            "id": f"att-{i}",
            "filename": f"file{i}.txt",
            "gs_url": f"gs://bucket/path/{i}",
        }
        for i in range(11)
    ]
    resp = await client.post(
        "/v0/messages",
        json={
            "assistant_id": assistant_id,
            "message": "Too many",
            "attachments": attachments,
        },
        headers=HEADERS,
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "10" in resp.json()["detail"]


@pytest.mark.anyio
async def test_complete_message_with_tags_and_attachments(
    client: AsyncClient,
    assistant_id: int,
):
    send_resp = await client.post(
        "/v0/messages",
        json={
            "assistant_id": assistant_id,
            "message": "Process this",
            "tags": ["source:api"],
        },
        headers=HEADERS,
    )
    message_id = send_resp.json()["info"]["message_id"]

    complete_resp = await client.put(
        f"/v0/admin/messages/{message_id}/complete",
        json={
            "response": "Done!",
            "tags": ["source:api"],
            "attachments": [SAMPLE_ATTACHMENT],
        },
        headers=ADMIN_HEADERS,
    )
    assert complete_resp.status_code == status.HTTP_200_OK
    body = complete_resp.json()["info"]
    assert body["response_tags"] == ["source:api"]
    assert len(body["response_attachments"]) == 1
    assert body["response_attachments"][0]["filename"] == "report.pdf"

    poll_resp = await client.get(f"/v0/messages/{message_id}", headers=HEADERS)
    poll_body = poll_resp.json()["info"]
    assert poll_body["tags"] == ["source:api"]
    assert poll_body["response_tags"] == ["source:api"]
    assert len(poll_body["response_attachments"]) == 1


@pytest.mark.anyio
async def test_lifecycle_with_tags_and_attachments(
    client: AsyncClient,
    assistant_id: int,
):
    """Full lifecycle with tags and attachments on both inbound and outbound."""
    send_resp = await client.post(
        "/v0/messages",
        json={
            "assistant_id": assistant_id,
            "message": "Analyze this file",
            "tags": ["channel:webhook", "priority:high"],
            "attachments": [SAMPLE_ATTACHMENT],
        },
        headers=HEADERS,
    )
    assert send_resp.status_code == status.HTTP_201_CREATED
    message_id = send_resp.json()["info"]["message_id"]

    poll1 = await client.get(f"/v0/messages/{message_id}", headers=HEADERS)
    info1 = poll1.json()["info"]
    assert info1["status"] == "processing"
    assert info1["tags"] == ["channel:webhook", "priority:high"]
    assert len(info1["attachments"]) == 1
    assert info1["response_tags"] is None
    assert info1["response_attachments"] is None

    response_attachment = {
        "id": "resp-att-001",
        "filename": "analysis.xlsx",
        "gs_url": "gs://bucket/assistant/resp-att-001_analysis.xlsx",
        "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "size_bytes": 54321,
    }
    await client.put(
        f"/v0/admin/messages/{message_id}/complete",
        json={
            "response": "Analysis complete, see attached.",
            "tags": ["channel:webhook", "priority:high"],
            "attachments": [response_attachment],
        },
        headers=ADMIN_HEADERS,
    )

    poll2 = await client.get(f"/v0/messages/{message_id}", headers=HEADERS)
    info2 = poll2.json()["info"]
    assert info2["status"] == "completed"
    assert info2["response"] == "Analysis complete, see attached."
    assert info2["tags"] == ["channel:webhook", "priority:high"]
    assert info2["response_tags"] == ["channel:webhook", "priority:high"]
    assert len(info2["response_attachments"]) == 1
    assert info2["response_attachments"][0]["filename"] == "analysis.xlsx"


@pytest.mark.anyio
async def test_default_tags_and_attachments_are_empty(
    client: AsyncClient,
    assistant_id: int,
):
    """Messages sent without tags/attachments have sensible defaults."""
    resp = await client.post(
        "/v0/messages",
        json={"assistant_id": assistant_id, "message": "Plain message"},
        headers=HEADERS,
    )
    body = resp.json()["info"]
    assert body["tags"] == []
    assert body["attachments"] == []
    assert body["response_tags"] is None
    assert body["response_attachments"] is None


@pytest.mark.anyio
async def test_send_message_other_users_assistant(client: AsyncClient):
    """A user cannot send a message to another user's assistant."""
    # Create assistant with default user
    payload = {"first_name": "Private", "surname": "Bot", "create_infra": False}
    resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert resp.status_code == status.HTTP_200_OK
    aid = int(resp.json()["info"]["agent_id"])

    # Try to send from a different user
    other_user = await create_test_user(client, "intruder@test.com")
    resp = await client.post(
        "/v0/messages",
        json={"assistant_id": aid, "message": "Unauthorized"},
        headers=other_user["headers"],
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND
