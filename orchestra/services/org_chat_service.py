"""Org chat: team participant resolution for the unified chat store.

Team group-chat *messages* live in the unified chat store
(:mod:`orchestra.db.dao.chat_dao`) and are served by the ``/chat`` API. This
module keeps the team-scoped helpers that the chat service and org-call
endpoints share: participant listings and assistant sender identity.

Realtime delivery and assistant fan-out are delegated to the hosted
communication layer (adapters ``POST /unify/chat``): one publish to the
per-organization Pub/Sub topic for Console SSE, plus one standard
``unify_message`` envelope per roster-visible team assistant (private
single-player coordinators are excluded) — team chat is
ordinary unify_message traffic fanned out to every team assistant, like a
large email CC chain.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from orchestra.db.dao.team_dao import TeamDAO
from orchestra.db.models.orchestra_models import Team, User

logger = logging.getLogger(__name__)

ASSISTANTS_PROJECT_NAME = "Assistants"

SENDER_KIND_USER = "user"
SENDER_KIND_ASSISTANT = "assistant"


def team_chat_participants(
    session: Session,
    *,
    team: Team,
) -> dict[str, list[dict[str, Any]]]:
    """Humans and roster-visible assistants participating in a team chat."""
    team_dao = TeamDAO(session)

    member_user_ids = team_dao.get_team_members(team.id)
    users = (
        session.query(User).filter(User.id.in_(member_user_ids)).all()
        if member_user_ids
        else []
    )
    humans = [
        {
            "user_id": user.id,
            "name": " ".join(part for part in [user.name, user.last_name] if part)
            or user.email,
        }
        for user in users
    ]

    assistants = [
        {
            "assistant_id": assistant.agent_id,
            "name": " ".join(
                part for part in [assistant.first_name, assistant.surname] if part
            )
            or f"Assistant {assistant.agent_id}",
        }
        for _, assistant in team_dao.list_assistant_members(team.id)
        if not assistant.is_private_coordinator
    ]
    return {"humans": humans, "assistants": assistants}


def assistant_email(session: Session, assistant_id: int) -> str:
    """The assistant's provisioned email address ("" when none).

    Chat fan-out carries this as the sender identity for AI-authored
    messages so receiving runtimes can resolve (or provision) a teammate
    contact for the author.
    """
    from orchestra.db.dao.assistant_contact_dao import AssistantContactDAO

    for contact in AssistantContactDAO(session).get_active_contacts_for_assistant(
        assistant_id,
    ):
        if contact.contact_type == "email" and contact.contact_value:
            return contact.contact_value
    return ""
