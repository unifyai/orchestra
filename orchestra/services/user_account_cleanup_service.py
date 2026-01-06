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
        - User exists in auth_user
        - User exists in users (billing)
        - Has pending bills (PENDING_INVOICE or INVOICE_CREATED)
        - Owns any organizations

        :param user_id: The user's ID
        :return: List of blockers (empty if deletion is allowed)
        """
        result = self.session.execute(
            text(
                """
                SELECT
                    EXISTS(SELECT 1 FROM auth_user WHERE id = :uid) as user_exists,
                    EXISTS(SELECT 1 FROM users WHERE id = :uid) as billing_exists,
                    EXISTS(
                        SELECT 1 FROM recharge
                        WHERE user_id = :uid
                        AND status IN ('PENDING_INVOICE', 'INVOICE_CREATED')
                    ) as has_pending_bills,
                    COALESCE(
                        (SELECT SUM(amount_usd) FROM recharge
                         WHERE user_id = :uid
                         AND status IN ('PENDING_INVOICE', 'INVOICE_CREATED')),
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
        2. Delete from tables with users.id FK (no CASCADE)
        3. Delete from users table (cascades: recharge)
        4. Delete from auth_user table (cascades all auth_user.id FKs)
        5. Archive Stripe customer (post-commit, best-effort)

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

        stripe_customer_id = self.session.execute(
            text("SELECT stripe_customer_id FROM users WHERE id = :uid"),
            {"uid": user_id},
        ).scalar()

        self._delete_users_table_dependencies(user_id)

        self.session.execute(
            text("DELETE FROM users WHERE id = :uid"),
            {"uid": user_id},
        )

        self.session.execute(
            text("DELETE FROM auth_user WHERE id = :uid"),
            {"uid": user_id},
        )

        self.session.commit()

        if stripe_customer_id:
            self._archive_stripe_customer(stripe_customer_id)

        logger.info(f"Successfully deleted user account: {user_id}")
        return DeletionResult(success=True, message="Account deleted successfully")

    def _delete_users_table_dependencies(self, user_id: str) -> None:
        """
        Delete from tables that reference users.id without CASCADE.

        Uses raw SQL - no ORM overhead, no entity loading.
        Order matters due to FK constraints.
        """
        deletion_statements = [
            "DELETE FROM query_tag_association WHERE user_id = :uid",
            "DELETE FROM tags WHERE user_id = :uid",
            "DELETE FROM query WHERE user_id = :uid",
            "DELETE FROM custom_endpoint WHERE user_id = :uid",
            "DELETE FROM custom_api_key WHERE user_id = :uid",
            "DELETE FROM custom_router WHERE user_id = :uid",
            "DELETE FROM credit_card_fingerprint WHERE user_id = :uid",
            "DELETE FROM local_endpoint WHERE user_id = :uid",
            "DELETE FROM router WHERE user_id = :uid",
        ]

        for stmt in deletion_statements:
            self.session.execute(text(stmt), {"uid": user_id})

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
