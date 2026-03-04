"""
Unit tests for _post_create_setup ordering guarantees.

These tests verify that log_pre_hire_chat runs AFTER PubSub topic creation and
assistant wakeup.  The adapter publishes to the assistant's PubSub topic, so
the topic must exist and Unity must be subscribed before the publish can succeed.

No database or FastAPI app is required — all external calls are mocked.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_log_pre_hire_chat_runs_after_pubsub_and_wakeup():
    """log_pre_hire_chat must execute after create_pubsub_topic and wake_up_assistant."""
    from orchestra.web.api.assistant.views import _post_create_setup

    order: list[str] = []

    async def track_create_pubsub(*a, **kw):
        order.append("create_pubsub_topic")
        return {"status": "ok"}

    async def track_wakeup(*a, **kw):
        resp = MagicMock()
        resp.status_code = 200
        order.append("wake_up_assistant")
        return resp

    async def track_log_chat(*a, **kw):
        order.append("log_pre_hire_chat")
        return {"status": "success"}

    assistant_in = SimpleNamespace(
        create_infra=True,
        email=None,
        user_phone=None,
        user_whatsapp_number=None,
        desktop_mode=None,
        pre_hire_chat=[
            SimpleNamespace(role="user", msg="Hello"),
            SimpleNamespace(role="assistant", msg="Welcome!"),
        ],
    )

    with patch.multiple(
        "orchestra.web.api.assistant.views",
        create_pubsub_topic=track_create_pubsub,
        wake_up_assistant=track_wakeup,
        log_pre_hire_chat=track_log_chat,
        jsonable_encoder=lambda x: x,
    ):
        await _post_create_setup(
            assistant_id="test-ordering-123",
            user_id="user-1",
            organization_id=None,
            assistant_in=assistant_in,
            api_key="fake-key",
            is_staging=True,
            session_factory=MagicMock(),
        )

    assert order == [
        "create_pubsub_topic",
        "wake_up_assistant",
        "log_pre_hire_chat",
    ], f"Expected PubSub → wakeup → log_pre_hire_chat, got: {order}"


@pytest.mark.anyio
async def test_log_pre_hire_chat_skipped_when_absent():
    """log_pre_hire_chat is not called when pre_hire_chat is None."""
    from orchestra.web.api.assistant.views import _post_create_setup

    mock_log = AsyncMock()

    assistant_in = SimpleNamespace(
        create_infra=False,
        email=None,
        user_phone=None,
        user_whatsapp_number=None,
        desktop_mode=None,
        pre_hire_chat=None,
    )

    with patch.multiple(
        "orchestra.web.api.assistant.views",
        create_pubsub_topic=AsyncMock(return_value={"status": "ok"}),
        wake_up_assistant=AsyncMock(return_value=MagicMock(status_code=200)),
        log_pre_hire_chat=mock_log,
        jsonable_encoder=lambda x: x,
    ):
        await _post_create_setup(
            assistant_id="test-no-chat-456",
            user_id="user-2",
            organization_id=None,
            assistant_in=assistant_in,
            api_key="fake-key",
            is_staging=True,
            session_factory=MagicMock(),
        )

    mock_log.assert_not_called()


@pytest.mark.anyio
async def test_log_pre_hire_chat_still_runs_when_wakeup_fails():
    """Pre-hire chat is still attempted even if wakeup returns a non-200 status.

    PubSub topic exists (create succeeded), so the adapter can publish even if
    the wakeup call itself failed — Unity may already be running or may start
    later and drain the backlog.
    """
    from orchestra.web.api.assistant.views import _post_create_setup

    order: list[str] = []

    async def track_create_pubsub(*a, **kw):
        order.append("create_pubsub_topic")
        return {"status": "ok"}

    async def track_wakeup(*a, **kw):
        order.append("wake_up_assistant")
        resp = MagicMock()
        resp.status_code = 503
        resp.text = "service unavailable"
        return resp

    async def track_log_chat(*a, **kw):
        order.append("log_pre_hire_chat")
        return {"status": "success"}

    assistant_in = SimpleNamespace(
        create_infra=True,
        email=None,
        user_phone=None,
        user_whatsapp_number=None,
        desktop_mode=None,
        pre_hire_chat=[
            SimpleNamespace(role="user", msg="Hi"),
        ],
    )

    with patch.multiple(
        "orchestra.web.api.assistant.views",
        create_pubsub_topic=track_create_pubsub,
        wake_up_assistant=track_wakeup,
        log_pre_hire_chat=track_log_chat,
        jsonable_encoder=lambda x: x,
    ):
        await _post_create_setup(
            assistant_id="test-wakeup-fail-789",
            user_id="user-3",
            organization_id=None,
            assistant_in=assistant_in,
            api_key="fake-key",
            is_staging=True,
            session_factory=MagicMock(),
        )

    assert "create_pubsub_topic" in order
    assert "wake_up_assistant" in order
    assert "log_pre_hire_chat" in order
    assert order.index("log_pre_hire_chat") > order.index("create_pubsub_topic")
