"""Service for cleaning up all resources when a user account is deleted.

Handles cleanup of:
- Outstanding billing (pay-then-delete)
- Stripe customer and payment methods
- External resources (GCS storage, Twilio phones, Gmail, PubSub topics)
- Cloned voices
- Organization memberships and resource access
- Database records in tables without CASCADE delete
"""

import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional

import stripe
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    Assistant,
    CreditCardFingerprint,
    CustomApiKey,
    CustomEndpoint,
    CustomRouter,
    Embedding,
    LocalEndpoint,
    LogEvent,
    LogEventLog,
    Organization,
    OrganizationMember,
    Project,
    Query,
    QueryTagAssociation,
    Recharge,
    RechargeStatus,
    ResourceAccess,
    Router,
    Tag,
    Users,
)

logger = logging.getLogger(__name__)


@dataclass
class SettlementResult:
    """Result of billing settlement before account deletion."""

    amount_settled: Decimal = Decimal("0")
    invoice_id: Optional[str] = None
    recharges_settled: int = 0


@dataclass
class CleanupResult:
    """Result of cleanup operation with counts and errors."""

    # Billing cleanup
    balance_settled: Optional[SettlementResult] = None
    stripe_customer_deleted: bool = False

    # Org membership cleanup
    org_memberships_cleaned: int = 0
    resource_access_deleted: int = 0

    # Existing cleanup
    assistants_cleaned: int = 0
    projects_cleaned: int = 0
    voices_deleted: int = 0
    embeddings_soft_deleted: int = 0
    legacy_records_deleted: dict = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "balance_settled": {
                "amount": float(self.balance_settled.amount_settled),
                "invoice_id": self.balance_settled.invoice_id,
                "recharges_settled": self.balance_settled.recharges_settled,
            }
            if self.balance_settled
            else None,
            "stripe_customer_deleted": self.stripe_customer_deleted,
            "org_memberships_cleaned": self.org_memberships_cleaned,
            "resource_access_deleted": self.resource_access_deleted,
            "assistants_cleaned": self.assistants_cleaned,
            "projects_cleaned": self.projects_cleaned,
            "voices_deleted": self.voices_deleted,
            "embeddings_soft_deleted": self.embeddings_soft_deleted,
            "legacy_records_deleted": self.legacy_records_deleted,
            "errors": self.errors,
        }


class UserAccountCleanupService:
    """Handles complete cleanup of user resources before account deletion."""

    def __init__(self, session: Session):
        self.session = session

    def cleanup_all_user_resources(self, user_id: str) -> CleanupResult:
        """
        Clean up all resources associated with a user.

        This must be called BEFORE deleting the auth_user record.
        Order matters due to foreign key dependencies.

        Args:
            user_id: The user ID to clean up resources for.

        Returns:
            CleanupResult with counts of deleted resources and any errors.

        Raises:
            ValueError: If outstanding balance cannot be settled.
        """
        result = CleanupResult()

        # 1. Settle outstanding balance (pay-then-delete)
        self._settle_outstanding_balance(user_id, result)

        # 2. Clean up Stripe billing (autorecharge, customer)
        self._cleanup_stripe_billing(user_id, result)

        # 3. Clean up organization memberships
        self._cleanup_org_memberships(user_id, result)

        # 4. Clean up orphan resource access grants
        self._cleanup_resource_access_grants(user_id, result)

        # 5. Clean up all user's assistants (external resources)
        self._cleanup_user_assistants(user_id, result)

        # 6. Clean up cloned voices
        self._cleanup_user_voices(user_id, result)

        # 7. Soft-delete embeddings for all user's projects
        self._soft_delete_user_embeddings(user_id, result)

        # 8. Clean up GCS media from user's project logs
        self._cleanup_project_gcs_media(user_id, result)

        # 9. Delete legacy users table dependencies (no CASCADE)
        self._delete_legacy_user_records(user_id, result)

        return result

    def _settle_outstanding_balance(
        self,
        user_id: str,
        result: CleanupResult,
    ) -> None:
        """
        Settle any outstanding balance before account deletion (pay-then-delete).

        Collects payment for all unpaid recharges (PENDING_INVOICE, INVOICE_CREATED).
        If payment fails, raises an exception to block deletion.

        Args:
            user_id: The user ID.
            result: CleanupResult to update.

        Raises:
            ValueError: If payment fails or no payment method available.
        """
        # Get all unpaid recharges for this user
        unpaid_statuses = [
            RechargeStatus.PENDING_INVOICE,
            RechargeStatus.INVOICE_CREATED,
            RechargeStatus.FAILED,
        ]

        unpaid_recharges = (
            self.session.query(Recharge)
            .filter(
                Recharge.user_id == user_id,
                Recharge.status.in_(unpaid_statuses),
            )
            .all()
        )

        if not unpaid_recharges:
            # No outstanding balance
            return

        # Calculate total owed
        total_owed = sum(Decimal(str(r.amount_usd)) for r in unpaid_recharges)

        if total_owed <= 0:
            return

        # Get user's Stripe customer ID
        user = self.session.query(Users).filter_by(id=user_id).first()
        if not user or not user.stripe_customer_id:
            raise ValueError(
                f"Cannot settle outstanding balance of ${total_owed:.2f}. "
                "No payment method on file.",
            )

        # Configure Stripe
        stripe_key = os.environ.get("STRIPE_SECRET_KEY")
        if not stripe_key:
            raise ValueError(
                "Payment processing unavailable. Contact support to settle "
                f"outstanding balance of ${total_owed:.2f}.",
            )

        stripe.api_key = stripe_key

        # Check for valid payment method
        customer = stripe.Customer.retrieve(user.stripe_customer_id)
        if not customer.invoice_settings.default_payment_method:
            raise ValueError(
                f"Outstanding balance of ${total_owed:.2f} requires payment. "
                "Please add a payment method before deleting your account.",
            )

        # Create and pay final invoice
        try:
            invoice = stripe.Invoice.create(
                customer=user.stripe_customer_id,
                auto_advance=False,
                description="Final settlement - account closure",
                metadata={
                    "user_id": user_id,
                    "type": "account_closure",
                },
            )

            stripe.InvoiceItem.create(
                customer=user.stripe_customer_id,
                amount=int(total_owed * 100),  # cents
                currency="usd",
                description=f"Outstanding balance - {len(unpaid_recharges)} recharge(s)",
                invoice=invoice.id,
            )

            finalized = stripe.Invoice.finalize_invoice(invoice.id)
            paid = stripe.Invoice.pay(invoice.id)

            if paid.status != "paid":
                raise ValueError(
                    f"Payment of ${total_owed:.2f} failed. "
                    "Please update your payment method or contact support.",
                )

            # Update all recharges to PAID
            for recharge in unpaid_recharges:
                recharge.status = RechargeStatus.PAID
                recharge.stripe_invoice_id = finalized.id

            self.session.flush()

            result.balance_settled = SettlementResult(
                amount_settled=total_owed,
                invoice_id=finalized.id,
                recharges_settled=len(unpaid_recharges),
            )

            logger.info(
                f"Settled ${total_owed:.2f} for user {user_id} before account deletion",
            )

        except stripe.error.StripeError as e:
            raise ValueError(
                f"Payment of ${total_owed:.2f} failed: {str(e)}. "
                "Please update your payment method or contact support.",
            )

    def _cleanup_stripe_billing(self, user_id: str, result: CleanupResult) -> None:
        """
        Clean up Stripe billing: disable autorecharge, delete pending items, delete customer.
        """
        user = self.session.query(Users).filter_by(id=user_id).first()
        if not user:
            return

        # Disable autorecharge in DB
        user.autorecharge = False
        self.session.flush()

        if not user.stripe_customer_id:
            return

        stripe_key = os.environ.get("STRIPE_SECRET_KEY")
        if not stripe_key:
            result.errors.append("Stripe cleanup skipped: no API key")
            return

        stripe.api_key = stripe_key

        try:
            # Delete any pending invoice items not attached to an invoice
            pending_items = stripe.InvoiceItem.list(
                customer=user.stripe_customer_id,
                pending=True,
            )
            for item in pending_items.auto_paging_iter():
                try:
                    stripe.InvoiceItem.delete(item.id)
                except stripe.error.StripeError:
                    pass  # Item may already be attached to invoice

            # Void any draft invoices
            draft_invoices = stripe.Invoice.list(
                customer=user.stripe_customer_id,
                status="draft",
            )
            for inv in draft_invoices.auto_paging_iter():
                try:
                    stripe.Invoice.void_invoice(inv.id)
                except stripe.error.StripeError:
                    pass  # May not be voidable

            # Delete the Stripe customer
            stripe.Customer.delete(user.stripe_customer_id)
            result.stripe_customer_deleted = True

            logger.info(f"Deleted Stripe customer {user.stripe_customer_id}")

        except stripe.error.StripeError as e:
            result.errors.append(f"Stripe cleanup error: {str(e)}")

    def _cleanup_org_memberships(self, user_id: str, result: CleanupResult) -> None:
        """
        Clean up organization membership resources before CASCADE delete.

        For each org where user is a member (not owner):
        - Delete unshared resources created by user
        - Revoke resource access grants
        - Mark Contact as non-system
        """
        from orchestra.db.dao.auth_user_dao import AuthUserDAO
        from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
        from orchestra.services.contact_sync_service import ContactSyncService

        # Get user email for Contact cleanup
        auth_user_dao = AuthUserDAO(self.session)
        user_row = auth_user_dao.get_by_id(user_id)
        user_email = user_row[0].email if user_row else None

        # Get all org memberships (excluding owned orgs - those block deletion)
        memberships = (
            self.session.query(OrganizationMember)
            .join(Organization, Organization.id == OrganizationMember.organization_id)
            .filter(
                OrganizationMember.user_id == user_id,
                Organization.owner_id != user_id,  # Not owner
            )
            .all()
        )

        resource_access_dao = ResourceAccessDAO(self.session)
        contact_sync_service = ContactSyncService(self.session)

        for membership in memberships:
            org_id = membership.organization_id

            try:
                # Delete unshared resources created by this user
                resource_access_dao.delete_unshared_resources_by_creator(
                    user_id,
                    org_id,
                )

                # Revoke resource access grants
                resource_access_dao.revoke_user_access_for_organization(user_id, org_id)

                # Mark Contact as non-system
                if user_email:
                    try:
                        contact_sync_service.mark_member_contact_as_non_system(
                            organization_id=org_id,
                            email=user_email,
                        )
                    except Exception:
                        pass  # Contact may not exist

                result.org_memberships_cleaned += 1

            except Exception as e:
                result.errors.append(
                    f"Failed to clean org membership for org {org_id}: {e}",
                )

    def _cleanup_resource_access_grants(
        self,
        user_id: str,
        result: CleanupResult,
    ) -> None:
        """
        Delete orphan ResourceAccess records where user is the grantee.

        ResourceAccess.grantee_id has no FK to auth_user, so these would remain
        as orphans after user deletion.
        """
        deleted = (
            self.session.query(ResourceAccess)
            .filter(
                ResourceAccess.grantee_type == "user",
                ResourceAccess.grantee_id == user_id,
            )
            .delete(synchronize_session="fetch")
        )

        result.resource_access_deleted = deleted
        self.session.flush()

    def _cleanup_user_assistants(self, user_id: str, result: CleanupResult) -> None:
        """Clean up external resources for all user's assistants."""
        from orchestra.services.bucket_service import BucketService
        from orchestra.settings import settings
        from orchestra.web.api.utils.assistant_infra import (
            delete_email,
            delete_phone_number,
            delete_pubsub_topic,
            stop_jobs,
        )

        assistants = (
            self.session.query(Assistant)
            .filter(
                Assistant.user_id == user_id,
                Assistant.organization_id.is_(None),
            )
            .all()
        )

        for assistant in assistants:
            try:
                # Stop running jobs
                try:
                    stop_jobs(str(assistant.agent_id), self.session)
                except Exception as e:
                    result.errors.append(
                        f"Failed to stop jobs for assistant {assistant.agent_id}: {e}",
                    )

                # Delete GCS profile photo
                if assistant.profile_photo and assistant.profile_photo.startswith(
                    "gs://",
                ):
                    try:
                        bucket_service = BucketService()
                        bucket_service.delete_assistant_file(assistant.profile_photo)
                    except Exception as e:
                        result.errors.append(
                            f"Failed to delete profile photo for assistant {assistant.agent_id}: {e}",
                        )

                # Delete GCS profile video
                if assistant.profile_video and assistant.profile_video.startswith(
                    "gs://",
                ):
                    try:
                        bucket_service = BucketService()
                        bucket_service.delete_assistant_file(assistant.profile_video)
                    except Exception as e:
                        result.errors.append(
                            f"Failed to delete profile video for assistant {assistant.agent_id}: {e}",
                        )

                # Delete pubsub topic
                try:
                    delete_pubsub_topic(
                        str(assistant.agent_id),
                        is_staging=settings.is_staging,
                    )
                except Exception as e:
                    result.errors.append(
                        f"Failed to delete pubsub topic for assistant {assistant.agent_id}: {e}",
                    )

                # Delete phone number
                if assistant.phone:
                    try:
                        delete_phone_number(assistant.phone)
                    except Exception as e:
                        result.errors.append(
                            f"Failed to delete phone for assistant {assistant.agent_id}: {e}",
                        )

                # Delete email
                if assistant.email:
                    try:
                        delete_email(assistant.email)
                    except Exception as e:
                        result.errors.append(
                            f"Failed to delete email for assistant {assistant.agent_id}: {e}",
                        )

                result.assistants_cleaned += 1

            except Exception as e:
                result.errors.append(
                    f"Failed to clean up assistant {assistant.agent_id}: {e}",
                )

    def _cleanup_user_voices(self, user_id: str, result: CleanupResult) -> None:
        """Delete cloned voices from external providers."""
        from orchestra.db.models.orchestra_models import Voice

        voices = (
            self.session.query(Voice)
            .filter(
                Voice.user_id == user_id,
                Voice.is_preset == False,  # noqa: E712
            )
            .all()
        )

        for voice in voices:
            try:
                if voice.provider == "elevenlabs":
                    self._delete_elevenlabs_voice(voice.voice_id, result)
                elif voice.provider == "cartesia":
                    self._delete_cartesia_voice(voice.voice_id, result)
                result.voices_deleted += 1
            except Exception as e:
                result.errors.append(
                    f"Failed to delete voice {voice.voice_id} from {voice.provider}: {e}",
                )

    def _delete_elevenlabs_voice(self, voice_id: str, result: CleanupResult) -> None:
        """Delete a cloned voice from ElevenLabs."""
        try:
            from orchestra.services.elevenlabs_service import ElevenLabsService

            service = ElevenLabsService()
            service.delete_voice(voice_id)
        except Exception as e:
            result.errors.append(f"ElevenLabs voice deletion failed: {e}")

    def _delete_cartesia_voice(self, voice_id: str, result: CleanupResult) -> None:
        """Delete a cloned voice from Cartesia."""
        try:
            from orchestra.services.cartesia_service import CartesiaService

            service = CartesiaService()
            service.delete_voice(voice_id)
        except Exception as e:
            result.errors.append(f"Cartesia voice deletion failed: {e}")

    def _soft_delete_user_embeddings(self, user_id: str, result: CleanupResult) -> None:
        """Soft-delete all embeddings for user's projects."""
        from sqlalchemy import select, update

        # Get all project IDs for this user
        user_projects = (
            self.session.query(Project.id).filter(Project.user_id == user_id).all()
        )
        project_ids = [p.id for p in user_projects]

        if not project_ids:
            return

        # Get log event IDs for these projects
        log_events_subquery = (
            select(LogEvent.id).where(LogEvent.project_id.in_(project_ids)).subquery()
        )

        # Soft-delete embeddings
        soft_delete_result = self.session.execute(
            update(Embedding)
            .where(Embedding.ref_id.in_(select(log_events_subquery.c.id)))
            .values(is_deleted=True),
        )
        result.embeddings_soft_deleted = soft_delete_result.rowcount
        self.session.flush()

    def _cleanup_project_gcs_media(self, user_id: str, result: CleanupResult) -> None:
        """Delete GCS media from all user's project logs."""
        from orchestra.db.dao.context_dao import ContextDAO
        from orchestra.db.dao.log_dao import LogDAO
        from orchestra.db.models.orchestra_models import Log

        user_projects = (
            self.session.query(Project).filter(Project.user_id == user_id).all()
        )

        context_dao = ContextDAO(self.session)
        log_dao = LogDAO(self.session, context_dao)

        for project in user_projects:
            try:
                # Get all log event IDs for this project
                log_events_subquery = (
                    self.session.query(LogEvent.id)
                    .filter(LogEvent.project_id == project.id)
                    .subquery()
                )

                # Get logs with GCS media
                logs_query = (
                    self.session.query(Log)
                    .join(LogEventLog, LogEventLog.log_id == Log.id)
                    .filter(
                        LogEventLog.log_event_id.in_(
                            self.session.query(log_events_subquery.c.id),
                        ),
                    )
                )

                log_dao._bulk_delete_gcs_media(logs_query)
                result.projects_cleaned += 1

            except Exception as e:
                result.errors.append(
                    f"Failed to clean GCS media for project {project.id}: {e}",
                )

    def _delete_legacy_user_records(self, user_id: str, result: CleanupResult) -> None:
        """Delete records from legacy users table dependencies (no CASCADE)."""
        legacy_deleted = {
            "custom_endpoints": 0,
            "custom_api_keys": 0,
            "custom_routers": 0,
            "credit_card_fingerprints": 0,
            "query_tag_associations": 0,
            "tags": 0,
            "local_endpoints": 0,
            "queries": 0,
            "routers": 0,
        }

        try:
            # Order matters: delete dependent tables first

            # Delete custom_endpoints (depends on custom_api_key)
            legacy_deleted["custom_endpoints"] = (
                self.session.query(CustomEndpoint)
                .filter(CustomEndpoint.user_id == user_id)
                .delete(synchronize_session="fetch")
            )

            # Delete custom_api_keys
            legacy_deleted["custom_api_keys"] = (
                self.session.query(CustomApiKey)
                .filter(CustomApiKey.user_id == user_id)
                .delete(synchronize_session="fetch")
            )

            # Delete custom_routers
            legacy_deleted["custom_routers"] = (
                self.session.query(CustomRouter)
                .filter(CustomRouter.user_id == user_id)
                .delete(synchronize_session="fetch")
            )

            # Delete credit_card_fingerprints
            legacy_deleted["credit_card_fingerprints"] = (
                self.session.query(CreditCardFingerprint)
                .filter(CreditCardFingerprint.user_id == user_id)
                .delete(synchronize_session="fetch")
            )

            # Delete query_tag_associations (depends on tags)
            legacy_deleted["query_tag_associations"] = (
                self.session.query(QueryTagAssociation)
                .filter(QueryTagAssociation.user_id == user_id)
                .delete(synchronize_session="fetch")
            )

            # Delete tags
            legacy_deleted["tags"] = (
                self.session.query(Tag)
                .filter(Tag.user_id == user_id)
                .delete(synchronize_session="fetch")
            )

            # Delete local_endpoints
            legacy_deleted["local_endpoints"] = (
                self.session.query(LocalEndpoint)
                .filter(LocalEndpoint.user_id == user_id)
                .delete(synchronize_session="fetch")
            )

            # Delete queries
            legacy_deleted["queries"] = (
                self.session.query(Query)
                .filter(Query.user_id == user_id)
                .delete(synchronize_session="fetch")
            )

            # Delete routers
            legacy_deleted["routers"] = (
                self.session.query(Router)
                .filter(Router.user_id == user_id)
                .delete(synchronize_session="fetch")
            )

            # Delete the users record itself
            self.session.query(Users).filter(Users.id == user_id).delete(
                synchronize_session="fetch",
            )

            self.session.flush()
            result.legacy_records_deleted = legacy_deleted

        except Exception as e:
            result.errors.append(f"Failed to delete legacy user records: {e}")
            raise

    def check_deletion_blockers(self, user_id: str) -> List[str]:
        """
        Check for conditions that block account deletion.

        Returns:
            List of blocking reasons. Empty list means deletion is allowed.
        """
        blockers = []

        # Check if user is the owner of any organization
        owned_orgs = (
            self.session.query(Organization)
            .filter(Organization.owner_id == user_id)
            .all()
        )

        if owned_orgs:
            org_names = [org.name for org in owned_orgs]
            blockers.append(
                f"User owns {len(owned_orgs)} organization(s): {', '.join(org_names)}. "
                "Transfer ownership or delete these organizations first.",
            )

        # Check if user is the billing delegate for any org with delegated billing
        delegated_billing_orgs = (
            self.session.query(Organization)
            .filter(
                Organization.billing_user_id == user_id,
                Organization.stripe_customer_id.is_(None),  # Delegated billing
            )
            .all()
        )

        if delegated_billing_orgs:
            org_names = [org.name for org in delegated_billing_orgs]
            blockers.append(
                f"User is the billing delegate for {len(delegated_billing_orgs)} "
                f"organization(s): {', '.join(org_names)}. "
                "Transfer billing responsibility first.",
            )

        return blockers
