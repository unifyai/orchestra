"""The Meet dispatch payload Orchestra POSTs to adapters for one assistant.

Lives apart from ``test_org_chat.py`` deliberately: that module stubs
``_post_meet_dispatch`` with an autouse fixture, so the payload it builds — in
particular which opening the assistant is handed — has no coverage there.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestra.web.api.calls import views


@pytest.fixture
def adapters(monkeypatch) -> list[dict]:
    """Capture the JSON bodies POSTed to adapters instead of sending them."""
    posted: list[dict] = []

    class _Client:
        async def post(self, _url, **kwargs):
            posted.append(kwargs["json"])

    monkeypatch.setattr(views, "get_async_client", lambda: _Client())
    monkeypatch.setattr(views, "ADAPTERS_URL", "https://adapters.example.com")
    monkeypatch.setattr(views, "LOCAL_ADAPTERS_URL", "")
    monkeypatch.setattr(views, "ADMIN_KEY", "admin-key")
    return posted


def _call_session(opening_config: dict | None) -> SimpleNamespace:
    return SimpleNamespace(
        id="call-1",
        livekit_room="unity_call_call-1",
        opening_config=opening_config,
    )


@pytest.mark.anyio
async def test_session_opening_is_used_when_no_override(adapters):
    """The opening a call started with is what its own assistants dispatch on."""
    await views._post_meet_dispatch(
        _call_session({"mode": "opener", "opener_text": "Quick sync?"}),
        assistant_id=7,
        roster=[],
    )
    assert adapters[0]["opening_config"] == {
        "mode": "opener",
        "opener_text": "Quick sync?",
    }


@pytest.mark.anyio
async def test_override_beats_the_session_opening(adapters):
    """A late joiner must not replay the opener the call was started with.

    Without the override it would speak the owner's verbatim opening line, or
    generate a fresh greeting, on top of a conversation already in progress.
    """
    await views._post_meet_dispatch(
        _call_session({"mode": "opener", "opener_text": "Quick sync?"}),
        assistant_id=7,
        roster=[],
        opening_config=views.JOINED_MID_CALL_OPENING,
    )
    assert adapters[0]["opening_config"] == {"mode": "silent"}


@pytest.mark.anyio
async def test_override_applies_when_the_session_has_no_opening(adapters):
    """The shape a human-created group call actually has.

    ``opening_config`` is null on such a session, which the runtime reads as
    "generate and speak a greeting" — the default the override exists to
    displace.
    """
    await views._post_meet_dispatch(
        _call_session(None),
        assistant_id=7,
        roster=[],
        opening_config=views.JOINED_MID_CALL_OPENING,
    )
    assert adapters[0]["opening_config"] == {"mode": "silent"}


@pytest.mark.anyio
async def test_no_opening_is_sent_when_neither_is_set(adapters):
    """Absent both, the key is omitted rather than sent as null."""
    await views._post_meet_dispatch(
        _call_session(None),
        assistant_id=7,
        roster=[],
    )
    assert "opening_config" not in adapters[0]
