from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional

import stripe
from fastapi import APIRouter, HTTPException, Query
from fastapi.param_functions import Depends
from sqlalchemy.orm import Session

from orchestra.db.dao.billing_account_dao import (
    MIN_AUTORECHARGE_AMOUNT,
    MIN_SPEND_FOR_AUTO_RECHARGE,
    BillingAccountDAO,
)
from orchestra.db.dao.organization_dao import OrganizationDAO
from orchestra.db.dao.organization_invite_dao import OrganizationInviteDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.recharge_dao import RechargeDAO
from orchestra.db.dao.recharge_type_dao import RechargeTypeDAO
from orchestra.db.dao.space_invite_dao import SpaceInviteDAO
from orchestra.db.dao.user_dao import UserDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import (
    AssistantCleanupTask,
    BillingAccount,
    Organization,
    Recharge,
    RechargeStatus,
    RechargeType,
    User,
)
from orchestra.services.assistant_cleanup_service import (
    DEFAULT_CLEANUP_TASK_BATCH_SIZE,
    MAX_CLEANUP_TASK_BATCH_SIZE,
    process_assistant_cleanup_tasks,
)
from orchestra.settings import settings
from orchestra.web.api.admin.schema import (  # noqa: WPS235
    AssistantContactCostRead,
    AssistantContactCostWrite,
    OrganizationListItem,
    OrganizationListResponse,
    RechargeModelRequest,
    RechargeModelResponse,
    RechargeTypeModelRequest,
    RechargeTypeModelResponse,
    SuspensionReason,
    UsersModelResponse,
)

router = APIRouter()


def _resolve_billing_account(
    session: Session,
    user_id: Optional[str] = None,
    organization_id: Optional[int] = None,
) -> BillingAccount:
    """
    Resolve a BillingAccount from either a user_id or an organization_id.

    Exactly one of the two parameters must be provided.
    Delegates to :class:`BillingAccountDAO` for the actual lookup.

    :param session: Database session.
    :param user_id: User ID (for personal billing accounts).
    :param organization_id: Organization ID (for org billing accounts).
    :return: The resolved BillingAccount.
    :raises HTTPException: If neither/both provided, or entity not found.
    """
    if user_id and organization_id:
        raise HTTPException(
            status_code=400,
            detail="Provide either user_id or organization_id, not both.",
        )
    if not user_id and not organization_id:
        raise HTTPException(
            status_code=400,
            detail="Provide either user_id or organization_id.",
        )

    ba_dao = BillingAccountDAO(session)

    if user_id:
        ba = ba_dao.resolve_for_user(user_id)
        if ba is None:
            raise HTTPException(
                status_code=404,
                detail=f"User {user_id} not found or has no billing account.",
            )
        return ba

    ba = ba_dao.resolve_for_org(organization_id)
    if ba is None:
        raise HTTPException(
            status_code=404,
            detail=f"Organization {organization_id} not found or has no billing account.",
        )
    return ba


@router.get("/get_all_users", response_model=List[UsersModelResponse])
def get_all_users_models(
    session=Depends(get_db_session),
) -> List[User]:
    """
    Retrieve all users objects from the database.

    :param user_dao: DAO for users models.
    :return: list of users objects from database.
    """
    user_dao = UserDAO(session)
    return user_dao.get_all_users()


@router.get(
    "/organizations",
    response_model=OrganizationListResponse,
    summary="Admin: List all organizations",
    description="Retrieve all organizations with pagination support.",
)
def admin_list_organizations(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    name: Optional[str] = Query(None, description="Filter by name (partial match)"),
    session=Depends(get_db_session),
) -> OrganizationListResponse:
    """
    List all organizations in the system with pagination.

    :param limit: Maximum number of results (1-1000).
    :param offset: Number of results to skip.
    :param name: Optional partial name match filter.
    :param session: Database session.
    :return: Paginated list of organizations with member counts.
    """
    org_dao = OrganizationDAO(session)
    member_dao = OrganizationMemberDAO(session)
    user_dao = UserDAO(session)

    orgs = org_dao.list_all(limit=limit, offset=offset, name_filter=name)

    # Batch-resolve owner emails
    owner_ids = list({org.owner_id for org in orgs})
    owner_email_map: dict[str, str] = {}
    for uid in owner_ids:
        row = user_dao.get_by_id(uid)
        if row:
            owner_email_map[uid] = row[0].email

    items = []
    for org in orgs:
        member_count = member_dao.count_members(org.id)
        items.append(
            OrganizationListItem(
                id=org.id,
                name=org.name,
                owner_id=org.owner_id,
                owner_email=owner_email_map.get(org.owner_id),
                created_at=org.created_at,
                member_count=member_count,
            ),
        )

    return OrganizationListResponse(
        organizations=items,
        limit=limit,
        offset=offset,
    )


@router.get("/get_user", response_model=List[UsersModelResponse])
def get_user(
    id: str,  # noqa: WPS125
    session=Depends(get_db_session),
) -> List[User]:
    """
    Retrieve specific users object from the database.

    :param id: id of users instance.
    :param user_dao: DAO for users models.
    :return: list of users objects from database.
    """
    user_dao = UserDAO(session)
    return user_dao.filter(id=id)


@router.get("/get_all_recharge_types", response_model=List[RechargeTypeModelResponse])
def get_recharge_type_models(
    limit: int = 10,
    offset: int = 0,
    session=Depends(get_db_session),
) -> List[RechargeType]:
    """
    Retrieve all recharge_type objects from the database.

    :param limit: limit of recharge_type objects, defaults to 10.
    :param offset: offset of recharge_type objects, defaults to 0.
    :param recharge_type_dao: DAO for recharge_type models.
    :return: list of recharge_type objects from database.
    """
    recharge_type_dao = RechargeTypeDAO(session)
    return recharge_type_dao.get_all_recharge_types(limit=limit, offset=offset)


@router.get("/get_recharge_type", response_model=List[RechargeTypeModelResponse])
def get_recharge_type(
    type: str,  # noqa: WPS125
    session=Depends(get_db_session),
) -> List[RechargeType]:
    """
    Retrieve specific recharge_type object from the database.

    :param type: type of recharge_type object.
    :param recharge_type_dao: DAO for recharge_type models.
    :return: recharge_type object from database.
    """
    recharge_type_dao = RechargeTypeDAO(session)
    return recharge_type_dao.filter(type=type)


@router.get("/get_all_recharges", response_model=List[RechargeModelResponse])
def get_recharge_models(
    limit: int = 10,
    offset: int = 0,
    session=Depends(get_db_session),
) -> List[Recharge]:
    """
    Retrieve all recharge objects from the database.

    :param limit: limit of recharge objects, defaults to 10.
    :param offset: offset of recharge objects, defaults to 0.
    :param recharge_dao: DAO for recharge models.
    :return: list of recharge objects from database.
    """
    recharge_dao = RechargeDAO(session)
    return recharge_dao.get_all_recharges(limit=limit, offset=offset)


@router.get("/get_recharge", response_model=List[RechargeModelResponse])
def get_recharge(  # noqa: WPS211
    id: Optional[int] = None,  # noqa: WPS125
    at: Optional[datetime] = None,
    billing_account_id: Optional[int] = None,
    quantity: Optional[int] = None,
    type: Optional[str] = None,  # noqa: WPS125
    session=Depends(get_db_session),
) -> List[Recharge]:
    """
    Retrieve specific recharge object from the database.

    :param id: id of recharge instance.
    :param at: at of recharge instance.
    :param billing_account_id: billing_account_id of recharge instance.
    :param quantity: quantity of recharge instance.
    :param type: type of recharge instance.
    :param recharge_dao: DAO for recharge models.
    :return: list of recharge objects from database.
    """
    recharge_dao = RechargeDAO(session)
    return recharge_dao.filter(
        id=id,
        at=at,
        billing_account_id=billing_account_id,
        quantity=quantity,
        type=type,
    )


@router.post("/create_recharge")
def create_recharge_model(
    new_recharge_object: RechargeModelRequest,
    session=Depends(get_db_session),
) -> None:
    """
    Create a recharge record and credit the billing account.

    The request body accepts ``user_id`` and/or ``organization_id``.  Exactly
    one must be provided.  The billing account is resolved from whichever is
    given.

    :param new_recharge_object: Recharge details (see schema).
    """
    import logging

    logger = logging.getLogger(__name__)

    entity_label = (
        f"user={new_recharge_object.user_id}"
        if new_recharge_object.user_id
        else f"org={new_recharge_object.organization_id}"
    )
    logger.info(
        f"Creating recharge - {entity_label}, "
        f"Type: {new_recharge_object.type}, "
        f"Quantity: {new_recharge_object.quantity}",
    )

    recharge_dao = RechargeDAO(session)

    if (
        new_recharge_object.type == "payment"
        and new_recharge_object.transaction_id is None
    ):
        raise HTTPException(
            status_code=400,
            detail="Transaction id must be specified when adding a payment.",
        )

    MAX_PROMO_AMOUNT = settings.max_promo_amount
    if (
        new_recharge_object.type == "promo"
        and new_recharge_object.quantity > MAX_PROMO_AMOUNT
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Promo recharges are capped at ${MAX_PROMO_AMOUNT}. "
                f"Requested: ${new_recharge_object.quantity:.2f}"
            ),
        )

    # Resolve billing account from user_id or organization_id
    ba = _resolve_billing_account(
        session,
        user_id=new_recharge_object.user_id,
        organization_id=new_recharge_object.organization_id,
    )

    at = datetime.now(timezone.utc)

    # Credit the billing account
    ba_dao = BillingAccountDAO(session)
    ba_dao.add_credits(
        ba.id,
        float(new_recharge_object.quantity),
        category="recharge" if new_recharge_object.type != "promo" else "promo",
        user_id=new_recharge_object.user_id,
        organization_id=(
            new_recharge_object.organization_id
            if hasattr(new_recharge_object, "organization_id")
            else None
        ),
        description=f"Admin recharge ({new_recharge_object.type})",
        detail={"event": "admin_recharge", "type": new_recharge_object.type},
    )

    # Calculate amount_usd and invoice_group
    amount_usd = new_recharge_object.quantity

    if new_recharge_object.target_month:
        try:
            year, month = map(int, new_recharge_object.target_month.split("-"))
            target_date = datetime(year, month, 1, tzinfo=timezone.utc)
            first_next_month = (
                target_date.replace(day=1) + timedelta(days=32)
            ).replace(day=1)
            invoice_group = (first_next_month - timedelta(microseconds=1)).date()
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Invalid target_month format. Use 'YYYY-MM' (e.g., '2025-06')",
            )
    else:
        first_next_month = (at.replace(day=1) + timedelta(days=32)).replace(day=1)
        invoice_group = (first_next_month - timedelta(microseconds=1)).date()

    # Set status based on recharge type
    if new_recharge_object.type in ["payment", "promo"]:
        status = RechargeStatus.PAID
    else:
        status = RechargeStatus.PENDING_INVOICE

    # For "auto" recharges, also create Stripe invoice item immediately
    if new_recharge_object.type == "auto":
        stripe_cid = ba.stripe_customer_id
        logger.info(
            f"Processing auto recharge for {entity_label}, "
            f"Stripe Customer ID: {stripe_cid}",
        )

        if stripe_cid:
            try:
                stripe_key = settings.stripe_secret_key
                if not stripe_key:
                    raise ValueError("STRIPE_SECRET_KEY environment variable not set")

                stripe.api_key = stripe_key
                quantity = int(new_recharge_object.quantity)

                if quantity > 0:
                    invoice_item = stripe.InvoiceItem.create(
                        customer=stripe_cid,
                        amount=int(new_recharge_object.quantity * 100),
                        currency="usd",
                        description=f"{new_recharge_object.quantity} credits",
                        metadata={
                            "recharge_type": "auto",
                            "billing_account_id": str(ba.id),
                            "invoice_group": str(invoice_group),
                        },
                    )
                    logger.info(
                        f"Stripe invoice item created: {invoice_item.id}",
                    )
                else:
                    logger.warning("Skipping invoice item creation - quantity is 0")

            except stripe.StripeError as e:
                logger.error(f"Stripe API error for auto-recharge: {e}")
                raise HTTPException(
                    status_code=500,
                    detail=f"Stripe error: {str(e)}",
                )
            except Exception as e:
                logger.error(f"Unexpected error creating Stripe invoice item: {e}")
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to create auto-recharge invoice item: {str(e)}",
                )
        else:
            logger.warning(f"Billing account {ba.id} has no Stripe customer ID")
    else:
        logger.info(
            f"Recharge type '{new_recharge_object.type}', skipping Stripe invoice item",
        )

    # Create the recharge record
    recharge_dao.create_recharge(
        billing_account_id=ba.id,
        quantity=int(new_recharge_object.quantity),
        amount_usd=amount_usd,
        invoice_group=invoice_group,
        type_=new_recharge_object.type,
        transaction_id=new_recharge_object.transaction_id,
        status=status,
    )

    logger.info(f"Recharge record created for billing_account {ba.id}")


@router.put("/create_recharge_type")
def create_recharge_type_model(
    new_recharge_type_object: RechargeTypeModelRequest,
    session=Depends(get_db_session),
) -> None:
    """
    Creates recharge_type model in the database.

    :param new_recharge_type_object: new recharge_type model item.
    :param recharge_type_dao: DAO for recharge_type models.
    """
    recharge_type_dao = RechargeTypeDAO(session)
    recharge_type_dao.create_recharge_type(
        type=new_recharge_type_object.type,
    )


@router.put("/stripe_customer_id")
def update_stripe_customer_id(  # noqa: WPS211
    stripe_customer_id: Optional[str] = None,
    id: Optional[str] = None,  # noqa: WPS125  # backward-compat: user_id
    user_id: Optional[str] = None,
    organization_id: Optional[int] = None,
    session=Depends(get_db_session),
) -> None:
    """
    Set or clear the Stripe customer ID on a billing account.

    Accepts ``user_id`` (or legacy ``id``) or ``organization_id``.
    Pass ``stripe_customer_id`` to set it, or omit / pass empty string
    to clear it (set to NULL).

    :param stripe_customer_id: Stripe customer ID, or None/empty to clear.
    :param id: (deprecated) Alias for user_id.
    :param user_id: User ID.
    :param organization_id: Organization ID.
    """
    effective_user_id = user_id or id
    ba = _resolve_billing_account(
        session,
        user_id=effective_user_id,
        organization_id=organization_id,
    )
    ba.stripe_customer_id = stripe_customer_id if stripe_customer_id else None
    session.commit()


@router.put("/enable_autorecharge")
def update_autorecharge(  # noqa: WPS211
    enable: bool,
    id: Optional[str] = None,  # noqa: WPS125  # backward-compat: user_id
    user_id: Optional[str] = None,
    organization_id: Optional[int] = None,
    session=Depends(get_db_session),
) -> None:
    """
    Enable or disable auto-recharge on a billing account.

    Accepts ``user_id`` (or legacy ``id``) or ``organization_id``.

    When *enabling*, the account must have met the minimum spending
    threshold (fraud-prevention measure).

    :param enable: Whether to enable or disable autorecharge.
    :param id: (deprecated) Alias for user_id.
    :param user_id: User ID.
    :param organization_id: Organization ID.
    """
    effective_user_id = user_id or id
    ba = _resolve_billing_account(
        session,
        user_id=effective_user_id,
        organization_id=organization_id,
    )

    if enable:
        ba_dao = BillingAccountDAO(session)
        if ba.account_status in ("SUSPENDED", "CLOSED"):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Cannot enable auto-recharge: account is "
                    f"{ba.account_status}. Resolve outstanding invoices first."
                ),
            )
        if ba_dao.has_unpaid_auto_recharges(ba.id):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Cannot enable auto-recharge: account has unpaid "
                    "auto-recharge invoices. Wait until they are resolved."
                ),
            )
        if not ba_dao.can_enable_auto_recharge(ba.id):
            total_spending = float(ba_dao.get_total_spending(ba.id))
            min_required = float(MIN_SPEND_FOR_AUTO_RECHARGE)
            raise HTTPException(
                status_code=400,
                detail=(
                    f"User must spend at least ${min_required:.2f} before enabling "
                    f"auto-recharge. Current spending: ${total_spending:.2f}"
                ),
            )

    ba.autorecharge = enable
    session.commit()


@router.put("/autorecharge_threshold")
def update_autorecharge_threshold(  # noqa: WPS211
    threshold: float,
    id: Optional[str] = None,  # noqa: WPS125  # backward-compat: user_id
    user_id: Optional[str] = None,
    organization_id: Optional[int] = None,
    session=Depends(get_db_session),
) -> None:
    """
    Set the autorecharge threshold on a billing account.

    Accepts ``user_id`` (or legacy ``id``) or ``organization_id``.

    :param threshold: New autorecharge threshold.
    :param id: (deprecated) Alias for user_id.
    :param user_id: User ID.
    :param organization_id: Organization ID.
    """
    effective_user_id = user_id or id
    ba = _resolve_billing_account(
        session,
        user_id=effective_user_id,
        organization_id=organization_id,
    )
    ba.autorecharge_threshold = Decimal(str(threshold))
    session.commit()


@router.put("/autorecharge_qty")
def update_autorecharge_qty(  # noqa: WPS211
    qty: float,
    id: Optional[str] = None,  # noqa: WPS125  # backward-compat: user_id
    user_id: Optional[str] = None,
    organization_id: Optional[int] = None,
    session=Depends(get_db_session),
) -> None:
    """
    Set the autorecharge quantity on a billing account.

    Accepts ``user_id`` (or legacy ``id``) or ``organization_id``.

    :param qty: New autorecharge quantity.
    :param id: (deprecated) Alias for user_id.
    :param user_id: User ID.
    :param organization_id: Organization ID.
    """
    if qty < float(MIN_AUTORECHARGE_AMOUNT):
        raise HTTPException(
            status_code=400,
            detail=f"Minimum auto-recharge amount is ${MIN_AUTORECHARGE_AMOUNT}. "
            f"Provided: ${qty:.2f}",
        )

    effective_user_id = user_id or id
    ba = _resolve_billing_account(
        session,
        user_id=effective_user_id,
        organization_id=organization_id,
    )
    ba.autorecharge_qty = Decimal(str(qty))
    session.commit()


@router.put("/update_user_prompt_telemetry")
def update_user_prompt_telemetry(
    user_id: str,
    activated: bool,
    session=Depends(get_db_session),
) -> None:
    """
    Updates database evaluation model in the database.
    """
    user_dao = UserDAO(session)
    user_dao.set_prompt_telemetry(user_id, activated)


@router.get("/user_prompt_telemetry")
def get_user_prompt_telemetry(
    user_id: str,
    session=Depends(get_db_session),
) -> bool:
    """
    Returns state of the store prompts attr for a given user.
    """
    user_dao = UserDAO(session)
    return user_dao.is_telemetry_activated(user_id)


@router.post("/billing/invoice-month")
def trigger_monthly_invoicing(
    year: Optional[int] = None,
    month: Optional[int] = None,
    session=Depends(get_db_session),
) -> dict:
    """
    Trigger monthly invoicing for the specified period.
    Defaults to previous month if not specified.

    This endpoint is designed to be called by Cloud Scheduler.
    """
    try:
        # Import here to avoid circular imports
        from orchestra.routines.monthly_invoicer import invoice_month

        # Pass the session to avoid creating a new one
        invoice_month(year, month, session=session)

        period = f"{year}-{month:02d}" if year and month else "previous month"
        return {
            "status": "success",
            "message": f"Monthly invoicing completed for {period}",
            "year": year,
            "month": month,
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Monthly invoicing failed: {str(e)}",
        )


@router.post("/billing/suspend-past-due")
def trigger_billing_guard() -> dict:
    """Deprecated — billing guard removed.

    SUSPENDED is now only set by dispute/fraud events, not by a
    scheduled status escalation.  Balance-based enforcement handles
    accounts with zero/negative credits.
    """
    return {
        "status": "noop",
        "message": "Billing guard has been removed. SUSPENDED is set only by disputes.",
    }


@router.post("/billing/resource-levy")
def trigger_assistant_contact_levy(
    year: Optional[int] = None,
    month: Optional[int] = None,
    session=Depends(get_db_session),
) -> dict:
    """
    Trigger the monthly resource levy for provisioned contact details.

    Charges each billing account for its active platform-provisioned contacts
    (phone numbers, email addresses, WhatsApp senders).

    Defaults to the current month if year/month are not specified.

    This endpoint is designed to be called by Cloud Scheduler on the 1st of
    each month (``0 0 1 * *``).

    Skipped in staging environments where billing infrastructure is not
    fully configured.
    """
    if settings.is_staging:
        return {
            "status": "skipped",
            "message": "Resource levy is disabled in staging environments.",
        }

    try:
        from orchestra.routines.assistant_contact_levy import levy_provisioned_resources

        result = levy_provisioned_resources(year, month, session=session)

        period = f"{year}-{month:02d}" if year and month else result.billing_month
        return {
            "status": "success",
            "message": f"Resource levy completed for {period}",
            "billing_month": result.billing_month,
            "total_contacts_billed": result.total_contacts_billed,
            "total_amount": float(result.total_amount),
            "accounts_processed": result.accounts_processed,
            "accounts_marked_past_due": result.accounts_marked_past_due,
            "auto_recharges_triggered": result.auto_recharges_triggered,
            "notifications_sent": result.notifications_sent,
        }

    except Exception as e:
        try:
            from orchestra.routines.billing_notifications import notify_failure

            notify_failure("Contact Levy", str(e))
        except Exception:
            import logging

            logger = logging.getLogger(__name__)

            logger.warning("Failed to send failure notification", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Resource levy failed: {str(e)}",
        )


@router.post("/billing/resource-suspension")
async def trigger_assistant_contact_suspension(
    session=Depends(get_db_session),
) -> dict:
    """
    Trigger daily grace-period enforcement for provisioned contact details.

    Checks all contacts in ``grace_period`` status:
    - If the billing account has been topped up (credits ≥ 0): restores
      contacts to ``active`` and reawakens affected assistants.
    - If the grace period has lasted ≥ 14 days: deprovisions the external
      resource (Twilio number, Google Workspace seat, etc.), soft-deletes
      the contact, clears backward-compat columns, reawakens the assistant,
      and sends a deletion notification email.
    - Sends scheduled reminder/warning emails on Days 7 and 13.

    This endpoint is designed to be called by Cloud Scheduler daily at
    01:00 UTC (``0 1 * * *``).
    """
    try:
        from orchestra.routines.assistant_contact_suspension import (
            suspend_overdue_contacts,
        )

        result = await suspend_overdue_contacts(session=session)

        return {
            "status": "success",
            "message": "Resource suspension check completed",
            "total_grace_contacts_found": result.total_grace_contacts_found,
            "accounts_processed": result.accounts_processed,
            "contacts_restored": result.contacts_restored,
            "contacts_deleted": result.contacts_deleted,
            "reminders_sent": result.reminders_sent,
            "deletion_emails_sent": result.deletion_emails_sent,
        }

    except Exception as e:
        try:
            from orchestra.routines.billing_notifications import notify_failure

            notify_failure("Contact Suspension", str(e))
        except Exception:
            import logging

            logger = logging.getLogger(__name__)

            logger.warning("Failed to send failure notification", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Resource suspension failed: {str(e)}",
        )


@router.post("/assistants/inactivity-followup")
async def trigger_inactivity_followup(
    session=Depends(get_db_session),
) -> dict:
    """
    Trigger the re-engagement follow-up + auto-cleanup routine.

    Two stages:
      1. Dispatch an inactivity follow-up for assistants whose most
         recent correspondence pre-dates ``inactivity_followup_days``
         and who do not yet have a follow-up in flight. Assistants
         with a provisioned email channel wake the Unity brain via
         the communication adapter so the brain composes and sends
         from the assistant's own mailbox; assistants without an
         email instead receive an orchestra-sent first-person email
         from ``hello@unify.ai`` redirecting the boss to the Unify
         console. Orchestra records ``last_followup_sent_at`` after
         a successful send on either path.
      2. Notify the assistant's lifecycle owner, then deprovision +
         hard-delete assistants whose silent/explicit termination
         grace period has elapsed (``inactivity_auto_cleanup_days``).

    Called by Cloud Scheduler at 01:15 and 13:15 UTC
    (``15 1,13 * * *``) — twice daily, staggered 15 min after the
    billing suspension routine at 01:00 UTC.
    """
    try:
        from orchestra.routines.inactivity_followup import run_inactivity_followup

        result = await run_inactivity_followup(session=session)

        return {
            "status": "success",
            "message": "Inactivity follow-up routine completed",
            "followup_candidates_found": result.followup_candidates_found,
            "followups_dispatched": result.followups_dispatched,
            "followups_failed": result.followups_failed,
            "cleanup_candidates_found": result.cleanup_candidates_found,
            "cleanups_completed": result.cleanups_completed,
            "cleanups_failed": result.cleanups_failed,
        }

    except Exception as e:
        import logging

        logger = logging.getLogger(__name__)
        logger.exception("Inactivity follow-up routine failed")
        raise HTTPException(
            status_code=500,
            detail=f"Inactivity follow-up failed: {str(e)}",
        )


@router.post("/billing/reconcile")
def trigger_billing_reconciliation(
    auto_fix: str = "none",
    lookback_days: int = 30,
    stale_hours: int = 48,
    notify: bool = True,
    session=Depends(get_db_session),
) -> dict:
    """
    Run Stripe ↔ DB billing reconciliation.

    Compares the authoritative Stripe state with the Orchestra database
    and reports discrepancies.  Uses the configured ``STRIPE_SECRET_KEY``
    — staging instances reconcile against Stripe test mode, production
    against live mode.

    Args:
        auto_fix: Fix tier — ``"none"`` (default), ``"safe"``,
            ``"moderate"``, or ``"all"``.  See module docstring for the
            mapping of checks to tiers.
        lookback_days: How far back to scan Stripe invoices (default 30).
        stale_hours: Recharges pending longer than this are checked
            against Stripe (default 48).
        notify: If True, send a Discord notification with the results.

    Designed to be called by Cloud Scheduler / GitHub Actions daily.
    """
    try:
        from orchestra.routines.billing_reconciliation import reconcile

        result = reconcile(
            session=session,
            auto_fix=auto_fix,
            lookback_days=lookback_days,
            stale_hours=stale_hours,
        )

        if notify:
            try:
                from orchestra.routines.billing_notifications import (
                    notify_reconciliation,
                )

                notify_reconciliation(result)
            except Exception:
                import logging

                logger = logging.getLogger(__name__)

                logger.warning(
                    "Failed to send reconciliation Discord notification",
                    exc_info=True,
                )

        return {
            "status": "success",
            **result.to_dict(),
        }

    except RuntimeError as e:
        if "Stripe is not configured" in str(e):
            return {
                "status": "skipped",
                "message": "Stripe is not configured on this server.",
            }
        if notify:
            try:
                from orchestra.routines.billing_notifications import notify_failure

                notify_failure("Reconciliation", str(e))
            except Exception:
                logger.warning("Failed to send failure notification", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Billing reconciliation failed: {str(e)}",
        )
    except Exception as e:
        if notify:
            try:
                from orchestra.routines.billing_notifications import notify_failure

                notify_failure("Reconciliation", str(e))
            except Exception:
                logger.warning("Failed to send failure notification", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Billing reconciliation failed: {str(e)}",
        )


@router.post("/billing/health")
def trigger_billing_health(
    lookback_hours: int = 24,
    notify: bool = True,
    session=Depends(get_db_session),
) -> dict:
    """
    Run billing health snapshot.

    Computes aggregate billing metrics from the database — account
    status distribution, recharge activity, at-risk accounts, and
    operational health indicators.  DB-only, no Stripe API calls.

    Args:
        lookback_hours: Time window for recharge activity (default 24).
        notify: If True, send a Discord notification with the results.
    """
    try:
        from orchestra.routines.billing_health import check_health

        report = check_health(
            session=session,
            lookback_hours=lookback_hours,
        )

        if notify:
            try:
                from orchestra.routines.billing_notifications import notify_health

                notify_health(report)
            except Exception:
                logger.warning(
                    "Failed to send health Discord notification",
                    exc_info=True,
                )

        return {
            "status": "success",
            **report.to_dict(),
        }

    except Exception as e:
        if notify:
            try:
                from orchestra.routines.billing_notifications import notify_failure

                notify_failure("Health Check", str(e))
            except Exception:
                import logging

                logger = logging.getLogger(__name__)

                logger.warning("Failed to send failure notification", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Billing health check failed: {str(e)}",
        )


# ============================================================================
# Contact-type cost management
# ============================================================================


@router.get("/billing/contact-costs")
def list_contact_costs(
    session=Depends(get_db_session),
) -> list[AssistantContactCostRead]:
    """
    Return every row from the ``contact_type_costs`` table.

    The frontend uses this to display accurate monthly/one-time costs in the
    contact-creation UI instead of relying on hardcoded constants.
    """
    from orchestra.db.dao.assistant_contact_dao import AssistantContactDAO

    dao = AssistantContactDAO(session)
    return [AssistantContactCostRead.model_validate(r) for r in dao.list_all_costs()]


@router.put("/billing/contact-costs")
def upsert_contact_cost(
    body: AssistantContactCostWrite,
    session=Depends(get_db_session),
) -> AssistantContactCostRead:
    """
    Create or update a pricing row in ``contact_type_costs``.

    If a row with the same ``(contact_type, provider, country_code)`` already
    exists, its costs are updated in place.  Otherwise a new row is created.
    """
    from orchestra.db.dao.assistant_contact_dao import AssistantContactDAO

    dao = AssistantContactDAO(session)
    row = dao.upsert_cost(
        body.contact_type,
        provider=body.provider,
        country_code=body.country_code,
        monthly_cost=Decimal(str(body.monthly_cost)),
        one_time_cost=Decimal(str(body.one_time_cost)),
    )
    return AssistantContactCostRead.model_validate(row)


@router.delete("/billing/contact-costs/{cost_id}")
def delete_contact_cost(
    cost_id: int,
    session=Depends(get_db_session),
) -> dict:
    """
    Delete a single pricing row from ``contact_type_costs`` by its primary key.

    Returns 404 if the row does not exist.
    """
    from orchestra.db.dao.assistant_contact_dao import AssistantContactDAO

    dao = AssistantContactDAO(session)
    if not dao.delete_cost(cost_id):
        raise HTTPException(status_code=404, detail="Cost row not found")
    return {"status": "deleted", "id": cost_id}


# ============================================================================
# Generalized billing-account operations (freeze, status, stripe-id)
# ============================================================================


@router.post("/billing/freeze")
def freeze_billing_account(
    freeze: bool = True,
    reason: Optional[SuspensionReason] = None,
    user_id: Optional[str] = None,
    organization_id: Optional[int] = None,
    session=Depends(get_db_session),
) -> dict:
    """
    Freeze (suspend) or unfreeze (activate) a billing account.

    Accepts ``user_id`` or ``organization_id`` (exactly one).

    :param freeze: True to suspend, False to activate.
    :param reason: Suspension reason — ``admin_freeze`` or ``dispute``.
        Defaults to ``admin_freeze`` when freezing.  Cleared on unfreeze.
    :param user_id: User ID.
    :param organization_id: Organization ID.
    """
    ba = _resolve_billing_account(
        session,
        user_id=user_id,
        organization_id=organization_id,
    )
    if freeze:
        ba.account_status = "SUSPENDED"
        ba.suspension_reason = (reason or SuspensionReason.ADMIN_FREEZE).value
    else:
        ba.account_status = "ACTIVE"
        ba.suspension_reason = None
    session.commit()
    status_str = "frozen" if freeze else "unfrozen"
    result: dict = {
        "message": f"Account {status_str} successfully!",
        "billing_account_id": ba.id,
    }
    if user_id:
        result["user_id"] = user_id
    if organization_id:
        result["organization_id"] = organization_id
    return result


@router.post("/billing/freeze-by-stripe-id")
def freeze_billing_account_by_stripe_id(
    stripe_id: str,
    freeze: bool = True,
    reason: Optional[SuspensionReason] = None,
    session=Depends(get_db_session),
) -> dict:
    """
    Freeze (suspend) or unfreeze (activate) a billing account looked up by
    its Stripe customer ID.

    Works for both user and organization billing accounts.

    :param stripe_id: Stripe customer ID.
    :param freeze: True to suspend, False to activate.
    :param reason: Suspension reason — ``admin_freeze`` or ``dispute``.
        Defaults to ``admin_freeze`` when freezing.  Cleared on unfreeze.
    """
    ba_dao = BillingAccountDAO(session)
    ba = ba_dao.get_by_stripe_customer_id(stripe_id)
    if not ba:
        raise HTTPException(
            status_code=404,
            detail=f"Billing account with Stripe ID {stripe_id} not found.",
        )
    new_status = "SUSPENDED" if freeze else "ACTIVE"
    ba_dao.set_account_status(ba.id, new_status)
    ba.suspension_reason = (
        (reason or SuspensionReason.ADMIN_FREEZE).value if freeze else None
    )
    status_str = "frozen" if freeze else "unfrozen"
    return {
        "message": f"Account with stripe_id {stripe_id} {status_str} successfully!",
        "billing_account_id": ba.id,
    }


@router.get("/billing/is-frozen")
def is_billing_account_frozen(
    user_id: Optional[str] = None,
    organization_id: Optional[int] = None,
    session=Depends(get_db_session),
) -> dict:
    """
    Check whether a billing account is frozen (SUSPENDED or CLOSED).

    Accepts ``user_id`` or ``organization_id`` (exactly one).

    :param user_id: User ID.
    :param organization_id: Organization ID.
    :return: ``{"is_frozen": bool, "billing_account_id": int, ...}``
    """
    ba = _resolve_billing_account(
        session,
        user_id=user_id,
        organization_id=organization_id,
    )
    frozen = ba.account_status in ("SUSPENDED", "CLOSED")
    result: dict = {
        "billing_account_id": ba.id,
        "is_frozen": frozen,
        "account_status": ba.account_status,
    }
    if user_id:
        result["user_id"] = user_id
    if organization_id:
        result["organization_id"] = organization_id
    return result


@router.get("/billing/account-info")
def get_billing_account_info(
    user_id: Optional[str] = None,
    organization_id: Optional[int] = None,
    session=Depends(get_db_session),
) -> dict:
    """
    Get billing account info (stripe customer ID, credits, auto-recharge
    settings) for a user or organization.

    Accepts ``user_id`` or ``organization_id`` (exactly one).

    :param user_id: User ID (for personal billing accounts).
    :param organization_id: Organization ID (for org billing accounts).
    :return: Billing account details.
    """
    ba = _resolve_billing_account(
        session,
        user_id=user_id,
        organization_id=organization_id,
    )
    result: dict = {
        "billing_account_id": ba.id,
        "stripe_customer_id": ba.stripe_customer_id,
        "credits": float(ba.credits) if ba.credits else 0,
        "autorecharge": ba.autorecharge,
        "autorecharge_threshold": (
            float(ba.autorecharge_threshold) if ba.autorecharge_threshold else 0
        ),
        "autorecharge_qty": float(ba.autorecharge_qty) if ba.autorecharge_qty else 0,
        "account_status": ba.account_status,
    }
    if user_id:
        result["user_id"] = user_id
    if organization_id:
        result["organization_id"] = organization_id
    return result


@router.put("/billing/stripe-id")
def set_stripe_id(
    stripe_id: str,
    user_id: Optional[str] = None,
    organization_id: Optional[int] = None,
    session=Depends(get_db_session),
) -> dict:
    """
    Set (or create) the Stripe customer ID on a billing account.

    Accepts ``user_id`` or ``organization_id`` (exactly one).
    If the entity has no billing account yet, one is created.

    :param stripe_id: Stripe customer ID to set.
    :param user_id: User ID.
    :param organization_id: Organization ID.
    """
    ba_dao = BillingAccountDAO(session)

    if user_id and organization_id:
        raise HTTPException(
            status_code=400,
            detail="Provide either user_id or organization_id, not both.",
        )
    if not user_id and not organization_id:
        raise HTTPException(
            status_code=400,
            detail="Provide either user_id or organization_id.",
        )

    if user_id:
        user_dao = UserDAO(session)
        user_row = user_dao.get_by_id(user_id)
        if not user_row:
            raise HTTPException(status_code=404, detail=f"User {user_id} not found.")
        user_instance = user_row[0]
        ba = user_instance.billing_account
        if ba is None:
            ba = ba_dao.create(stripe_customer_id=stripe_id)
            user_instance.billing_account_id = ba.id
        else:
            ba.stripe_customer_id = stripe_id
    else:
        org = session.query(Organization).filter_by(id=organization_id).first()
        if org is None:
            raise HTTPException(
                status_code=404,
                detail=f"Organization {organization_id} not found.",
            )
        ba = org.billing_account
        if ba is None:
            ba = ba_dao.create(stripe_customer_id=stripe_id)
            org.billing_account_id = ba.id
        else:
            ba.stripe_customer_id = stripe_id

    session.commit()
    entity_label = f"user {user_id}" if user_id else f"organization {organization_id}"
    return {"message": f"Stripe ID set for {entity_label}", "billing_account_id": ba.id}


VALID_TIERS = {"developer", "professional", "enterprise"}


@router.put("/billing/tier")
def set_billing_account_tier(
    tier: str,
    user_id: Optional[str] = None,
    organization_id: Optional[int] = None,
    session=Depends(get_db_session),
) -> dict:
    """
    Set the tier on a billing account.

    Accepts ``user_id`` or ``organization_id`` (exactly one).

    :param tier: One of ``developer``, ``professional``, ``enterprise``.
    :param user_id: User ID (for personal billing accounts).
    :param organization_id: Organization ID (for org billing accounts).
    """
    if tier not in VALID_TIERS:
        raise HTTPException(
            status_code=400,
            detail=f"Tier must be one of {', '.join(sorted(VALID_TIERS))}.",
        )

    ba_dao = BillingAccountDAO(session)

    # Resolve or create billing account
    if user_id and organization_id:
        raise HTTPException(
            status_code=400,
            detail="Provide either user_id or organization_id, not both.",
        )
    if not user_id and not organization_id:
        raise HTTPException(
            status_code=400,
            detail="Provide either user_id or organization_id.",
        )

    if user_id:
        user_dao = UserDAO(session)
        user_rows = user_dao.filter(id=user_id)
        if not user_rows:
            raise HTTPException(status_code=404, detail=f"User {user_id} not found.")
        user_instance = user_rows[0][0]
        ba = user_instance.billing_account
        if ba is None:
            ba = ba_dao.create(tier=tier)
            user_instance.billing_account_id = ba.id
        else:
            ba.tier = tier
    else:
        org = session.query(Organization).filter_by(id=organization_id).first()
        if org is None:
            raise HTTPException(
                status_code=404,
                detail=f"Organization {organization_id} not found.",
            )
        ba = org.billing_account
        if ba is None:
            ba = ba_dao.create(tier=tier)
            org.billing_account_id = ba.id
        else:
            ba.tier = tier

    session.commit()
    entity_label = f"user {user_id}" if user_id else f"organization {organization_id}"
    return {
        "message": f"Tier set to '{tier}' for {entity_label}",
        "billing_account_id": ba.id,
    }


@router.get("/billing_eligibility")
@router.get("/user_billing_eligibility")  # backward-compat alias
def get_billing_eligibility(
    user_id: Optional[str] = None,
    organization_id: Optional[int] = None,
    session=Depends(get_db_session),
) -> dict:
    """
    Get auto-recharge eligibility for a billing account.

    Accepts either ``user_id`` or ``organization_id`` (exactly one).

    Checks if the account has spent at least the minimum threshold in
    real-money transactions before it can enable automatic top-ups.
    This is a fraud-prevention measure to stop bot accounts from setting
    up very low, repeated automatic refills and then disputing the charges.

    :param user_id: User ID (for personal billing).
    :param organization_id: Organization ID (for org billing).
    :param session: Database session.
    :return: Dictionary with eligibility information.
    """
    ba = _resolve_billing_account(
        session,
        user_id=user_id,
        organization_id=organization_id,
    )
    ba_dao = BillingAccountDAO(session)

    total_spending = float(ba_dao.get_total_spending(ba.id))
    can_enable = ba_dao.can_enable_auto_recharge(ba.id)
    min_required = float(MIN_SPEND_FOR_AUTO_RECHARGE)

    result: dict = {
        "billing_account_id": ba.id,
        "total_spending": total_spending,
        "can_enable_auto_recharge": can_enable,
        "minimum_spend_required": min_required,
        "remaining_spend_needed": max(0.0, min_required - total_spending),
    }
    # Include the caller's key for backward compatibility
    if user_id:
        result["user_id"] = user_id
    if organization_id:
        result["organization_id"] = organization_id
    return result


@router.post("/billing/migrate-accounts")
@router.post("/billing/migrate-users")  # backward-compat alias
def migrate_billing_accounts_to_compliance(
    session=Depends(get_db_session),
) -> dict:
    """
    Migrate **all** billing accounts (users + organizations) to comply with
    auto-recharge requirements.

    This endpoint will:
    1. Disable auto-recharge for accounts that haven't met the minimum spend threshold.
    2. Set auto-recharge amount to $25 for accounts with amounts below $25.

    :param session: Database session.
    :return: Dictionary with migration results.
    """
    ba_dao = BillingAccountDAO(session)

    # Fetch every billing account in the system
    all_accounts: List[BillingAccount] = session.query(BillingAccount).all()

    results: dict = {
        "total_accounts_processed": 0,
        "accounts_disabled": [],
        "accounts_amount_updated": [],
        "accounts_unaffected": [],
        "errors": [],
    }

    min_required = float(MIN_SPEND_FOR_AUTO_RECHARGE)

    for ba in all_accounts:
        try:
            results["total_accounts_processed"] += 1

            total_spending = float(ba_dao.get_total_spending(ba.id))
            can_enable = ba_dao.can_enable_auto_recharge(ba.id)

            original_autorecharge = ba.autorecharge
            original_autorecharge_qty = ba.autorecharge_qty

            changes_made = False

            # Disable auto-recharge for accounts that haven't met the spend threshold
            if original_autorecharge and not can_enable:
                ba.autorecharge = False
                results["accounts_disabled"].append(
                    {
                        "billing_account_id": ba.id,
                        "spending": total_spending,
                        "reason": f"Insufficient spending (${total_spending:.2f} < ${min_required:.2f})",
                    },
                )
                changes_made = True

            # Enforce minimum auto-recharge amount of $25
            if original_autorecharge_qty is None or float(
                original_autorecharge_qty,
            ) < float(MIN_AUTORECHARGE_AMOUNT):
                ba.autorecharge_qty = MIN_AUTORECHARGE_AMOUNT
                old_amt = (
                    float(original_autorecharge_qty)
                    if original_autorecharge_qty is not None
                    else None
                )
                results["accounts_amount_updated"].append(
                    {
                        "billing_account_id": ba.id,
                        "old_amount": old_amt,
                        "new_amount": float(MIN_AUTORECHARGE_AMOUNT),
                        "reason": (
                            f"Amount below minimum (${old_amt:.2f} < ${MIN_AUTORECHARGE_AMOUNT})"
                            if old_amt is not None
                            else f"Amount was None, set to minimum ${MIN_AUTORECHARGE_AMOUNT}"
                        ),
                        "autorecharge_enabled": original_autorecharge,
                    },
                )
                changes_made = True

            if not changes_made:
                results["accounts_unaffected"].append(
                    {
                        "billing_account_id": ba.id,
                        "autorecharge_enabled": original_autorecharge,
                        "autorecharge_amount": (
                            float(original_autorecharge_qty)
                            if original_autorecharge_qty is not None
                            else None
                        ),
                        "spending": total_spending,
                        "auto_recharge_eligible": can_enable,
                    },
                )

        except Exception as e:
            results["errors"].append(
                {
                    "billing_account_id": ba.id if hasattr(ba, "id") else "unknown",
                    "error": str(e),
                },
            )
            continue

    # Commit all changes
    try:
        session.commit()
        total = results["total_accounts_processed"]
        results["status"] = "success"
        results["message"] = (
            f"Migration completed successfully. Processed {total} billing account(s)."
        )
    except Exception as e:
        session.rollback()
        results["status"] = "error"
        results["message"] = f"Migration failed during commit: {str(e)}"
        raise HTTPException(status_code=500, detail=f"Migration failed: {str(e)}")

    return results


@router.post(
    "/cleanup/email-verifications",
    summary="Admin: Cleanup expired email verification codes",
    description="Delete all expired email verification records (signup and password reset codes). "
    "Called by scheduled cleanup job.",
)
def admin_cleanup_email_verifications(
    session=Depends(get_db_session),
) -> dict:
    """
    Clean up expired email verification codes.

    This endpoint is designed to be called by a scheduled job (e.g., GitHub Actions cron).
    It deletes all email_verification rows where expires_at is in the past.

    :param session: Database session.
    :return: Count of deleted records and timestamp.
    """
    try:
        from orchestra.routines.email_verification_cleanup import (
            cleanup_expired_verifications,
        )

        deleted_count = cleanup_expired_verifications(session)
        return {
            "deleted_count": deleted_count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": f"Successfully deleted {deleted_count} expired verification(s)",
        }
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to cleanup email verifications: {str(e)}",
        )


@router.post(
    "/cleanup/expired-invites",
    summary="Admin: Cleanup expired organization invites",
    description="Delete all expired pending organization invites. "
    "Called by scheduled cleanup job.",
)
def admin_cleanup_expired_invites(
    session=Depends(get_db_session),
) -> dict:
    """
    Clean up expired organization invites.

    This endpoint is designed to be called by a scheduled job (e.g., GitHub Actions cron).
    It deletes all organization invites where expires_at is in the past.

    :param session: Database session.
    :return: Count of deleted invites and timestamp.
    """
    invite_dao = OrganizationInviteDAO(session)

    try:
        deleted_count = invite_dao.cleanup_expired_invites()
        session.commit()

        return {
            "deleted_count": deleted_count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": f"Successfully deleted {deleted_count} expired invite(s)",
        }
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to cleanup expired invites: {str(e)}",
        )


@router.post(
    "/cleanup/expired-space-invites",
    summary="Admin: Cleanup expired space invitations",
    description="Transition all expired pending space invitations. "
    "Called by scheduled cleanup job.",
)
def admin_cleanup_expired_space_invites(
    session=Depends(get_db_session),
) -> dict:
    """Transition expired space invitations to their terminal status.

    This endpoint is designed to be called by a scheduled job. It marks
    pending invitations whose expiry timestamp is in the past as expired.

    :param session: Database session.
    :return: Count of transitioned invitations and timestamp.
    """
    invite_dao = SpaceInviteDAO(session)

    try:
        transitioned_count = invite_dao.expire_pending_invites()
        session.commit()

        return {
            "transitioned_count": transitioned_count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": (
                f"Transitioned {transitioned_count} pending space-invite(s) "
                "to expired"
            ),
        }
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to cleanup expired space invites: {str(e)}",
        )


@router.get(
    "/cleanup/assistant-runtime",
    summary="Admin: Inspect durable assistant cleanup tasks",
    description="List queued assistant cleanup tasks, optionally filtered by assistant_id.",
)
def admin_list_assistant_cleanup_tasks(
    assistant_id: int | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    session: Session = Depends(get_db_session),
) -> dict:
    """Return cleanup task rows for observability and integration testing."""
    query = session.query(AssistantCleanupTask)
    if assistant_id is not None:
        query = query.filter(AssistantCleanupTask.assistant_id == assistant_id)
    if status is not None:
        query = query.filter(AssistantCleanupTask.status == status)
    tasks = query.order_by(AssistantCleanupTask.created_at.desc()).limit(limit).all()
    return {
        "tasks": [
            {
                "id": t.id,
                "assistant_id": t.assistant_id,
                "status": t.status,
                "source_flow": t.source_flow,
                "attempt_count": t.attempt_count,
                "last_error": t.last_error,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "completed_at": t.completed_at.isoformat() if t.completed_at else None,
                "next_retry_at": (
                    t.next_retry_at.isoformat() if t.next_retry_at else None
                ),
            }
            for t in tasks
        ],
        "count": len(tasks),
    }


@router.post(
    "/cleanup/assistant-runtime",
    summary="Admin: Process durable assistant cleanup tasks",
    description="Run the Orchestra-owned retry queue for assistant runtime cleanup.",
)
async def admin_process_assistant_cleanup_tasks(
    limit: int = Query(
        DEFAULT_CLEANUP_TASK_BATCH_SIZE,
        ge=1,
        le=MAX_CLEANUP_TASK_BATCH_SIZE,
    ),
    session: Session = Depends(get_db_session),
) -> dict:
    """Drain the assistant cleanup retry queue for a bounded batch of tasks."""
    try:
        result = await process_assistant_cleanup_tasks(session, limit=limit)
        return {
            **result,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to process assistant cleanup tasks: {str(e)}",
        )


# ============================================================================
# Spending Limit Notification Endpoints
# ============================================================================


@router.post(
    "/cleanup/spending-limit-notifications",
    summary="Cleanup old spending limit notifications",
    description="Delete spending limit notifications older than 6 months. "
    "Called by scheduled cleanup job.",
)
def admin_cleanup_spending_limit_notifications(
    months_to_keep: int = 6,
    session=Depends(get_db_session),
) -> dict:
    """
    Clean up old spending limit notifications.

    This endpoint is designed to be called by a scheduled job (e.g., GitHub Actions cron).
    It deletes all spending limit notification records where the month is older than
    the specified retention period.

    :param months_to_keep: Number of months of notifications to retain (default: 6).
    :param session: Database session.
    :return: Count of deleted notifications and timestamp.
    """
    from orchestra.db.dao.spending_limit_notification_dao import (
        SpendingLimitNotificationDAO,
    )

    notification_dao = SpendingLimitNotificationDAO(session)

    try:
        deleted_count = notification_dao.cleanup_old_notifications(months_to_keep)
        session.commit()

        return {
            "deleted_count": deleted_count,
            "months_retained": months_to_keep,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": f"Successfully deleted {deleted_count} old notification(s)",
        }
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to cleanup spending limit notifications: {str(e)}",
        )


@router.get(
    "/spending-limit-notifications",
    summary="Get recent spending limit notifications",
    description="Query the spending_limit_notifications table for debugging. "
    "Returns recent notifications with optional filters.",
)
def admin_get_spending_limit_notifications(
    entity_type: Optional[str] = Query(None, description="Filter by entity type"),
    entity_id: Optional[str] = Query(None, description="Filter by entity ID"),
    month: Optional[str] = Query(None, description="Filter by month (YYYY-MM)"),
    limit: int = Query(50, ge=1, le=500),
    session=Depends(get_db_session),
) -> dict:
    """
    Get recent spending limit notifications for debugging.

    :param entity_type: Optional filter by entity type (assistant, user, member, organization)
    :param entity_id: Optional filter by entity ID
    :param month: Optional filter by month in YYYY-MM format
    :param limit: Maximum number of results (default: 50, max: 500)
    :param session: Database session
    :return: List of notification records
    """
    from orchestra.db.dao.spending_limit_notification_dao import (
        SpendingLimitNotificationDAO,
    )

    notification_dao = SpendingLimitNotificationDAO(session)

    try:
        notifications = notification_dao.get_recent_notifications(
            entity_type=entity_type,
            entity_id=entity_id,
            month=month,
            limit=limit,
        )

        return {
            "count": len(notifications),
            "notifications": [
                {
                    "id": n.id,
                    "entity_type": n.entity_type,
                    "entity_id": n.entity_id,
                    "entity_name": n.entity_name,
                    "month": n.month,
                    "limit_value": float(n.limit_value) if n.limit_value else None,
                    "current_spend": (
                        float(n.current_spend) if n.current_spend else None
                    ),
                    "notified_user_ids": n.notified_user_ids,
                    "notified_at": n.notified_at.isoformat() if n.notified_at else None,
                    "limit_set_at": (
                        n.limit_set_at.isoformat() if n.limit_set_at else None
                    ),
                }
                for n in notifications
            ],
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get spending limit notifications: {str(e)}",
        )


# =============================================================================
# Rate Limit Administration
# =============================================================================


@router.post(
    "/rate-limits/cleanup",
    summary="Clean up old rate limit records",
    description="Remove rate limit records older than 48 hours. Returns count of deleted records.",
    tags=["Rate Limiting"],
)
def admin_rate_limit_cleanup(
    session: Session = Depends(get_db_session),
) -> dict:
    """
    Manually trigger cleanup of old rate limit records.

    This removes records older than 48 hours (the cleanup threshold).
    This should normally run automatically via scheduled job.
    """
    from orchestra.routines.rate_limit_cleanup import cleanup_rate_limit_records

    deleted_count = cleanup_rate_limit_records(session)
    return {
        "status": "success",
        "deleted_count": deleted_count,
    }


@router.get(
    "/rate-limits/stats",
    summary="Get rate limit statistics",
    description="Get statistics about rate limit records for monitoring.",
    tags=["Rate Limiting"],
)
def admin_rate_limit_stats(
    session: Session = Depends(get_db_session),
) -> dict:
    """
    Get statistics about rate limit records.

    Returns:
    - total_records: Total number of records in the table
    - active_records_24h: Records in the active rate limiting window
    - cleanup_eligible_48h: Records that will be deleted on next cleanup
    - unique_users_24h: Number of unique users with requests in last 24h
    - records_by_category: Breakdown by rate limit category
    """
    from orchestra.routines.rate_limit_cleanup import get_rate_limit_stats

    return get_rate_limit_stats(session)


@router.get(
    "/rate-limits/user/{user_id}",
    summary="Get rate limit usage for a user",
    description="Get the current rate limit usage for a specific user.",
    tags=["Rate Limiting"],
)
def admin_rate_limit_user_usage(
    user_id: str,
    session: Session = Depends(get_db_session),
) -> dict:
    """
    Get rate limit usage summary for a specific user.

    Shows current usage counts for all categories in the 24-hour window.
    """
    from orchestra.db.dao.rate_limit_counter_dao import RateLimitCounterDAO

    dao = RateLimitCounterDAO(session)
    usage = dao.get_usage_summary(user_id=user_id)

    return {
        "user_id": user_id,
        "usage_24h": usage,
    }


# =============================================================================
# Temp File Cleanup
# =============================================================================


@router.post(
    "/temp-files/cleanup",
    summary="Clean up old temporary files",
    description=(
        "Remove temporary files (animation inputs, etc.) older than the "
        "specified age from the assistant media GCS bucket."
    ),
    tags=["Storage"],
)
def admin_temp_file_cleanup(
    max_age_hours: int = 2,
) -> dict:
    """
    Manually trigger cleanup of old temporary files in the ``tmp/``
    folder of the assistant media bucket.

    Args:
        max_age_hours: Delete files older than this many hours.
            Defaults to 2.
    """
    from orchestra.routines.temp_file_cleanup import cleanup_temp_files

    deleted_count = cleanup_temp_files(max_age_hours=max_age_hours)
    return {
        "status": "success",
        "deleted_count": deleted_count,
    }
