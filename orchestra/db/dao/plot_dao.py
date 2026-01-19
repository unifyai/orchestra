"""DAO for Plot model operations."""

import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session, joinedload

from orchestra.db.models.orchestra_models import Plot

logger = logging.getLogger(__name__)


class TokenGenerationError(Exception):
    """Raised when unable to generate a unique token after max retries."""

    pass


class PlotDAO:
    """Data access object for Plot operations."""

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
        plot_config: Dict[str, Any],
        project_config: Dict[str, Any],
        title: Optional[str] = None,
    ) -> Plot:
        """
        Create a new plot.

        Args:
            project_id: ID of the project this plot belongs to.
            user_id: ID of the user creating the plot.
            organization_id: ID of the organization (None for personal plots).
            plot_config: Plot configuration (type, axes, etc.).
            project_config: Project/logs configuration (filters, limits, etc.).
            title: Optional title for the plot.

        Returns:
            The created Plot object.
        """
        token = self._generate_token()

        plot = Plot(
            token=token,
            project_id=project_id,
            user_id=user_id,
            organization_id=organization_id,
            title=title,
            plot_config=plot_config,
            project_config=project_config,
        )

        self.session.add(plot)
        self.session.flush()

        logger.info(f"Created plot with token {token} for project {project_id}")
        return plot

    def get_by_token(self, token: str) -> Optional[Plot]:
        """
        Get a plot by its token.

        Args:
            token: The 12-character hex token.

        Returns:
            The Plot if found, None otherwise.
        """
        return (
            self.session.query(Plot)
            .options(joinedload(Plot.project))
            .filter(Plot.token == token)
            .first()
        )

    def get_by_id(self, plot_id: int) -> Optional[Plot]:
        """
        Get a plot by its ID.

        Args:
            plot_id: The plot ID.

        Returns:
            The Plot if found, None otherwise.
        """
        return (
            self.session.query(Plot)
            .options(joinedload(Plot.project))
            .filter(Plot.id == plot_id)
            .first()
        )

    def list_by_project(
        self,
        project_id: int,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[Plot], int]:
        """
        List all plots for a project.

        Args:
            project_id: ID of the project.
            limit: Maximum number of results to return (default 50, max 100).
            offset: Number of results to skip for pagination.

        Returns:
            Tuple of (list of plots, total count).
        """
        # Clamp limit to max 100
        limit = min(limit, 100)

        query = self.session.query(Plot).filter(Plot.project_id == project_id)

        # Get total count before pagination
        total_count = query.count()

        # Eager load project relationship
        results = (
            query.options(joinedload(Plot.project))
            .order_by(Plot.created_at.desc())
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
    ) -> Tuple[List[Plot], int]:
        """
        List plots accessible to a user based on API key context.

        For personal API keys (organization_id is None):
            Returns plots for personal projects (org_id is NULL).

        For organization API keys:
            Returns plots for that organization's projects.

        Args:
            user_id: The user ID.
            organization_id: Organization ID from API key context (None for personal).
            project_id: Optional project ID to filter by.
            context: Optional context to filter by (stored in project_config JSONB).
            limit: Maximum number of results to return (default 50, max 100).
            offset: Number of results to skip for pagination.

        Returns:
            Tuple of (list of plots, total count).
        """
        # Clamp limit to max 100
        limit = min(limit, 100)

        query = self.session.query(Plot)

        if organization_id is None:
            # Personal context - filter by BOTH org_id AND user_id
            query = query.filter(Plot.organization_id.is_(None))
            query = query.filter(Plot.user_id == user_id)
        else:
            # Organization context - plots for that organization
            query = query.filter(Plot.organization_id == organization_id)

        if project_id is not None:
            query = query.filter(Plot.project_id == project_id)

        if context is not None:
            query = query.filter(Plot.project_config["context"].astext == context)

        # Get total count before pagination
        total_count = query.count()

        # Eager load project relationship to avoid N+1 queries
        results = (
            query.options(joinedload(Plot.project))
            .order_by(Plot.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

        return results, total_count

    def update(
        self,
        plot_id: int,
        title: Optional[str] = None,
        plot_config: Optional[Dict[str, Any]] = None,
        project_config: Optional[Dict[str, Any]] = None,
        project_id: Optional[int] = None,
        organization_id: Optional[int] = ...,  # Use ... as sentinel for "not provided"
    ) -> Optional[Plot]:
        """
        Update a plot's fields.

        Args:
            plot_id: ID of the plot to update.
            title: New title (if provided).
            plot_config: New plot config (if provided).
            project_config: New project config (if provided).
            project_id: New project ID (if project changed).
            organization_id: New organization ID (if project changed). Use ... to skip.

        Returns:
            The updated Plot, or None if not found.
        """
        plot = self.get_by_id(plot_id)
        if not plot:
            return None

        if title is not None:
            plot.title = title
        if plot_config is not None:
            plot.plot_config = plot_config
        if project_config is not None:
            plot.project_config = project_config
        if project_id is not None:
            plot.project_id = project_id
        if organization_id is not ...:
            plot.organization_id = organization_id

        self.session.flush()
        logger.info(f"Updated plot {plot_id}")
        return plot

    def delete(self, plot_id: int) -> bool:
        """
        Delete a plot by ID.

        Args:
            plot_id: ID of the plot to delete.

        Returns:
            True if deleted, False if not found.
        """
        plot = self.get_by_id(plot_id)
        if not plot:
            return False

        self.session.delete(plot)
        self.session.flush()
        logger.info(f"Deleted plot {plot_id}")
        return True

    def delete_by_token(self, token: str) -> bool:
        """
        Delete a plot by token.

        Args:
            token: The 12-character hex token.

        Returns:
            True if deleted, False if not found.
        """
        plot = self.get_by_token(token)
        if not plot:
            return False

        self.session.delete(plot)
        self.session.flush()
        logger.info(f"Deleted plot with token {token}")
        return True

    def delete_by_project(
        self,
        project_id: int,
        context: Optional[str] = None,
    ) -> int:
        """
        Delete all plots for a project, optionally filtered by context.

        Args:
            project_id: ID of the project.
            context: Optional context stored in project_config to filter by.

        Returns:
            Number of plots deleted.
        """
        query = self.session.query(Plot).filter(Plot.project_id == project_id)

        if context is not None:
            query = query.filter(Plot.project_config["context"].astext == context)

        count = query.delete(synchronize_session="fetch")
        self.session.flush()

        logger.info(
            f"Deleted {count} plots for project {project_id}"
            + (f" with context '{context}'" if context else ""),
        )
        return count

    def update_organization_id(
        self,
        project_id: int,
        organization_id: Optional[int],
    ) -> int:
        """
        Update organization_id for all plots belonging to a project.

        Used when a project is transferred between personal and organization ownership.

        Args:
            project_id: ID of the project.
            organization_id: New organization ID (None for personal).

        Returns:
            Number of plots updated.
        """
        result = (
            self.session.query(Plot)
            .filter(Plot.project_id == project_id)
            .update(
                {Plot.organization_id: organization_id},
                synchronize_session="fetch",
            )
        )
        self.session.flush()
        logger.info(
            f"Updated organization_id to {organization_id} for {result} plots "
            f"in project {project_id}",
        )
        return result
