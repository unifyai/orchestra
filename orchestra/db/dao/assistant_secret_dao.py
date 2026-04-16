"""DAO for the assistant_secrets table."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import AssistantSecret

logger = logging.getLogger(__name__)


class AssistantSecretDAO:
    """CRUD operations for per-assistant secrets (OAuth tokens, API keys, etc.)."""

    def __init__(self, session: Session):
        self.session = session

    def get_all(self, agent_id: int) -> dict[str, str]:
        """Return all secrets for *agent_id* as ``{name: value}``."""
        rows = (
            self.session.query(AssistantSecret)
            .filter(AssistantSecret.agent_id == agent_id)
            .all()
        )
        return {r.secret_name: r.secret_value for r in rows}

    def get(self, agent_id: int, name: str) -> str | None:
        """Return a single secret value, or ``None`` if it doesn't exist."""
        row = (
            self.session.query(AssistantSecret)
            .filter(
                AssistantSecret.agent_id == agent_id,
                AssistantSecret.secret_name == name,
            )
            .first()
        )
        return row.secret_value if row else None

    def upsert(
        self,
        user_id: str,
        agent_id: int,
        name: str,
        value: str,
    ) -> AssistantSecret:
        """Create or update a secret."""
        row = (
            self.session.query(AssistantSecret)
            .filter(
                AssistantSecret.agent_id == agent_id,
                AssistantSecret.secret_name == name,
            )
            .first()
        )
        if row:
            row.secret_value = value
            row.updated_at = datetime.now(timezone.utc)
        else:
            row = AssistantSecret(
                user_id=user_id,
                agent_id=agent_id,
                secret_name=name,
                secret_value=value,
            )
            self.session.add(row)
        self.session.flush()
        return row

    def delete(self, agent_id: int, name: str) -> bool:
        """Delete a single secret.  Returns ``True`` if a row was removed."""
        count = (
            self.session.query(AssistantSecret)
            .filter(
                AssistantSecret.agent_id == agent_id,
                AssistantSecret.secret_name == name,
            )
            .delete()
        )
        self.session.flush()
        return count > 0

    def delete_all(self, agent_id: int) -> int:
        """Delete all secrets for an assistant.  Returns the number of rows removed."""
        count = (
            self.session.query(AssistantSecret)
            .filter(AssistantSecret.agent_id == agent_id)
            .delete()
        )
        self.session.flush()
        return count
