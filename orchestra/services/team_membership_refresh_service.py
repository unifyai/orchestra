"""Publish assistant runtime refreshes after team membership changes."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable

from sqlalchemy.orm import Session

from orchestra.db.dao.team_dao import TeamDAO
from orchestra.db.models.orchestra_models import Assistant
from orchestra.web.api.utils.assistant_infra import reawaken_assistant

logger = logging.getLogger(__name__)

MEMBERSHIP_REFRESH_CONCURRENCY = 10
MembershipRefreshPayload = tuple[int, str | None, dict[str, str]]


def membership_refresh_payloads(
    session: Session,
    assistants: Iterable[Assistant],
) -> list[MembershipRefreshPayload]:
    """Build assistant membership-refresh payloads after team state changes."""

    assistants_by_id = {assistant.agent_id: assistant for assistant in assistants}
    assistant_ids = sorted(assistants_by_id)
    if not assistant_ids:
        return []

    team_dao = TeamDAO(session)
    team_summaries_by_assistant = team_dao.team_summaries_for_assistants(
        assistant_ids,
    )
    return [
        (
            assistant_id,
            assistants_by_id[assistant_id].deploy_env,
            {
                "assistant_id": str(assistant_id),
                "team_ids": json.dumps(
                    [
                        summary["team_id"]
                        for summary in team_summaries_by_assistant.get(
                            assistant_id,
                            [],
                        )
                    ],
                ),
                "team_summaries": json.dumps(
                    team_summaries_by_assistant.get(assistant_id, []),
                ),
                "update_kind": "membership",
            },
        )
        for assistant_id in assistant_ids
    ]


async def publish_membership_refreshes_best_effort(
    payloads: Iterable[MembershipRefreshPayload],
) -> None:
    """Best-effort notify live runtimes that their accessible teams changed."""

    semaphore = asyncio.Semaphore(MEMBERSHIP_REFRESH_CONCURRENCY)

    async def _publish(payload: MembershipRefreshPayload) -> None:
        assistant_id, deploy_env, data = payload
        async with semaphore:
            try:
                await reawaken_assistant(
                    str(assistant_id),
                    deploy_env=deploy_env,
                    data=data,
                )
            except Exception:
                logger.exception(
                    "Failed to publish team membership refresh for assistant %s",
                    assistant_id,
                )

    await asyncio.gather(*[_publish(payload) for payload in payloads])
