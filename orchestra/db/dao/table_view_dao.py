"""DAO for TableView model operations."""

import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session, joinedload

from orchestra.db.models.orchestra_models import Project, TableView

logger = logging.getLogger(__name__)


class TokenGenerationError(Exception):
    """Raised when unable to generate a unique token after max retries."""

    pass


class TableViewDAO:
    """Data access object for TableView operations."""

    def __init__(self, session: Session):
        self.session = session

    def _generate_token(self) -> str:
        """
        Generate a unique 12-character hex token.

        Retries up to 3 times in case of collision.

        Raises:
            TokenGenerationError: If unable to generate unique token after retries.
        """
        for _ in range(3):
            token = uuid.uuid4().hex[:12]
            if not self.get_by_token(token):
                return token
        raise TokenGenerationError("Failed to generate unique token after 3 attempts")

    def create(
        self,
        project_id: int,
        user_id: str,
        organization_id: Optional[int],
        table_config: Dict[str, Any],
        project_config: Dict[str, Any],
        title: Optional[str] = None,
    ) -> TableView:
        """
        Create a new table view.

        Args:
            project_id: ID of the project this table view belongs to.
            user_id: ID of the user creating the table view.
            organization_id: ID of the organization (None for personal table views).
            table_config: Table configuration (columns, visibility, etc.).
            project_config: Project/logs configuration (filters, limits, etc.).
            title: Optional title for the table view.

        Returns:
            The created TableView object.
        """
        token = self._generate_token()

        table_view = TableView(
            token=token,
            project_id=project_id,
            user_id=user_id,
            organization_id=organization_id,
            title=title,
            table_config=table_config,
            project_config=project_config,
        )

        self.session.add(table_view)
        self.session.flush()

        logger.info(f"Created table_view with token {token} for project {project_id}")
        return table_view

    def get_by_token(self, token: str) -> Optional[TableView]:
        """
        Get a table view by its token.

        Args:
            token: The 12-character hex token.

        Returns:
            The TableView if found, None otherwise.
        """
        return (
            self.session.query(TableView)
            .options(joinedload(TableView.project))
            .filter(TableView.token == token)
            .first()
        )

    def get_by_id(self, table_view_id: int) -> Optional[TableView]:
        """
        Get a table view by its ID.

        Args:
            table_view_id: The table view ID.

        Returns:
            The TableView if found, None otherwise.
        """
        return (
            self.session.query(TableView)
            .options(joinedload(TableView.project))
            .filter(TableView.id == table_view_id)
            .first()
        )

    def list_by_project(
        self,
        project_id: int,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[TableView], int]:
        """
        List all table views for a project.

        Args:
            project_id: ID of the project.
            limit: Maximum number of results to return (default 50, max 100).
            offset: Number of results to skip for pagination.

        Returns:
            Tuple of (list of table views, total count).
        """
        # Clamp limit to max 100
        limit = min(limit, 100)

        query = self.session.query(TableView).filter(TableView.project_id == project_id)

        # Get total count before pagination
        total_count = query.count()

        # Eager load project relationship
        results = (
            query.options(joinedload(TableView.project))
            .order_by(TableView.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

        return results, total_count

    def list_by_user_context(
        self,
        user_id: str,
        organization_id: Optional[int],
        project_id: Optional[int] = None,
        context: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[TableView], int]:
        """
        List table views accessible to a user based on API key context.

        For personal API keys (organization_id is None):
            Returns table views for personal projects (org_id is NULL).

        For organization API keys:
            Returns table views for that organization's projects.

        Args:
            user_id: The user ID.
            organization_id: Organization ID from API key context (None for personal).
            project_id: Optional project ID to filter by.
            context: Optional context to filter by (stored in project_config JSONB).
            limit: Maximum number of results to return (default 50, max 100).
            offset: Number of results to skip for pagination.

        Returns:
            Tuple of (list of table views, total count).
        """
        # Clamp limit to max 100
        limit = min(limit, 100)

        query = self.session.query(TableView)

        if organization_id is None:
            # Personal context - filter by BOTH org_id AND user_id
            query = query.filter(TableView.organization_id.is_(None))
            query = query.filter(TableView.user_id == user_id)
        else:
            # Organization context - table views for that organization
            query = query.filter(TableView.organization_id == organization_id)

        if project_id is not None:
            query = query.filter(TableView.project_id == project_id)

        if context is not None:
            query = query.filter(TableView.project_config["context"].astext == context)

        # Get total count before pagination
        total_count = query.count()

        # Eager load project relationship to avoid N+1 queries
        results = (
            query.options(joinedload(TableView.project))
            .order_by(TableView.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

        return results, total_count

    def update(
        self,
        table_view_id: int,
        title: Optional[str] = None,
        table_config: Optional[Dict[str, Any]] = None,
        project_config: Optional[Dict[str, Any]] = None,
        project_id: Optional[int] = None,
        organization_id: Optional[int] = ...,  # Use ... as sentinel for "not provided"
    ) -> Optional[TableView]:
        """
        Update a table view's fields.

        Args:
            table_view_id: ID of the table view to update.
            title: New title (if provided).
            table_config: New table config (if provided).
            project_config: New project config (if provided).
            project_id: New project ID (if project changed).
            organization_id: New organization ID (if project changed). Use ... to skip.

        Returns:
            The updated TableView, or None if not found.
        """
        table_view = self.get_by_id(table_view_id)
        if not table_view:
            return None

        if title is not None:
            table_view.title = title
        if table_config is not None:
            table_view.table_config = table_config
        if project_config is not None:
            table_view.project_config = project_config
        if project_id is not None:
            table_view.project_id = project_id
        if organization_id is not ...:
            table_view.organization_id = organization_id

        self.session.flush()
        logger.info(f"Updated table_view {table_view_id}")
        return table_view

    def delete(self, table_view_id: int) -> bool:
        """
        Delete a table view by ID.

        Args:
            table_view_id: ID of the table view to delete.

        Returns:
            True if deleted, False if not found.
        """
        table_view = self.get_by_id(table_view_id)
        if not table_view:
            return False

        self.session.delete(table_view)
        self.session.flush()
        logger.info(f"Deleted table_view {table_view_id}")
        return True

    def delete_by_token(self, token: str) -> bool:
        """
        Delete a table view by token.

        Args:
            token: The 12-character hex token.

        Returns:
            True if deleted, False if not found.
        """
        table_view = self.get_by_token(token)
        if not table_view:
            return False

        self.session.delete(table_view)
        self.session.flush()
        logger.info(f"Deleted table_view with token {token}")
        return True

    def delete_by_project(
        self,
        project_id: int,
        context: Optional[str] = None,
    ) -> int:
        """
        Delete all table views for a project, optionally filtered by context.

        Args:
            project_id: ID of the project.
            context: Optional context stored in project_config to filter by.

        Returns:
            Number of table views deleted.
        """
        query = self.session.query(TableView).filter(TableView.project_id == project_id)

        if context is not None:
            query = query.filter(TableView.project_config["context"].astext == context)

        count = query.delete(synchronize_session="fetch")
        self.session.flush()

        logger.info(
            f"Deleted {count} table_views for project {project_id}"
            + (f" with context '{context}'" if context else ""),
        )
        return count

    def update_organization_id(
        self,
        project_id: int,
        organization_id: Optional[int],
    ) -> int:
        """
        Update organization_id for all table views belonging to a project.

        Used when a project is transferred between personal and organization ownership.

        Args:
            project_id: ID of the project.
            organization_id: New organization ID (None for personal).

        Returns:
            Number of table views updated.
        """
        result = (
            self.session.query(TableView)
            .filter(TableView.project_id == project_id)
            .update(
                {TableView.organization_id: organization_id},
                synchronize_session="fetch",
            )
        )
        self.session.flush()
        logger.info(
            f"Updated organization_id to {organization_id} for {result} table_views "
            f"in project {project_id}",
        )
        return result
