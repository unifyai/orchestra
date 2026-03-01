"""DAO for managing user onboarding status."""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import OnboardingStatus


class OnboardingStatusDAO:
    """
    DAO for managing user onboarding status.

    The database fields are intentionally freeform. Value validation
    is done at the API layer via Pydantic schemas.
    """

    def __init__(self, session: Session):
        self.session = session

    def get_by_user_id(self, user_id: str) -> Optional[OnboardingStatus]:
        """Get onboarding status for a user."""
        query = select(OnboardingStatus).where(OnboardingStatus.user_id == user_id)
        return self.session.execute(query).scalar_one_or_none()

    def create(
        self,
        user_id: str,
        current_step: str = "workspace_setup",
        step_data: Optional[Dict[str, Any]] = None,
    ) -> OnboardingStatus:
        """
        Create onboarding status for a user.

        Args:
            user_id: The user's ID
            current_step: Initial step (default: "workspace_setup")
            step_data: Optional initial step data

        Returns:
            The created OnboardingStatus
        """
        status = OnboardingStatus(
            user_id=user_id,
            current_step=current_step,
            step_data=step_data or {},
        )
        self.session.add(status)
        return status

    def get_or_create(
        self,
        user_id: str,
        current_step: str = "workspace_setup",
        step_data: Optional[Dict[str, Any]] = None,
    ) -> OnboardingStatus:
        """
        Get existing onboarding status or create new one.

        Args:
            user_id: The user's ID
            current_step: Step to use if creating (default: "workspace_setup")
            step_data: Step data to use if creating

        Returns:
            The existing or newly created OnboardingStatus
        """
        existing = self.get_by_user_id(user_id)
        if existing:
            return existing
        return self.create(user_id, current_step, step_data)

    def update(
        self,
        user_id: str,
        current_step: Optional[str] = None,
        step_data: Optional[Dict[str, Any]] = None,
    ) -> Optional[OnboardingStatus]:
        """
        Update onboarding status for a user.

        Args:
            user_id: The user's ID
            current_step: New step (optional)
            step_data: New step data (optional, replaces existing)

        Returns:
            The updated OnboardingStatus, or None if not found
        """
        status = self.get_by_user_id(user_id)
        if not status:
            return None

        if current_step is not None:
            status.current_step = current_step
        if step_data is not None:
            status.step_data = step_data

        status.updated_at = datetime.now(timezone.utc)
        return status

    def update_step_data_field(
        self,
        user_id: str,
        field: str,
        value: Any,
    ) -> Optional[OnboardingStatus]:
        """
        Update a single field in step_data without replacing the whole dict.

        Args:
            user_id: The user's ID
            field: The field to update
            value: The new value

        Returns:
            The updated OnboardingStatus, or None if not found
        """
        status = self.get_by_user_id(user_id)
        if not status:
            return None

        # Copy and update step_data to trigger SQLAlchemy change detection
        new_step_data = dict(status.step_data or {})
        new_step_data[field] = value
        status.step_data = new_step_data
        status.updated_at = datetime.now(timezone.utc)
        return status

    def mark_completed(self, user_id: str) -> Optional[OnboardingStatus]:
        """
        Mark onboarding as completed.

        Preserves existing step_data and adds the completed_at timestamp.

        Args:
            user_id: The user's ID

        Returns:
            The updated OnboardingStatus, or None if not found
        """
        status = self.get_by_user_id(user_id)
        if not status:
            return None

        merged_data = dict(status.step_data or {})
        merged_data["completed_at"] = datetime.now(timezone.utc).isoformat()

        return self.update(
            user_id=user_id,
            current_step="completed",
            step_data=merged_data,
        )

    def delete(self, user_id: str) -> bool:
        """
        Delete onboarding status for a user.

        Args:
            user_id: The user's ID

        Returns:
            True if deleted, False if not found
        """
        status = self.get_by_user_id(user_id)
        if status:
            self.session.delete(status)
            return True
        return False

    def reset(self, user_id: str) -> OnboardingStatus:
        """
        Reset onboarding status to initial state.

        If status exists, resets it. Otherwise creates new.

        Args:
            user_id: The user's ID

        Returns:
            The reset OnboardingStatus
        """
        status = self.get_by_user_id(user_id)
        if status:
            status.current_step = "workspace_setup"
            status.step_data = {}
            status.updated_at = datetime.now(timezone.utc)
            return status
        return self.create(user_id, current_step="workspace_setup")
