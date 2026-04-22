"""Data access object for Hive rows."""

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import Hive


class HiveDAO:
    """CRUD access for the ``hives`` table.

    All writes use ``session.flush()`` so callers control transaction boundaries.
    Callers are responsible for checking that the hive belongs to the expected
    organization before mutating it.
    """

    def __init__(self, session: Session):
        self.session = session

    def create(
        self,
        organization_id: int,
        name: str,
        description: Optional[str] = None,
    ) -> Hive:
        """Insert a new Hive row and flush."""
        hive = Hive(
            organization_id=organization_id,
            name=name,
            description=description,
        )
        self.session.add(hive)
        self.session.flush()
        return hive

    def get_by_id(self, hive_id: int) -> Optional[Hive]:
        """Return the Hive with this primary key, or None."""
        return self.session.get(Hive, hive_id)

    def list_for_org(self, organization_id: int) -> list[Hive]:
        """Return all Hives that belong to *organization_id*."""
        stmt = select(Hive).where(Hive.organization_id == organization_id)
        return list(self.session.execute(stmt).scalars())

    def update(
        self,
        hive: Hive,
        name: Optional[str],
        description: Optional[str],
    ) -> Hive:
        """Apply name / description edits in-place and flush."""
        if name is not None:
            hive.name = name
        if description is not None:
            hive.description = description
        self.session.flush()
        return hive

    def set_status(self, hive: Hive, status: str) -> None:
        """Update the status field and flush."""
        hive.status = status
        self.session.flush()

    def delete(self, hive: Hive) -> None:
        """Delete the Hive row. ``ON DELETE SET NULL`` clears ``assistants.hive_id``."""
        self.session.delete(hive)
