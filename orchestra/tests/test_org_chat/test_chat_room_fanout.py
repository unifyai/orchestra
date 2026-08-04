"""Rooms fan out to every member assistant, which needs a floor and a signal.

Two separate gaps, both about a room message reaching several assistants at once:

* An assistant's reply is itself a room message, so it reaches the other member
  assistants carrying the same "there is an unanswered message here" weight a
  human's would. Two assistants in a room could volley with nothing bounding it.
* Being @mentioned was the only thing separating "this is for me" from "I am
  cc'd", and the structured mentions never left Orchestra — only the literal
  "@Name" inside the body did, for the model to spot as prose.

These are unit tests over the fan-out decision; they need no database.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestra.db.dao.chat_dao import KIND_ASSISTANT_DM, KIND_DM, KIND_GROUP, KIND_TEAM
from orchestra.services import chat_service
from orchestra.services.chat_service import (
    MAX_CONSECUTIVE_ASSISTANT_ROOM_MESSAGES as VOLLEY_LIMIT,
)
from orchestra.services.chat_service import (
    _fanout_assistant_ids,
    build_chat_dispatch_payload,
)

MEMBER_ASSISTANT_IDS = [11, 22]


def _thread(kind: str, thread_id: int = 5) -> SimpleNamespace:
    return SimpleNamespace(
        kind=kind,
        id=thread_id,
        organization_id=1,
        assistant_id=11,
        peer_assistant_id=None,
        user_id="owner",
        team_id=7 if kind == KIND_TEAM else None,
        group_id=9 if kind == KIND_GROUP else None,
        user_a_id=None,
        user_b_id=None,
    )


def _history(*senders: str) -> list[SimpleNamespace]:
    """Messages oldest-first; "a" is assistant-authored, "h" is a human."""
    return [
        SimpleNamespace(sender_assistant_id=11 if who == "a" else None)
        for who in senders
    ]


@pytest.fixture
def room_history(monkeypatch):
    """Stub the thread tail read and the room's assistant roster."""
    state: dict[str, list] = {"messages": []}

    class _FakeDAO:
        def __init__(self, _session):
            pass

        def list_messages(self, *, thread_id, limit):
            # Mirrors the real DAO: the newest `limit` messages, oldest first.
            del thread_id
            return state["messages"][-limit:]

    monkeypatch.setattr(chat_service, "ChatDAO", _FakeDAO)
    monkeypatch.setattr(
        "orchestra.services.org_chat_service.team_chat_participants",
        lambda _session, *, team: {
            "humans": [],
            "assistants": [{"assistant_id": aid} for aid in MEMBER_ASSISTANT_IDS],
        },
    )
    monkeypatch.setattr(
        "orchestra.services.chat_group_service.get_active_group",
        lambda _session, *, organization_id, group_id: SimpleNamespace(id=group_id),
    )
    monkeypatch.setattr(
        "orchestra.services.chat_group_service.chat_group_participants",
        lambda _session, *, group: {
            "humans": [],
            "assistants": [{"assistant_id": aid} for aid in MEMBER_ASSISTANT_IDS],
        },
    )

    def _set(*senders: str) -> None:
        state["messages"] = _history(*senders)

    return _set


def _fanout(kind: str, exclude: int | None = None) -> list[int]:
    return _fanout_assistant_ids(
        SimpleNamespace(get=lambda _model, _id: SimpleNamespace(id=_id, name="Team")),
        thread=_thread(kind),
        exclude_assistant_id=exclude,
    )


# ── The volley brake ────────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", [KIND_TEAM, KIND_GROUP])
def test_a_room_that_has_heard_from_a_human_still_fans_out(room_history, kind):
    room_history("h", "a", "a")

    assert _fanout(kind) == MEMBER_ASSISTANT_IDS


@pytest.mark.parametrize("kind", [KIND_TEAM, KIND_GROUP])
def test_a_room_of_only_assistants_stops_fanning_out(room_history, kind):
    """The brake: nobody human has spoken for the whole recent history."""
    room_history(*["a"] * VOLLEY_LIMIT)

    assert _fanout(kind) == []


def test_a_thread_too_young_to_have_volleyed_is_left_alone(room_history):
    """Below the limit there is no run to speak of, so no suppression."""
    room_history(*["a"] * (VOLLEY_LIMIT - 1))

    assert _fanout(KIND_TEAM) == MEMBER_ASSISTANT_IDS


def test_a_human_turn_releases_the_brake(room_history):
    """A human joining in makes the room live again, however long the run was."""
    room_history(*(["a"] * (VOLLEY_LIMIT * 2) + ["h"]))

    assert _fanout(KIND_TEAM) == MEMBER_ASSISTANT_IDS


def test_the_authoring_assistant_is_still_excluded_under_the_brake(room_history):
    """Exclusion and suppression are separate; the brake does not resurrect it."""
    room_history("h", "a", "a")

    assert _fanout(KIND_TEAM, exclude=MEMBER_ASSISTANT_IDS[0]) == [
        MEMBER_ASSISTANT_IDS[1],
    ]


def test_a_one_to_one_assistant_dm_is_not_a_room(room_history):
    """The brake is about rooms. A DM has one assistant and cannot volley."""
    room_history(*["a"] * VOLLEY_LIMIT)

    assert _fanout(KIND_ASSISTANT_DM) == [11]


def test_suppression_is_logged_rather_than_silent(room_history, caplog):
    """A room gone quiet from the brake must be distinguishable from a quiet room."""
    room_history(*["a"] * VOLLEY_LIMIT)

    with caplog.at_level("WARNING"):
        _fanout(KIND_TEAM)

    assert any("Suppressed chat fan-out" in record.message for record in caplog.records)


# ── Mentions reach the runtime ──────────────────────────────────────────────


def test_the_dispatch_payload_carries_who_was_addressed(room_history):
    mentions = [{"kind": "assistant", "id": "22", "name": "Bo"}]

    payload = build_chat_dispatch_payload(
        SimpleNamespace(get=lambda _model, _id: None),
        thread=_thread(KIND_DM),
        message={"id": 1, "content": "@Bo can you take this?", "mentions": mentions},
    )

    assert payload["assistant_event"]["mentions"] == mentions


def test_an_unaddressed_message_carries_an_empty_mention_list(room_history):
    """Absent and empty must look the same to the runtime, never None."""
    payload = build_chat_dispatch_payload(
        SimpleNamespace(get=lambda _model, _id: None),
        thread=_thread(KIND_DM),
        message={"id": 1, "content": "morning all"},
    )

    assert payload["assistant_event"]["mentions"] == []
