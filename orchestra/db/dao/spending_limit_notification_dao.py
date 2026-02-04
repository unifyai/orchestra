"""DAO for spending limit notifications."""

from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional

from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    Assistant,
    OrganizationMember,
    Role,
    SpendingLimitNotification,
)


class SpendingLimitNotificationDAO:
    """
    Data Access Object for spending limit notifications.

    Handles deduplication logic for spending limit notifications,
    including the "limit re-enabled" scenario using limit_set_at.
    """

    def __init__(self, session: Session):
        self.session = session

    def find_existing_notification(
        self,
        entity_type: str,
        entity_id: str,
        month: str,
        limit_value: Decimal,
    ) -> Optional[SpendingLimitNotification]:
        """
        Find an existing notification for the given entity, month, and limit value.

        This is used for deduplication - if a notification exists and the limit
        hasn't been re-configured since then, we skip sending another.
        """
        return (
            self.session.query(SpendingLimitNotification)
            .filter(
                SpendingLimitNotification.entity_type == entity_type,
                SpendingLimitNotification.entity_id == entity_id,
                SpendingLimitNotification.month == month,
                SpendingLimitNotification.limit_value == limit_value,
            )
            .first()
        )

    def should_notify(
        self,
        entity_type: str,
        entity_id: str,
        month: str,
        limit_value: Decimal,
        limit_set_at: Optional[datetime] = None,
    ) -> bool:
        """
        Check if we should send a notification.

        Returns True if:
        - No existing notification for this (entity, month, limit_value)
        - OR the limit was re-configured after the last notification (limit_set_at > notified_at)

        Args:
            entity_type: Type of entity ('assistant', 'user', 'member', 'organization')
            entity_id: ID of the entity
            month: Billing month in YYYY-MM format
            limit_value: The limit value that was reached
            limit_set_at: When the limit was configured (optional, for re-enable detection)

        Returns:
            True if notification should be sent, False if should be skipped
        """
        existing = self.find_existing_notification(
            entity_type=entity_type,
            entity_id=entity_id,
            month=month,
            limit_value=limit_value,
        )

        if existing is None:
            # No existing notification - should notify
            return True

        # Check if the limit was re-configured after we last notified
        if limit_set_at is not None and existing.notified_at is not None:
            # Ensure both are timezone-aware for comparison
            notified_at = existing.notified_at
            if notified_at.tzinfo is None:
                notified_at = notified_at.replace(tzinfo=timezone.utc)
            if limit_set_at.tzinfo is None:
                limit_set_at = limit_set_at.replace(tzinfo=timezone.utc)

            if limit_set_at > notified_at:
                # Limit was re-configured after notification - should notify again
                return True

        # Already notified and limit hasn't been re-configured
        return False

    def record_notification(
        self,
        entity_type: str,
        entity_id: str,
        month: str,
        limit_value: Decimal,
        notified_user_ids: List[str],
        limit_set_at: Optional[datetime] = None,
        entity_name: Optional[str] = None,
        current_spend: Optional[Decimal] = None,
    ) -> SpendingLimitNotification:
        """
        Record that a notification was sent.

        If a notification already exists for this (entity, month, limit_value),
        it will be updated with the new notified_at timestamp.

        Args:
            entity_type: Type of entity ('assistant', 'user', 'member', 'organization')
            entity_id: ID of the entity
            month: Billing month in YYYY-MM format
            limit_value: The limit value that was reached
            notified_user_ids: List of user IDs who received the notification
            limit_set_at: When the limit was configured (optional)
            entity_name: Name of the entity for auditing (optional)
            current_spend: Spend amount at time of notification (optional)

        Returns:
            The created or updated notification record
        """
        # Check if there's an existing record to update
        existing = self.find_existing_notification(
            entity_type=entity_type,
            entity_id=entity_id,
            month=month,
            limit_value=limit_value,
        )

        now = datetime.now(timezone.utc)

        if existing is not None:
            # Update existing record (for re-enable scenario)
            existing.notified_at = now
            existing.notified_user_ids = notified_user_ids
            existing.limit_set_at = limit_set_at
            existing.entity_name = entity_name
            existing.current_spend = current_spend
            return existing

        # Create new record
        notification = SpendingLimitNotification(
            entity_type=entity_type,
            entity_id=entity_id,
            month=month,
            limit_value=limit_value,
            notified_at=now,
            notified_user_ids=notified_user_ids,
            limit_set_at=limit_set_at,
            entity_name=entity_name,
            current_spend=current_spend,
        )
        self.session.add(notification)
        return notification

    def get_notifications_for_entity(
        self,
        entity_type: str,
        entity_id: str,
        month: Optional[str] = None,
    ) -> List[SpendingLimitNotification]:
        """
        Get all notifications for an entity, optionally filtered by month.

        Useful for auditing and debugging.
        """
        query = self.session.query(SpendingLimitNotification).filter(
            SpendingLimitNotification.entity_type == entity_type,
            SpendingLimitNotification.entity_id == entity_id,
        )

        if month is not None:
            query = query.filter(SpendingLimitNotification.month == month)

        return query.order_by(SpendingLimitNotification.notified_at.desc()).all()

    def cleanup_old_notifications(self, months_to_keep: int = 6) -> int:
        """
        Delete notifications older than the specified number of months.

        Args:
            months_to_keep: Number of months to keep (default: 6)

        Returns:
            Number of records deleted
        """
        # Calculate the cutoff month (YYYY-MM format)
        now = datetime.now(timezone.utc)
        # Simple calculation - go back months_to_keep months
        cutoff_month = now.month - months_to_keep
        cutoff_year = now.year
        while cutoff_month <= 0:
            cutoff_month += 12
            cutoff_year -= 1
        cutoff_month_str = f"{cutoff_year:04d}-{cutoff_month:02d}"

        deleted = (
            self.session.query(SpendingLimitNotification)
            .filter(SpendingLimitNotification.month < cutoff_month_str)
            .delete(synchronize_session=False)
        )

        return deleted

    # =========================================================================
    # Organization Query Methods (for notification recipient resolution)
    # =========================================================================

    def get_org_admin_user_ids(self, org_id: int) -> List[str]:
        """
        Get user IDs of organization admins (owners and admins).

        Args:
            org_id: Organization ID

        Returns:
            List of user IDs who have Owner or Admin roles in the organization
        """
        # Get owner and admin role IDs
        admin_roles = (
            self.session.query(Role.id).filter(Role.name.in_(["Owner", "Admin"])).all()
        )
        admin_role_ids = [r.id for r in admin_roles]

        # Get members with admin roles
        members = (
            self.session.query(OrganizationMember)
            .filter(
                OrganizationMember.organization_id == org_id,
                OrganizationMember.role_id.in_(admin_role_ids),
            )
            .all()
        )

        return [m.user_id for m in members]

    def get_org_members_with_assistants(self, org_id: int) -> List[str]:
        """
        Get user IDs of org members who have at least one assistant in the org.

        Args:
            org_id: Organization ID

        Returns:
            List of user IDs who own at least one assistant in the organization
        """
        # Get distinct user IDs of assistants in the org
        assistants_in_org = (
            self.session.query(Assistant.user_id)
            .filter(Assistant.organization_id == org_id)
            .distinct()
            .all()
        )

        return [a.user_id for a in assistants_in_org]
