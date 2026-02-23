"""Data Access Object for Permission model."""

from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import Permission


class PermissionDAO:
    """DAO for managing permissions."""

    def __init__(self, session: Session):
        self.session = session

    def get(self, id: int) -> Optional[Permission]:
        """
        Get a permission by ID.

        :param id: Permission ID.
        :return: Permission object or None if not found.
        """
        return self.session.query(Permission).filter_by(id=id).first()

    def get_by_name(self, name: str) -> Optional[Permission]:
        """
        Get a permission by name.

        :param name: Permission name (e.g., 'project:read').
        :return: Permission object or None if not found.
        """
        return self.session.query(Permission).filter_by(name=name).first()

    def filter(
        self,
        id: Optional[int] = None,
        name: Optional[str] = None,
        resource_type: Optional[str] = None,
        action: Optional[str] = None,
    ) -> List[Permission]:
        """
        Filter permissions by various criteria.

        :param id: Permission ID.
        :param name: Permission name.
        :param resource_type: Resource type (e.g., 'project', 'interface').
        :param action: Action type (e.g., 'read', 'write').
        :return: List of matching permissions.
        """
        query = select(Permission)
        if id:
            query = query.where(Permission.id == id)
        if name:
            query = query.where(Permission.name == name)
        if resource_type:
            query = query.where(Permission.resource_type == resource_type)
        if action:
            query = query.where(Permission.action == action)
        rows = self.session.execute(query)
        return [row[0] for row in rows.fetchall()]

    def list_all(self) -> List[Permission]:
        """
        List all permissions.

        :return: List of all permissions.
        """
        return list(self.session.query(Permission).all())

    def get_by_resource_type(self, resource_type: str) -> List[Permission]:
        """
        Get all permissions for a specific resource type.

        :param resource_type: Resource type (e.g., 'project', 'interface').
        :return: List of permissions for the resource type.
        """
        return list(
            self.session.query(Permission)
            .filter(Permission.resource_type == resource_type)
            .all(),
        )
