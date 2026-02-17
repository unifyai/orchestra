"""
Service for deleting user accounts and all associated data.

Uses raw SQL for maximum performance - no ORM entity loading overhead.
All deletions happen in a single transaction for data integrity.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@dataclass
class DeletionBlocker:
    """Represents a condition that blocks account deletion."""

    reason: str
    details: dict


@dataclass
class DeletionResult:
    """Result of a deletion attempt."""

    success: bool
    message: str
    blockers: Optional[list[DeletionBlocker]] = None


class UserAccountCleanupService:
    """
    Service for deleting user accounts with all associated data.

    Performance optimizations:
    - Raw SQL for all operations (no ORM overhead)
    - Single DELETE statement per table (no loops/chunking)
    - Combined blocker checks in single query
    - Database CASCADE handles related tables
    """

    def __init__(self, session: Session):
        self.session = session

    def check_deletion_blockers(self, user_id: str) -> list[DeletionBlocker]:
        """
        Check all conditions that would block account deletion.

        Executes a single optimized query that checks:
        - User exists in user table
        - Has pending bills (PENDING_INVOICE or INVOICE_CREATED)
        - Owns any organizations

        :param user_id: The user's ID
        :return: List of blockers (empty if deletion is allowed)
        """
        result = self.session.execute(
            text(
                """
                SELECT
                    EXISTS(SELECT 1 FROM "user" WHERE id = :uid) as user_exists,
                    EXISTS(
                        SELECT 1 FROM recharge r
                        JOIN "user" u ON u.billing_account_id = r.billing_account_id
                        WHERE u.id = :uid
                        AND r.status IN ('PENDING_INVOICE', 'INVOICE_CREATED')
                    ) as has_pending_bills,
                    COALESCE(
                        (SELECT SUM(r.amount_usd) FROM recharge r
                         JOIN "user" u ON u.billing_account_id = r.billing_account_id
                         WHERE u.id = :uid
                         AND r.status IN ('PENDING_INVOICE', 'INVOICE_CREATED')),
                        0
                    ) as pending_amount,
                    EXISTS(
                        SELECT 1 FROM organization WHERE owner_id = :uid
                    ) as owns_organizations,
                    (SELECT array_agg(name) FROM organization WHERE owner_id = :uid) as owned_org_names
            """,
            ),
            {"uid": user_id},
        ).fetchone()

        blockers = []

        if not result.user_exists:
            blockers.append(
                DeletionBlocker(
                    reason="user_not_found",
                    details={"user_id": user_id},
                ),
            )
            return blockers

        if result.has_pending_bills:
            blockers.append(
                DeletionBlocker(
                    reason="pending_bills",
                    details={
                        "pending_amount_usd": float(result.pending_amount),
                        "message": f"User has ${result.pending_amount:.2f} in pending invoices. "
                        "Please wait for invoices to be processed before deleting account.",
                    },
                ),
            )

        if result.owns_organizations:
            blockers.append(
                DeletionBlocker(
                    reason="organization_owner",
                    details={
                        "organizations": result.owned_org_names or [],
                        "message": "User owns organizations. "
                        "Transfer ownership before deleting account.",
                    },
                ),
            )

        return blockers

    def delete_user_account(
        self,
        user_id: str,
        force_org_check: bool = False,
    ) -> DeletionResult:
        """
        Delete a user account and all associated data.

        Deletion order (within single transaction):
        1. Check blockers
        2. Delete from tables with user.id FK (no CASCADE)
        3. Delete from user table (cascades all user.id FKs)
        4. Archive Stripe customer (post-commit, best-effort)

        :param user_id: The user's ID
        :param force_org_check: If True, skip organization ownership check
        :return: DeletionResult with success status and message
        """
        blockers = self.check_deletion_blockers(user_id)

        if force_org_check:
            blockers = [b for b in blockers if b.reason != "organization_owner"]

        if blockers:
            first_blocker = blockers[0]
            return DeletionResult(
                success=False,
                message=first_blocker.details.get("message", first_blocker.reason),
                blockers=blockers,
            )

        # Get the billing account info before deletion
        ba_info = self.session.execute(
            text(
                """
                SELECT ba.id as ba_id, ba.stripe_customer_id
                FROM "user" u
                LEFT JOIN billing_account ba ON u.billing_account_id = ba.id
                WHERE u.id = :uid
                """,
            ),
            {"uid": user_id},
        ).fetchone()

        stripe_customer_id = ba_info.stripe_customer_id if ba_info else None
        billing_account_id = ba_info.ba_id if ba_info else None

        self._delete_user_table_dependencies(user_id, billing_account_id)

        self.session.execute(
            text('DELETE FROM "user" WHERE id = :uid'),
            {"uid": user_id},
        )

        # Delete the billing account if it exists (cascade will handle recharges/fingerprints)
        if billing_account_id:
            self.session.execute(
                text("DELETE FROM billing_account WHERE id = :ba_id"),
                {"ba_id": billing_account_id},
            )

        self.session.commit()

        # Post-commit cleanup operations (best-effort, don't block on failure)
        if stripe_customer_id:
            self._archive_stripe_customer(stripe_customer_id)

        self._cleanup_user_attachments(user_id)

        logger.info(f"Successfully deleted user account: {user_id}")
        return DeletionResult(success=True, message="Account deleted successfully")

    def _delete_user_table_dependencies(
        self,
        user_id: str,
        billing_account_id: int | None = None,
    ) -> None:
        """
        Delete from tables that reference user.id without CASCADE.

        Uses raw SQL - no ORM overhead, no entity loading.
        Order matters due to FK constraints.

        Note: credit_card_fingerprint and recharge now reference billing_account_id
        and will be cascade-deleted when the billing_account row is removed.
        """
        # Currently no non-cascading tables reference user.id directly.
        # credit_card_fingerprint is cascade-deleted via billing_account.

    def _archive_stripe_customer(self, stripe_customer_id: str) -> None:
        """
        Archive Stripe customer after account deletion.

        Best-effort operation - logs errors but doesn't fail the deletion.
        Archiving (vs deleting) preserves historical invoice records in Stripe.
        """
        try:
            import stripe

            stripe.Customer.modify(
                stripe_customer_id,
                metadata={
                    "account_deleted": "true",
                    "deleted_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            logger.info(f"Archived Stripe customer: {stripe_customer_id}")
        except Exception as e:
            logger.error(f"Failed to archive Stripe customer {stripe_customer_id}: {e}")

    def _cleanup_user_attachments(self, user_id: str) -> None:
        """
        Delete user's message attachments from GCS.

        Best-effort operation - logs errors but doesn't fail the deletion.
        Attachments are stored in the unify-message-attachments bucket with
        user-scoped paths: {user_id}/{attachment_id}_{filename}
        """
        try:
            from orchestra.services.bucket_service import BucketService

            bucket_service = BucketService()
            deleted_count = bucket_service.delete_message_attachments_for_user(user_id)
            if deleted_count > 0:
                logger.info(
                    f"Cleaned up {deleted_count} message attachments for user {user_id}",
                )
        except Exception as e:
            logger.error(f"Failed to cleanup attachments for user {user_id}: {e}")
