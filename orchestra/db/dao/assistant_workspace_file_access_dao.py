"""DAO for the assistant_workspace_file_access table."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import AssistantWorkspaceFileAccess

logger = logging.getLogger(__name__)


class AssistantWorkspaceFileAccessDAO:
    """CRUD operations for the per-assistant, per-provider file allowlist."""

    def __init__(self, session: Session):
        self.session = session

    def get(self, agent_id: int, provider: str) -> AssistantWorkspaceFileAccess | None:
        """Return the policy row for *agent_id*/*provider*, or ``None``."""
        return (
            self.session.query(AssistantWorkspaceFileAccess)
            .filter(
                AssistantWorkspaceFileAccess.agent_id == agent_id,
                AssistantWorkspaceFileAccess.provider == provider,
            )
            .first()
        )

    def upsert(
        self,
        agent_id: int,
        provider: str,
        default_allow: bool,
        decisions: list[dict[str, Any]],
    ) -> AssistantWorkspaceFileAccess:
        """Create or replace the policy for *agent_id*/*provider*."""
        row = self.get(agent_id, provider)
        if row:
            row.default_allow = default_allow
            row.decisions = decisions
            row.updated_at = datetime.now(timezone.utc)
        else:
            row = AssistantWorkspaceFileAccess(
                agent_id=agent_id,
                provider=provider,
                default_allow=default_allow,
                decisions=decisions,
            )
            self.session.add(row)
        self.session.flush()
        return row

    def delete(self, agent_id: int, provider: str) -> bool:
        """Delete a provider policy.  Returns ``True`` if a row was removed."""
        count = (
            self.session.query(AssistantWorkspaceFileAccess)
            .filter(
                AssistantWorkspaceFileAccess.agent_id == agent_id,
                AssistantWorkspaceFileAccess.provider == provider,
            )
            .delete()
        )
        self.session.flush()
        return count > 0
