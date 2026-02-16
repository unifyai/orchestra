"""Service for handling spending limit notifications."""

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional

from sqlalchemy.orm import Session

from orchestra.db.dao.assistant_dao import AssistantDAO
from orchestra.db.dao.organization_dao import OrganizationDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.spending_limit_notification_dao import (
    SpendingLimitNotificationDAO,
)
from orchestra.db.dao.user_dao import UserDAO
from orchestra.web.api.utils.email import send_email_async

logger = logging.getLogger(__name__)


@dataclass
class NotificationRecipient:
    """A recipient of a spending limit notification."""

    user_id: str
    email: str
    is_org_admin: bool = False


@dataclass
class NotificationResult:
    """Result of processing a spending limit notification."""

    notified: bool
    reason: Optional[str] = None
    recipient_count: Optional[int] = None
    notified_user_ids: Optional[List[str]] = None


class SpendingLimitNotificationService:
    """
    Service for handling spending limit notifications.

    Responsibilities:
    - Determining notification recipients based on entity type
    - Building email content
    - Orchestrating the notification flow (deduplication, sending, recording)
    """

    def __init__(self, session: Session):
        self.session = session
        self.notification_dao = SpendingLimitNotificationDAO(session)
        self.assistant_dao = AssistantDAO(session)
        self.user_dao = UserDAO(session)
        self.org_dao = OrganizationDAO(session)
        self.org_member_dao = OrganizationMemberDAO(session)

    def process_limit_reached(
        self,
        limit_type: str,
        entity_id: str,
        limit_value: float,
        current_spend: float,
        month: str,
        limit_set_at: Optional[object] = None,
        entity_name: Optional[str] = None,
        organization_id: Optional[int] = None,
    ) -> NotificationResult:
        """
        Process a spending limit reached event.

        This method:
        1. Checks deduplication
        2. Gets recipients
        3. Sends emails (fire-and-forget)
        4. Records the notification

        Args:
            limit_type: Type of limit ('assistant', 'user', 'member', 'organization')
            entity_id: ID of the entity that hit the limit
            limit_value: The limit value that was reached
            current_spend: Current spend amount
            month: Billing month in YYYY-MM format
            limit_set_at: When the limit was last configured (optional)
            entity_name: Name of the entity (optional, for email content)

        Returns:
            NotificationResult with outcome details
        """
        # Normalize limit value for consistent comparison
        normalized_limit = Decimal(str(limit_value)).quantize(Decimal("0.01"))

        # Check deduplication
        if not self.notification_dao.should_notify(
            entity_type=limit_type,
            entity_id=entity_id,
            month=month,
            limit_value=normalized_limit,
            limit_set_at=limit_set_at,
        ):
            return NotificationResult(notified=False, reason="already_notified")

        # Get recipients and resolve entity name
        recipients, resolved_entity_name = self._get_recipients(
            limit_type=limit_type,
            entity_id=entity_id,
            entity_name=entity_name,
            organization_id=organization_id,
        )

        if not recipients:
            return NotificationResult(notified=False, reason="no_recipients")

        # Send emails (fire-and-forget)
        notified_user_ids = []
        for recipient in recipients:
            subject, body = self._build_email(
                entity_type=limit_type,
                entity_name=resolved_entity_name,
                limit_value=limit_value,
                current_spend=current_spend,
                is_org_admin=recipient.is_org_admin,
            )
            # Send from noreply@unify.ai (an alias), but impersonate hello@unify.ai (the real user)
            asyncio.create_task(
                send_email_async(
                    recipient.email,
                    subject,
                    body,
                    from_email="noreply@unify.ai",
                    impersonate_email="hello@unify.ai",
                ),
            )
            notified_user_ids.append(recipient.user_id)

        # Record the notification for deduplication
        self.notification_dao.record_notification(
            entity_type=limit_type,
            entity_id=entity_id,
            month=month,
            limit_value=normalized_limit,
            notified_user_ids=notified_user_ids,
            limit_set_at=limit_set_at,
            entity_name=resolved_entity_name,
            current_spend=Decimal(str(current_spend)).quantize(Decimal("0.01")),
        )

        return NotificationResult(
            notified=True,
            recipient_count=len(recipients),
            notified_user_ids=notified_user_ids,
        )

    def _get_recipients(
        self,
        limit_type: str,
        entity_id: str,
        entity_name: Optional[str] = None,
        organization_id: Optional[int] = None,
    ) -> tuple[List[NotificationRecipient], str]:
        """
        Get notification recipients based on entity type.

        Args:
            limit_type: Type of limit
            entity_id: ID of the entity
            entity_name: Optional entity name (will be resolved if not provided)
            organization_id: Optional org ID (required for member limits)

        Returns:
            Tuple of (list of recipients, resolved entity name)
        """
        recipients: List[NotificationRecipient] = []
        resolved_name = entity_name or "Unknown"

        if limit_type == "assistant":
            recipients, resolved_name = self._get_assistant_recipients(
                entity_id,
                entity_name,
            )

        elif limit_type == "user":
            recipients, resolved_name = self._get_user_recipients(
                entity_id,
                entity_name,
            )

        elif limit_type == "member":
            recipients, resolved_name = self._get_member_recipients(
                entity_id,
                entity_name,
                organization_id,
            )

        elif limit_type == "organization":
            recipients, resolved_name = self._get_org_recipients(entity_id, entity_name)

        return recipients, resolved_name

    def _get_assistant_recipients(
        self,
        entity_id: str,
        entity_name: Optional[str],
    ) -> tuple[List[NotificationRecipient], str]:
        """Get recipients for assistant limit notification."""
        recipients: List[NotificationRecipient] = []
        resolved_name = entity_name or "Unknown"

        assistant = self.assistant_dao.get_assistant_by_agent_id(int(entity_id))
        if assistant:
            user_row = self.user_dao.get_by_id(assistant.user_id)
            if user_row:
                user = user_row[0]
                if user.email:
                    recipients.append(
                        NotificationRecipient(user_id=user.id, email=user.email),
                    )

            if not entity_name or entity_name == "Unknown":
                name_parts = [assistant.first_name or "", assistant.surname or ""]
                resolved_name = " ".join(name_parts).strip() or "Assistant"

        return recipients, resolved_name

    def _get_user_recipients(
        self,
        entity_id: str,
        entity_name: Optional[str],
    ) -> tuple[List[NotificationRecipient], str]:
        """Get recipients for user limit notification."""
        recipients: List[NotificationRecipient] = []
        resolved_name = entity_name or "Unknown"

        user_row = self.user_dao.get_by_id(entity_id)
        if user_row:
            user = user_row[0]
            if user.email:
                recipients.append(
                    NotificationRecipient(user_id=user.id, email=user.email),
                )

                if not entity_name or entity_name == "Unknown":
                    name_parts = [user.name or "", user.last_name or ""]
                    resolved_name = " ".join(name_parts).strip() or "Your account"

        return recipients, resolved_name

    def _get_member_recipients(
        self,
        entity_id: str,
        entity_name: Optional[str],
        organization_id: Optional[int] = None,
    ) -> tuple[List[NotificationRecipient], str]:
        """Get recipients for member limit notification.

        For member limits, entity_id is the user_id and organization_id
        identifies which org's member limit was reached.
        """
        recipients: List[NotificationRecipient] = []
        resolved_name = entity_name or "Unknown"

        # entity_id is the user_id for member limits
        user_id = entity_id

        if organization_id is not None:
            # Use get_member with user_id and org_id
            member = self.org_member_dao.get_member(user_id, organization_id)
            if member:
                user_row = self.user_dao.get_by_id(user_id)
                if user_row:
                    user = user_row[0]
                    if user.email:
                        recipients.append(
                            NotificationRecipient(user_id=user.id, email=user.email),
                        )
        else:
            # Fallback: just get user by user_id
            user_row = self.user_dao.get_by_id(user_id)
            if user_row:
                user = user_row[0]
                if user.email:
                    recipients.append(
                        NotificationRecipient(user_id=user.id, email=user.email),
                    )

        return recipients, resolved_name

    def _get_org_recipients(
        self,
        entity_id: str,
        entity_name: Optional[str],
    ) -> tuple[List[NotificationRecipient], str]:
        """Get recipients for organization limit notification."""
        recipients: List[NotificationRecipient] = []
        resolved_name = entity_name or "Unknown"

        org_id = int(entity_id)
        org = self.org_dao.get(org_id)

        if org:
            if not entity_name or entity_name == "Unknown":
                resolved_name = org.name or "Organization"

            # Get org admins for special messaging
            admin_user_ids = set(
                self.notification_dao.get_org_admin_user_ids(org_id),
            )

            # Get all members with assistants
            member_user_ids = self.notification_dao.get_org_members_with_assistants(
                org_id,
            )

            for user_id in member_user_ids:
                user_row = self.user_dao.get_by_id(user_id)
                if user_row:
                    user = user_row[0]
                    if user.email:
                        is_admin = user_id in admin_user_ids
                        recipients.append(
                            NotificationRecipient(
                                user_id=user.id,
                                email=user.email,
                                is_org_admin=is_admin,
                            ),
                        )

        return recipients, resolved_name

    def _build_email(
        self,
        entity_type: str,
        entity_name: str,
        limit_value: float,
        current_spend: float,
        is_org_admin: bool = False,
    ) -> tuple[str, str]:
        """
        Build email subject and body for a spending limit notification.

        Args:
            entity_type: Type of limit ('assistant', 'user', 'member', 'organization')
            entity_name: Name of the entity that hit the limit
            limit_value: The limit value
            current_spend: Current spend amount
            is_org_admin: Whether the recipient is an org admin (gets extra info)

        Returns:
            Tuple of (subject, body)
        """
        subject = f"{entity_type.capitalize()} Spending Limit Reached"

        entity_label = {
            "assistant": "assistant",
            "user": "account",
            "member": "organization membership",
            "organization": "organization",
        }.get(entity_type, entity_type)

        body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <h2 style="color: #d97706;">Monthly Spending Limit Reached</h2>

            <p>Your {entity_label} <strong>{entity_name}</strong> has reached its monthly spending limit.</p>

            <table style="border-collapse: collapse; margin: 20px 0;">
                <tr>
                    <td style="padding: 8px 16px; border: 1px solid #ddd; background: #f9f9f9;">
                        <strong>Limit:</strong>
                    </td>
                    <td style="padding: 8px 16px; border: 1px solid #ddd;">
                        ${limit_value:.2f}
                    </td>
                </tr>
                <tr>
                    <td style="padding: 8px 16px; border: 1px solid #ddd; background: #f9f9f9;">
                        <strong>Current Spend:</strong>
                    </td>
                    <td style="padding: 8px 16px; border: 1px solid #ddd;">
                        ${current_spend:.2f}
                    </td>
                </tr>
            </table>

            <p style="color: #d97706;">
                <strong>⚠️ Important:</strong> Your assistant's current work could be stopped due to this limit.
                New operations will be blocked until the next billing period or until the limit is increased.
            </p>
        """

        if is_org_admin:
            body += """
            <p>
                <strong>As an organization administrator</strong>, you can update the organization's
                spending limits in your organization settings.
            </p>
            """

        body += """
            <hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">
            <p style="font-size: 12px; color: #888;">
                This is an automated notification. Please do not reply to this email.
            </p>
        </body>
        </html>
        """

        return subject, body
