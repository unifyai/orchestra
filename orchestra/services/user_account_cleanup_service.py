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

from orchestra.web.api.utils.assistant_infra import teardown_assistant_runtime_sync

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
        - Has disputed recharges (DISPUTED)
        - Billing account is in SUSPENDED or CLOSED state
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
                        SELECT 1 FROM recharge r
                        JOIN "user" u ON u.billing_account_id = r.billing_account_id
                        WHERE u.id = :uid
                        AND r.status = 'DISPUTED'
                    ) as has_disputed_charges,
                    (
                        SELECT ba.account_status FROM billing_account ba
                        JOIN "user" u ON u.billing_account_id = ba.id
                        WHERE u.id = :uid
                    ) as account_status,
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

        if result.has_disputed_charges:
            blockers.append(
                DeletionBlocker(
                    reason="open_disputes",
                    details={
                        "message": "User has open payment disputes. "
                        "Please wait for disputes to be resolved before deleting account.",
                    },
                ),
            )

        if result.account_status in ("SUSPENDED", "CLOSED"):
            blockers.append(
                DeletionBlocker(
                    reason="account_status",
                    details={
                        "account_status": result.account_status,
                        "message": f"User's billing account is {result.account_status}. "
                        "Please resolve outstanding billing issues before deleting account.",
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

        # Collect assistant runtime metadata *before* deleting the user row
        # (CASCADE will remove the assistants rows).
        assistant_runtime_specs = self._get_user_assistant_runtime_specs(user_id)
        assistant_ids = [int(spec["assistant_id"]) for spec in assistant_runtime_specs]

        # Soft-delete and deprovision all contacts for this user's personal
        # assistants *before* the CASCADE deletes the rows.
        self._deprovision_user_contacts(user_id)

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

        self._teardown_user_assistant_runtimes(
            assistant_runtime_specs,
            user_id=user_id,
        )
        self._cleanup_user_data(user_id, assistant_ids)

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

        Note: recharge references billing_account_id and will be
        cascade-deleted when the billing_account row is removed.
        """
        # Currently no non-cascading tables reference user.id directly.

    def _deprovision_user_contacts(self, user_id: str) -> None:
        """Soft-delete and deprovision all contacts for the user's personal assistants.

        Must be called *before* the user row is deleted (CASCADE would
        hard-delete the contact rows otherwise).

        Deprovisioning (Twilio / Google Workspace API calls) is best-effort
        – failures are logged but do not block user deletion.
        """
        try:
            from orchestra.db.dao.assistant_contact_dao import AssistantContactDAO

            contact_dao = AssistantContactDAO(self.session)

            # Collect personal assistant IDs
            assistant_ids = [
                row[0]
                for row in self.session.execute(
                    text(
                        "SELECT agent_id FROM assistants "
                        "WHERE user_id = :uid AND organization_id IS NULL",
                    ),
                    {"uid": user_id},
                ).fetchall()
            ]
            if not assistant_ids:
                return

            # Fetch active contacts before soft-deleting
            active_contacts = contact_dao.get_active_contacts_for_assistants(
                assistant_ids,
            )

            # Deprovision external resources (best-effort)
            import asyncio

            from orchestra.routines.assistant_contact_suspension import (
                _deprovision_contact,
            )

            for contact in active_contacts:
                try:
                    asyncio.get_event_loop().run_until_complete(
                        _deprovision_contact(contact),
                    )
                except RuntimeError:
                    # No event loop – create one
                    asyncio.run(_deprovision_contact(contact))
                except Exception as e:
                    logger.error(
                        f"Failed to deprovision {contact.contact_type} "
                        f"({contact.contact_value}) for user {user_id}: {e}",
                    )

            # Soft-delete all contact rows
            contact_dao.soft_delete_contacts_for_user(user_id)

        except Exception as e:
            logger.error(
                f"Failed to deprovision contacts for user {user_id}: {e}",
            )

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

    def _get_user_assistant_ids(self, user_id: str) -> list[int]:
        """
        Return all assistant ``agent_id`` values owned by *user_id*.

        Must be called **before** the user row is deleted (CASCADE would
        remove the assistants rows).
        """
        rows = self.session.execute(
            text("SELECT agent_id FROM assistants WHERE user_id = :uid"),
            {"uid": user_id},
        ).fetchall()
        return [row[0] for row in rows] if rows else []

    def _get_user_assistant_runtime_specs(
        self,
        user_id: str,
    ) -> list[dict[str, str | int | None]]:
        """
        Return assistant runtime metadata owned by *user_id* before CASCADE delete.
        """
        rows = self.session.execute(
            text(
                "SELECT agent_id, deploy_env, desktop_mode "
                "FROM assistants WHERE user_id = :uid",
            ),
            {"uid": user_id},
        ).fetchall()
        if not rows:
            return []

        specs: list[dict[str, str | int | None]] = []
        for row in rows:
            deploy_env = row[1] if len(row) > 1 else None
            desktop_mode = row[2] if len(row) > 2 else None
            specs.append(
                {
                    "assistant_id": row[0],
                    "deploy_env": deploy_env,
                    "desktop_mode": desktop_mode,
                },
            )
        return specs

    def _teardown_user_assistant_runtimes(
        self,
        assistant_runtime_specs: list[dict[str, str | int | None]],
        *,
        user_id: str,
    ) -> None:
        """Best-effort runtime teardown for assistants deleted with the user."""
        for spec in assistant_runtime_specs:
            try:
                result = teardown_assistant_runtime_sync(
                    spec["assistant_id"],
                    deploy_env=spec["deploy_env"],
                    desktop_mode=spec["desktop_mode"],
                )
            except Exception as e:
                logger.error(
                    "Failed runtime teardown for assistant %s during user deletion %s: %s",
                    spec["assistant_id"],
                    user_id,
                    e,
                )
                continue

            if result.get("errors"):
                logger.error(
                    "Runtime teardown cleanup issues for assistant %s during user deletion %s: %s",
                    spec["assistant_id"],
                    user_id,
                    result["errors"],
                )

    def _cleanup_user_data(
        self,
        user_id: str,
        assistant_ids: list[int],
    ) -> None:
        """
        Delete all GCS data for every assistant owned by a user, plus the
        user's account photos.

        *assistant_ids* is pre-fetched before the DB commit (since CASCADE
        deletes the rows).  If the list is empty we fall back to the legacy
        user-prefix cleanup.

        Best-effort operation – logs errors but never fails the deletion.
        """
        try:
            from orchestra.services.bucket_service import BucketService

            bucket_service = BucketService()

            if assistant_ids:
                total = {"media": 0, "recordings": 0, "attachments": 0}
                for aid in assistant_ids:
                    try:
                        counts = bucket_service.delete_all_assistant_data(aid)
                        for key in total:
                            total[key] += counts.get(key, 0)
                    except Exception as e:
                        logger.error(
                            f"Failed to cleanup GCS data for assistant {aid} "
                            f"(user {user_id}): {e}",
                        )
                grand_total = sum(total.values())
                if grand_total > 0:
                    logger.info(
                        f"Cleaned up {grand_total} GCS file(s) across "
                        f"{len(assistant_ids)} assistant(s) for user {user_id}: {total}",
                    )
            else:
                # Fallback: no assistants found (user had none, or they were
                # already cleaned up).  Try the legacy user-prefix cleanup.
                deleted_count = bucket_service.delete_message_attachments_for_user(
                    user_id,
                )
                if deleted_count > 0:
                    logger.info(
                        f"Cleaned up {deleted_count} legacy attachment(s) for user {user_id}",
                    )

            # Clean up account photos from the dedicated account photo bucket
            try:
                photo_count = bucket_service.delete_user_account_photos(user_id)
                if photo_count > 0:
                    logger.info(
                        f"Cleaned up {photo_count} account photo(s) for user {user_id}",
                    )
            except Exception as e:
                logger.error(
                    f"Failed to cleanup account photos for user {user_id}: {e}",
                )
        except Exception as e:
            logger.error(f"Failed to cleanup GCS data for user {user_id}: {e}")
