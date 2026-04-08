"""DAO for DashboardToken model operations."""

from typing import Optional

from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import DashboardToken


class DashboardTokenDAO:
    """Data access object for DashboardToken lookup table."""

    def __init__(self, session: Session):
        self.session = session

    def register(
        self,
        token: str,
        entity_type: str,
        context_name: str,
        project_id: int,
        user_id: str,
        organization_id: Optional[int],
    ) -> DashboardToken:
        """Register a new token mapping.

        The token is generated client-side (Unity) and assumed unique.
        The PRIMARY KEY constraint enforces uniqueness at the DB level.
        """
        entry = DashboardToken(
            token=token,
            entity_type=entity_type,
            context_name=context_name,
            project_id=project_id,
            user_id=user_id,
            organization_id=organization_id,
        )
        self.session.add(entry)
        self.session.flush()
        return entry

    def get_by_token(self, token: str) -> Optional[DashboardToken]:
        """Resolve a token to its context mapping."""
        return (
            self.session.query(DashboardToken)
            .filter(DashboardToken.token == token)
            .first()
        )

    def delete_by_token(self, token: str) -> bool:
        """Remove a token mapping. Returns True if deleted."""
        entry = self.get_by_token(token)
        if not entry:
            return False
        self.session.delete(entry)
        self.session.flush()
        return True
