import datetime as _dt
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional

import stripe
from fastapi import APIRouter, HTTPException, Query
from fastapi.param_functions import Depends
from sqlalchemy.orm import Session

from orchestra.db.dao.billing_account_dao import BillingAccountDAO
from orchestra.db.dao.organization_dao import OrganizationDAO
from orchestra.db.dao.organization_invite_dao import OrganizationInviteDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.recharge_dao import RechargeDAO
from orchestra.db.dao.recharge_type_dao import RechargeTypeDAO
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
from orchestra.lib.billing import grant_promo_credits
from orchestra.lib.time import month_end_utc
from orchestra.services.assistant_cleanup_service import (
    DEFAULT_CLEANUP_TASK_BATCH_SIZE,
    MAX_CLEANUP_TASK_BATCH_SIZE,
    process_assistant_cleanup_tasks,
)
from orchestra.settings import settings
from orchestra.web.api.admin.schema import (  # noqa: WPS235
    AdminInvoiceListItem,
    AdminInvoiceListResponse,
    AssignPlanGroupRequest,
    AssignPlanGroupResponse,
    AssistantContactCostRead,
    AssistantContactCostWrite,
    BillingPlanAssignmentResponse,
    BillingPlanTemplateCreate,
    BillingPlanTemplateResponse,
    BillingProfileResponse,
    BillingProfileUpdateRequest,
    EnsureStripeCustomerRequest,
    EnsureStripeCustomerResponse,
    OrganizationListItem,
    OrganizationListResponse,
    PaymentPreferencesRequest,
    PaymentPreferencesResponse,
    PlanGroupAddMemberRequest,
    PlanGroupCreateRequest,
    PlanGroupListResponse,
    PlanGroupMemberItem,
    PlanGroupResponse,
    PlanGroupSetPositionsRequest,
    PlanGroupSummaryItem,
    PlanGroupUpdateRequest,
    PlanHistoryResponse,
    RechargeModelRequest,
    RechargeModelResponse,
    RechargeTypeModelRequest,
    RechargeTypeModelResponse,
    SetPlanRequest,
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

    # Calculate amount_usd and invoice_group
    amount_usd = new_recharge_object.quantity

    if new_recharge_object.target_month:
        try:
            year, month = map(int, new_recharge_object.target_month.split("-"))
            invoice_group = month_end_utc(_dt.date(year, month, 1))
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Invalid target_month format. Use 'YYYY-MM' (e.g., '2025-06')",
            )
    else:
        # Use month_end_utc to get the last day of the current month.
        # The previous in-line formula (replace(day=1) + 32d → replace(day=1)
        # → subtract 1us → .date()) did NOT zero out ``at``'s time-of-day
        # component, so for any recharge created at non-midnight UTC the
        # final subtraction stayed on the 1st of the next month instead of
        # crossing back into the previous day. That produced 78+ recharges
        # in production with first-of-next-month invoice_group values —
        # cosmetic on PAID promo/payment rows, but a real invoicing skip
        # for the one PENDING_INVOICE auto-recharge that ever went through
        # this endpoint (Recharge 20934 / Nassim, reconciled 2026-05-13).
        invoice_group = month_end_utc(at)

    # Promo recharges are a pure no-charge credit grant: delegate to the
    # shared helper (also used by the self-serve manual top-up endpoint).
    if new_recharge_object.type == "promo":
        grant_promo_credits(
            session,
            ba,
            float(new_recharge_object.quantity),
            user_id=new_recharge_object.user_id,
            organization_id=new_recharge_object.organization_id,
            description="Admin recharge (promo)",
            detail={"event": "admin_recharge", "type": "promo"},
            invoice_group=invoice_group,
        )
        logger.info(f"Recharge record created for billing_account {ba.id}")
        return

    # Credit the billing account (payment / auto types).
    ba_dao = BillingAccountDAO(session)
    ba_dao.add_credits(
        ba.id,
        float(new_recharge_object.quantity),
        category="recharge",
        user_id=new_recharge_object.user_id,
        organization_id=new_recharge_object.organization_id,
        description=f"Admin recharge ({new_recharge_object.type})",
        detail={"event": "admin_recharge", "type": new_recharge_object.type},
    )

    # Set status based on recharge type
    if new_recharge_object.type == "payment":
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


@router.post(
    "/billing/invoice-metered-month",
    summary="Admin: Run the metered-mode invoicer for a period",
    description=(
        "Run the monthly metered-mode invoicer routine for the given "
        "period. Defaults to the previous month. Production runs on "
        "Cloud Scheduler "
        "(``orchestra-production-monthly-metered-invoicer``, "
        "``5 2 1 * *`` UTC); "
        "staging is on-demand only — invoke ``invoice_metered_month`` "
        "from a Python shell to verify changes. "
        "\n\n"
        "Idempotent: a unique key on ``(billing_account_id, "
        "invoice_group)`` plus Stripe idempotency keys at the line- "
        "and invoice-create call sites mean re-running for the same "
        "period is a no-op for already-invoiced accounts. "
        "\n\n"
        "Soft guard: rejects with 400 if ``year``/``month`` resolves "
        "to the current or any future month — invoicing an in-progress "
        "period is always a misconfiguration (the routine bills closed "
        "periods only). Past-month replays remain allowed for "
        "operator-initiated catch-up runs. See "
        "``monthly_metered_invoicer`` module docstring for the full "
        "scheduling rationale."
    ),
)
def trigger_monthly_metered_invoicing(
    year: Optional[int] = None,
    month: Optional[int] = None,
    session=Depends(get_db_session),
) -> dict:
    """Trigger the metered-mode invoicing routine for a period."""
    try:
        from orchestra.routines.monthly_metered_invoicer import invoice_metered_month

        # Resolve the requested period for the soft current/future-month
        # guard. Mirrors the routine's own default-resolution logic
        # (previous month if year/month are None) so the guard message
        # accurately reflects what would actually be invoiced.
        today_utc = datetime.now(timezone.utc).date()
        if year is None or month is None:
            first_this_month = today_utc.replace(day=1)
            last_month_end = first_this_month - timedelta(days=1)
            resolved_year, resolved_month = (
                last_month_end.year,
                last_month_end.month,
            )
        else:
            resolved_year, resolved_month = year, month

        first_of_current_month = today_utc.replace(day=1)
        period_start = _dt.date(resolved_year, resolved_month, 1)
        if period_start >= first_of_current_month:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Refusing to invoice {resolved_year}-{resolved_month:02d}: "
                    "period is current or future. The metered invoicer only "
                    "bills closed periods."
                ),
            )

        result = invoice_metered_month(resolved_year, resolved_month, session=session)
        return {
            "status": "success",
            "period": result.period,
            "accounts_invoiced": result.accounts_invoiced,
            "accounts_skipped": result.accounts_skipped,
            "accounts_failed": result.accounts_failed,
            "errors": result.errors,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Monthly metered invoicing failed: {str(e)}",
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

    Skipped when billing is disabled for the current environment.
    """
    if not settings.charges_billing:
        return {
            "status": "skipped",
            "message": "Resource levy is disabled for this environment.",
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
    Trigger the per-user re-engagement nudge routine.

    Finds users who have not interacted with any of their assistants
    (the Coordinator included) for ``inactivity_followup_days`` and whose
    personal Coordinator has not already followed up since their last
    activity (and has not opted out), then emails each owner a soft
    check-in from the shared Coordinator mailbox (``twin@``), recording
    ``last_followup_sent_at`` on the Coordinator after a successful send.

    This routine never deletes or deprovisions assistants — contact
    lifecycle/cost is governed solely by the billing suspension routine.

    Called by the ``inactivity-followup`` GitHub Actions workflow at
    01:15 and 13:15 UTC (``15 1,13 * * *``) — twice daily, staggered
    15 min after the billing suspension routine at 01:00 UTC.
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
            "followups_skipped": result.followups_skipped,
        }

    except Exception as e:
        import logging

        logger = logging.getLogger(__name__)
        logger.exception("Inactivity follow-up routine failed")
        raise HTTPException(
            status_code=500,
            detail=f"Inactivity follow-up failed: {str(e)}",
        )


@router.post("/assistants/founder-interview-ask")
async def trigger_founder_interview_ask(
    session=Depends(get_db_session),
) -> dict:
    """
    Trigger the one-shot founder interview-ask routine.

    Emails eligible personal Coordinator owners from ``dan@`` with a
    Cal.com booking link (engaged-then-quiet, never-engaged, and
    still-active cohorts). Stamps ``founder_interview_asked_at`` only
    after a successful send.

    Called by the ``founder-interview-ask`` GitHub Actions workflow
    daily at 14:30 UTC.
    """
    try:
        from orchestra.routines.founder_interview_ask import run_founder_interview_ask

        result = await run_founder_interview_ask(session=session)

        return {
            "status": "success",
            "message": "Founder interview-ask routine completed",
            "interview_candidates_found": result.interview_candidates_found,
            "interviews_dispatched": result.interviews_dispatched,
            "interviews_failed": result.interviews_failed,
            "interviews_skipped": result.interviews_skipped,
        }

    except Exception as e:
        import logging

        logger = logging.getLogger(__name__)
        logger.exception("Founder interview-ask routine failed")
        raise HTTPException(
            status_code=500,
            detail=f"Founder interview-ask failed: {str(e)}",
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


@router.post(
    "/billing/sweep-expired-grants",
    summary="Admin: Forfeit unconsumed remainders of expired credit grants",
    description=(
        "Run the credit-grant expiry sweep. For every CREDITS account "
        "holding an expired grant (trial signup credits past their "
        "1-week window, or a plan grant whose cycle webhook was "
        "missed) with an unconsumed remainder, posts a single negative "
        "``forfeit`` ledger row and decrements the wallet — never below "
        "zero, never touching non-expiring credits. Idempotent: "
        "re-running is a no-op for already-forfeited grants. Production "
        "runs daily on Cloud Scheduler "
        "(``orchestra-production-credit-grant-expiry-sweep``, "
        "``30 1 * * *`` UTC); staging is on-demand only. See "
        "``credit_grant_expiry_sweep`` module docstring for the full "
        "scheduling rationale."
    ),
)
def trigger_credit_grant_expiry_sweep(
    session=Depends(get_db_session),
) -> dict:
    """Trigger the credit-grant expiry sweep."""
    try:
        from orchestra.routines.credit_grant_expiry_sweep import sweep_expired_grants

        result = sweep_expired_grants(session=session)
        return {
            "status": "success",
            **result.to_dict(),
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Credit-grant expiry sweep failed: {str(e)}",
        )


@router.post(
    "/billing/send-expiry-reminders",
    summary="Admin: Email holders before their credit grants expire",
    description=(
        "Run the pre-expiry credit reminder. For every CREDITS account "
        "holding an unconsumed grant whose expiry falls inside the "
        "reminder window (``CREDIT_EXPIRY_REMINDER_DAYS``, default 3), "
        "emails the holder a single 'use-it-or-lose-it' (plan) / "
        "'subscribe before you lose these' (trial) notice. Idempotent: "
        "each distinct expiry is reminded at most once "
        "(``billing_account.credit_expiry_reminded_at``). Production runs "
        "daily on Cloud Scheduler "
        "(``orchestra-production-credit-expiry-reminder``, ``0 9 * * *`` "
        "UTC); staging is on-demand only. See "
        "``credit_expiry_reminder`` module docstring for the full "
        "scheduling rationale."
    ),
)
def trigger_credit_expiry_reminder(
    session=Depends(get_db_session),
) -> dict:
    """Trigger the pre-expiry credit reminder run."""
    try:
        from orchestra.routines.credit_expiry_reminder import (
            send_credit_expiry_reminders,
        )

        result = send_credit_expiry_reminders(session=session)
        return {
            "status": "success",
            **result.to_dict(),
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Credit-expiry reminder run failed: {str(e)}",
        )


@router.get(
    "/billing/comms-gate",
    summary="Admin: Billing-gate state for an assistant's inbound comms",
    description=(
        "Whether the assistant's owning account is billing-gated "
        "(suspended, card required, or out of credits) and, when gated, "
        "the owner-facing explanation to auto-reply over the inbound "
        "channel. Called by the unify-deploy adapters before dispatching "
        "an inbound message to the runtime."
    ),
)
def get_comms_gate(
    assistant_id: int,
    session=Depends(get_db_session),
) -> dict:
    from orchestra.db.dao.assistant_dao import AssistantDAO
    from orchestra.lib.trial_subscription import comms_gate_for_assistant

    assistant = AssistantDAO(session).get_assistant_by_agent_id(assistant_id)
    if assistant is None:
        raise HTTPException(status_code=404, detail="Assistant not found.")
    return comms_gate_for_assistant(session, assistant)


@router.post(
    "/billing/card-gate-freeze-sweep",
    summary="Admin: Freeze ACTIVE never-paid accounts pending card",
    description=(
        "One-shot migration sweep for the card-gated trial rollout: "
        "suspend (reason ``card_required``) every ACTIVE account with "
        "neither a linked subscription nor real payment history. "
        "Refuses unless ``REQUIRE_CARD_ON_FILE`` is enabled. Completing "
        "the trial Checkout auto-reinstates an account. Defaults to "
        "``dry_run=true`` — pass ``dry_run=false`` to apply."
    ),
)
def trigger_card_gate_freeze_sweep(
    dry_run: bool = True,
    session=Depends(get_db_session),
) -> dict:
    from orchestra.routines.card_gate_sweep import freeze_never_paid_accounts

    result = freeze_never_paid_accounts(session, dry_run=dry_run)
    if not dry_run:
        session.commit()
    return {"status": "success", **result.to_dict()}


@router.post(
    "/billing/burner-cluster-report",
    summary="Admin: Report never-paid accounts farming in an origin cluster",
    description=(
        "The credit-farming signal for when free credits are Console-only. "
        "Names accounts in a *cluster* — several never-paid accounts "
        "sharing a signup origin inside a short window, each having "
        "drained its grant — and does nothing else. Single accounts are "
        "never named, however fast they burn. "
        "Read-only by design: the evidence is circumstantial however many "
        "conditions are stacked, so acting on it is a person's decision, "
        "taken through ``POST /admin/billing/freeze`` with reason "
        "``abuse_fingerprint``. "
        "Accounts with no ``signup_ip`` are skipped rather than grouped "
        "under a shared NULL, and when every candidate is unreadable the "
        "response says so — a blind run and a clear one otherwise look "
        "identical."
    ),
)
def trigger_burner_cluster_report(
    session=Depends(get_db_session),
) -> dict:
    from orchestra.routines.billing_notifications import notify_burner_cluster_report
    from orchestra.routines.card_gate_sweep import report_burner_clusters

    result = report_burner_clusters(session)
    notify_burner_cluster_report(result)
    return {"status": "success", **result.to_dict()}


# NOTE: ``POST /admin/billing/health`` was retired alongside the
# ``orchestra.routines.billing_health`` routine when the v2 billing
# refactor folded health-snapshot KPIs into Grafana. The reconciliation
# routine (``POST /admin/billing/reconcile``) still surfaces critical
# discrepancies via Discord; aggregate counts live in the Grafana
# billing dashboard. There is no caller of the old endpoint anywhere
# in this repo, the console, or any Cloud Scheduler entry — removed
# rather than stubbed for that reason.


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


@router.get("/billing/runtime-access")
def get_runtime_access(
    user_id: Optional[str] = None,
    organization_id: Optional[int] = None,
    session=Depends(get_db_session),
) -> dict:
    """Whether metered runtime work may be started for this account.

    The same question ``require_console_origin_for_free_accounts`` answers,
    asked on behalf of a dispatcher rather than a caller. That dependency
    reads the origin off the request, which works while a person is asking
    for work; scheduled work has no such request. A schedule is armed once
    and fired thereafter by Cloud Tasks, so every run after the first
    reaches the runtime with no origin left to inspect, and the gate that
    refuses a free account never sees it.

    Answering by account rather than by request closes that: a dispatcher
    knows whose assistant it holds even when it knows nothing about who
    armed the schedule.
    """
    from orchestra.lib.trial_subscription import has_api_access

    ba = BillingAccountDAO(session).resolve(user_id, organization_id)
    allowed = has_api_access(session, ba)
    return {
        "allowed": allowed,
        "reason": None if allowed else "payment_required",
    }


@router.get("/billing/account-info")
def get_billing_account_info(
    user_id: Optional[str] = None,
    organization_id: Optional[int] = None,
    session=Depends(get_db_session),
) -> dict:
    """
    Get billing account info (stripe customer ID, credits, account
    status) for a user or organization.

    Accepts ``user_id`` or ``organization_id`` (exactly one).

    :param user_id: User ID (for personal billing accounts).
    :param organization_id: Organization ID (for org billing accounts).
    :return: Billing account details.
    """
    from orchestra.lib.billing import fetch_billing_profile_from_stripe

    ba = _resolve_billing_account(
        session,
        user_id=user_id,
        organization_id=organization_id,
    )
    result: dict = {
        "billing_account_id": ba.id,
        "stripe_customer_id": ba.stripe_customer_id,
        "credits": float(ba.credits) if ba.credits else 0,
        "account_status": ba.account_status,
        # NULL = invoicer falls back to the per-CollectionMethod default
        # (``['card']`` for AUTO_CARD, ``['card', 'customer_balance']``
        # for SEND_INVOICE_NET_30).
        "preferred_payment_method_types": ba.preferred_payment_method_types,
        # Business profile snapshot — PII lives only on the Stripe Customer
        # now, so fetch it live for the admin UI rather than reading a local
        # mirror. ``is_business`` is the derived local flag.
        "billing_profile": {
            **fetch_billing_profile_from_stripe(ba.stripe_customer_id),
            "is_business": bool(ba.is_business),
        },
        # Self-serve switch catalog assignment. NULL = self-serve
        # switching disabled for this account; admins can still call
        # ``POST /v0/admin/billing/plan`` to change the active
        # template directly. The admin org page renders this as a
        # searchable dropdown driven by ``GET /v0/admin/billing/plans/groups``.
        "plan_group_id": ba.plan_group_id,
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
    "/cleanup/abandoned-provider-accounts",
    summary="Admin: Release provider accounts from unfinished connect attempts",
    description="Delete connected accounts left behind by connect attempts the "
    "user never completed. Called by scheduled cleanup job.",
)
def admin_cleanup_abandoned_provider_accounts(
    older_than_seconds: int = 3600,
    limit: int = 500,
    session=Depends(get_db_session),
) -> dict:
    """Release provider accounts nobody finished connecting.

    A provider creates the connected account when the auth link is issued,
    not when the user authorises, so an abandoned attempt leaves a real
    account behind that no disconnect will ever reach — the user never
    believes they connected anything. Nothing else reclaims them.

    :param older_than_seconds: Only touch attempts idle at least this long.
    :param limit: Most accounts to release in one pass.
    :param session: Database session.
    :return: Counts of candidates, released and held.
    """
    from orchestra.web.api.integrations.operations import (
        release_abandoned_provider_accounts,
    )

    try:
        result = release_abandoned_provider_accounts(
            session,
            older_than_seconds=older_than_seconds,
            limit=limit,
        )
        session.commit()
        return {
            **result,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": f"Released {result['released']} abandoned account(s)",
        }
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to release abandoned provider accounts: {str(e)}",
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
    assistant_id: int | None = Query(
        None,
        description="When set, only claim cleanup tasks for this assistant.",
    ),
    session: Session = Depends(get_db_session),
) -> dict:
    """Drain the assistant cleanup retry queue for a bounded batch of tasks."""
    try:
        result = await process_assistant_cleanup_tasks(
            session,
            limit=limit,
            assistant_id=assistant_id,
        )
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


@router.post(
    "/partition-prune-monitor",
    summary="Scan for unpruned partitioned-table queries",
    description=(
        "Read-only scan of pg_stat_statements for queries against the "
        "LIST(project_id)-partitioned tables (log_event, log_event_context, "
        "embedding, embedding_queue) that lack a project_id predicate and thus "
        "fan out across every tenant's partition."
    ),
    tags=["Monitoring"],
)
def admin_partition_prune_monitor(
    session: Session = Depends(get_db_session),
) -> dict:
    """Report partitioned-table queries not pruned by project_id (read-only)."""
    from orchestra.routines.partition_prune_monitor import run_partition_prune_monitor

    return run_partition_prune_monitor(session)


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


# ===========================================================================
# Managed-billing admin endpoints
#
# Templates + plan assignments are admin-managed: contracts are
# negotiated, not self-served. The customer-facing surface (in
# ``orchestra/web/api/billing/views.py``) only reads the result via
# ``GET /v0/billing/account-info`` and ``GET /v0/billing/invoices``.
# ===========================================================================


def _enforce_at_boundary(effective_at: Optional[datetime]) -> datetime:
    """HTTP wrapper around the AT_BOUNDARY rule.

    Defaults ``None`` to the next-month boundary and converts a
    non-boundary value into HTTP 400. The calendar math itself lives
    in ``billing_plan_assignment_dao`` so the DAO and other routines
    can share it.
    """
    from orchestra.db.dao.billing_plan_assignment_dao import (
        is_month_boundary_utc,
        next_month_boundary_utc,
    )

    if effective_at is None:
        return next_month_boundary_utc()
    if not is_month_boundary_utc(effective_at):
        raise HTTPException(
            status_code=400,
            detail=(
                "effective_at must be midnight UTC on the 1st of a month "
                "(AT_BOUNDARY policy). Mid-period plan changes are not "
                "yet supported."
            ),
        )
    moment = effective_at
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


@router.post(
    "/billing/plans/templates",
    response_model=BillingPlanTemplateResponse,
    summary="Admin: Create a billing plan template",
    description=(
        "Create an immutable, named billing plan template. Templates are the "
        "catalog of contracts that can be assigned to billing accounts. To "
        "'edit' a template, create a new one with `supersedes_template_id` "
        "pointing at the old one and (optionally) deprecate the old one."
    ),
)
def admin_create_billing_template(
    body: BillingPlanTemplateCreate,
    session=Depends(get_db_session),
) -> BillingPlanTemplateResponse:
    """Create a new billing plan template (admin-only)."""
    from decimal import Decimal as _D

    from orchestra.db.dao.billing_plan_template_dao import BillingPlanTemplateDAO
    from orchestra.db.models.orchestra_models import (
        BillingMode,
        CollectionMethod,
        FxPolicy,
        ProrationPolicy,
    )

    def _enum_or_400(enum_cls, value, field):
        try:
            return enum_cls(value)
        except ValueError:
            allowed = ", ".join(e.value for e in enum_cls)
            raise HTTPException(
                status_code=400,
                detail=f"Invalid {field}={value!r}. Must be one of: {allowed}",
            )

    template_dao = BillingPlanTemplateDAO(session)
    if template_dao.get_by_name(body.name) is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Template with name {body.name!r} already exists.",
        )

    try:
        template = template_dao.create_template(
            name=body.name,
            display_name=body.display_name,
            billing_mode=_enum_or_400(BillingMode, body.billing_mode, "billing_mode"),
            is_custom=body.is_custom,
            is_active=body.is_active,
            description=body.description,
            commit_amount=(
                _D(str(body.commit_amount)) if body.commit_amount is not None else None
            ),
            currency=body.currency,
            commit_period=body.commit_period,
            commit_schedule=body.commit_schedule,
            collection_method=_enum_or_400(
                CollectionMethod,
                body.collection_method,
                "collection_method",
            ),
            proration_policy=_enum_or_400(
                ProrationPolicy,
                body.proration_policy,
                "proration_policy",
            ),
            credits_rollover_policy=body.credits_rollover_policy,
            fx_policy=(
                _enum_or_400(FxPolicy, body.fx_policy, "fx_policy")
                if body.fx_policy is not None
                else None
            ),
            fx_locked_rate=(
                _D(str(body.fx_locked_rate))
                if body.fx_locked_rate is not None
                else None
            ),
            supersedes_template_id=body.supersedes_template_id,
            created_by_user_id=body.created_by_user_id,
        )
    except HTTPException:
        # Re-raise enum-validation errors from _enum_or_400 verbatim.
        session.rollback()
        raise
    except Exception as exc:
        # Surface DB check-constraint violations (positive commit_amount
        # requires commit_period, non-USD requires fx_policy, etc.) as
        # HTTP 400 rather than 500.
        session.rollback()
        raise HTTPException(status_code=400, detail=f"Template invalid: {exc}")

    session.commit()
    return BillingPlanTemplateResponse.from_orm_row(template)


@router.get(
    "/billing/plans/templates",
    response_model=List[BillingPlanTemplateResponse],
    summary="Admin: List billing plan templates",
    description=(
        "List billing plan templates. Defaults to active catalog rows "
        "(``is_custom=false`` AND ``is_active=true``). Pass query "
        "parameters ``include_custom=true`` to also return bespoke "
        "per-customer contracts, and ``include_inactive=true`` to also "
        "return deprecated rows."
    ),
)
def admin_list_billing_templates(
    include_custom: Optional[bool] = Query(default=None),
    include_inactive: bool = Query(default=False),
    session=Depends(get_db_session),
) -> List[BillingPlanTemplateResponse]:
    """List billing plan templates filtered by catalog placement.

    Two orthogonal axes:

    * ``include_custom`` — None = both, True = custom-only,
      False = catalog-only (the default for self-serve UIs).
    * ``include_inactive`` — False = active-only (default),
      True = also return deprecated rows.
    """
    from orchestra.db.dao.billing_plan_template_dao import BillingPlanTemplateDAO

    templates = BillingPlanTemplateDAO(session).list_catalog(
        include_custom=include_custom,
        include_inactive=include_inactive,
    )
    return [BillingPlanTemplateResponse.from_orm_row(t) for t in templates]


@router.post(
    "/billing/plans/templates/{template_id}/deprecate",
    summary="Admin: Deprecate a billing plan template",
    description=(
        "Flip ``is_active`` to false. Existing assignments keep working; "
        "new assignments to this template are blocked. The ``is_custom`` "
        "flag is preserved (a deprecated bespoke contract stays bespoke). "
        "No-op for templates that are already inactive."
    ),
)
def admin_deprecate_billing_template(
    template_id: int,
    session=Depends(get_db_session),
) -> dict:
    """Deprecate a template (flip ``is_active`` to false).

    Refuses with HTTP 409 if any account still has the template as
    their active assignment — see ``TemplateInUseError`` in the DAO
    for the full rationale.
    """
    from orchestra.db.dao.billing_plan_template_dao import (
        BillingPlanTemplateDAO,
        TemplateInUseError,
    )

    template_dao = BillingPlanTemplateDAO(session)
    template = template_dao.get_by_id(template_id)
    if template is None:
        raise HTTPException(
            status_code=404,
            detail=f"Template id={template_id} not found.",
        )
    try:
        template_dao.deprecate(template_id)
    except TemplateInUseError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    session.commit()
    # Re-read so the response reflects the new state.
    refreshed = template_dao.get_by_id(template_id)
    return {
        "status": "ok",
        "template_id": template_id,
        "is_active": bool(refreshed.is_active) if refreshed else None,
        "is_custom": bool(refreshed.is_custom) if refreshed else None,
    }


@router.post(
    "/billing/stripe-customer",
    response_model=EnsureStripeCustomerResponse,
    summary="Admin: Ensure a billing account has a Stripe Customer",
    description=(
        "Idempotent: returns the existing ``stripe_customer_id`` if set, "
        "otherwise creates a Stripe Customer using the BillingProfile "
        "fields (and the optional fallbacks for any missing values). "
        "Required prerequisite for assigning a METERED template to an "
        "account that has never gone through the Checkout flow."
    ),
)
def admin_ensure_stripe_customer(
    body: EnsureStripeCustomerRequest,
    session: Session = Depends(get_db_session),
) -> EnsureStripeCustomerResponse:
    """Create-or-return a Stripe Customer for a billing account."""
    from orchestra.lib.billing import ensure_stripe_customer

    ba = _resolve_billing_account(
        session,
        user_id=body.user_id,
        organization_id=body.organization_id,
    )
    pre_existing = ba.stripe_customer_id

    from orchestra.lib.subscription_billing import resolve_is_business

    is_business = (
        body.is_business
        if body.is_business is not None
        else resolve_is_business(ba, body.organization_id)
    )

    try:
        customer_id = ensure_stripe_customer(
            session,
            ba,
            is_business=is_business,
            fallback_email=body.fallback_email,
            fallback_name=body.fallback_name,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except stripe.error.StripeError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Stripe API error: {exc}",
        )

    session.commit()
    return EnsureStripeCustomerResponse(
        billing_account_id=ba.id,
        stripe_customer_id=customer_id,
        created=pre_existing is None,
    )


@router.put(
    "/billing/profile",
    response_model=BillingProfileResponse,
    summary="Admin: Update the business profile of a billing account",
    description=(
        "Partial update — only fields explicitly set on the request "
        "are written. ``billing_address`` is merged with the existing "
        "dict (so the caller can patch a single line). If the account "
        "already has a ``stripe_customer_id``, the same fields are "
        "best-effort synced to Stripe so the next invoice picks them "
        "up; sync failures are logged but do not fail the request "
        "(matches the customer-facing endpoint contract)."
    ),
)
def admin_update_billing_profile(
    body: BillingProfileUpdateRequest,
    session=Depends(get_db_session),
) -> BillingProfileResponse:
    """Admin-side equivalent of the customer-facing profile update."""
    from orchestra.lib.billing import (
        ensure_stripe_customer,
        fetch_billing_profile_from_stripe,
        is_billing_address_complete,
        sync_billing_profile_to_stripe,
    )

    log = logging.getLogger(__name__)

    ba = _resolve_billing_account(
        session,
        user_id=body.user_id,
        organization_id=body.organization_id,
    )

    # PII is no longer stored locally — read the current profile from Stripe
    # so a partial tax-id patch can resolve the country, then push the
    # changes back to Stripe (the source of truth).
    existing_profile = fetch_billing_profile_from_stripe(ba.stripe_customer_id)
    existing_address = existing_profile.get("billing_address") or {}

    # Derive the business flag: an explicit override wins, otherwise a
    # non-empty tax ID flips it on (else preserve the existing flag).
    if body.is_business is not None:
        is_business = body.is_business
    elif body.tax_id is not None:
        is_business = bool((body.tax_id or "").strip())
    else:
        is_business = bool(ba.is_business)

    # Create the Stripe Customer if one doesn't exist yet (so the profile has
    # somewhere to live), seeding it with whatever the admin provided.
    if not ba.stripe_customer_id:
        try:
            ensure_stripe_customer(
                session,
                ba,
                is_business=is_business,
                name=body.name,
                email=body.billing_email,
                address=body.billing_address,
                tax_id=body.tax_id,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except stripe.error.StripeError as exc:
            raise HTTPException(status_code=502, detail=f"Stripe API error: {exc}")

    # Push the changes to Stripe so the next invoice / current Customer
    # record reflect them. Only fields the caller actually provided are
    # forwarded — ``sync_billing_profile_to_stripe`` already skips ``None``.
    if ba.stripe_customer_id:
        sync_billing_profile_to_stripe(
            ba.stripe_customer_id,
            is_business=is_business,
            billing_email=body.billing_email,
            name=body.name,
            tax_id=body.tax_id,
            billing_address=body.billing_address,
            existing_billing_address=existing_address,
            logger_instance=log,
        )

    # Re-read the merged profile from Stripe so the response and the derived
    # ``billing_setup_complete`` gate reflect what's actually on file — an
    # admin address edit must keep the gate fresh just like the user PATCH.
    profile = fetch_billing_profile_from_stripe(ba.stripe_customer_id)
    merged_address = profile.get("billing_address") or {}
    billing_setup_complete = is_billing_address_complete(merged_address)

    ba.is_business = is_business
    ba.billing_setup_complete = billing_setup_complete
    session.commit()

    return BillingProfileResponse(
        billing_account_id=ba.id,
        billing_email=profile.get("billing_email"),
        name=profile.get("name"),
        tax_id=profile.get("tax_id"),
        tax_id_type=profile.get("tax_id_type"),
        billing_address=merged_address,
        billing_setup_complete=billing_setup_complete,
        is_business=is_business,
    )


@router.patch(
    "/billing/payment-preferences",
    response_model=PaymentPreferencesResponse,
    summary="Admin: Set the payment-method preference for a billing account",
    description=(
        "Override which Stripe payment methods are exposed on hosted "
        "invoices for a specific billing account. Use this to mark a "
        "customer as wire-only (``['customer_balance']``), card-only "
        "(``['card']``), or any combination supported by the "
        "PaymentMethodType enum.\n\n"
        "Pass ``preferred_payment_method_types: null`` (or omit it) to "
        "clear the override and fall back to the per-CollectionMethod "
        "defaults: ``['card']`` for AUTO_CARD, "
        "``['card', 'customer_balance']`` for SEND_INVOICE_NET_30. "
        "Validation is strict — empty lists, duplicates, and unknown "
        "method names are rejected with 400."
    ),
)
def admin_set_payment_preferences(
    body: PaymentPreferencesRequest,
    session: Session = Depends(get_db_session),
) -> PaymentPreferencesResponse:
    """Set or clear the per-customer payment-method override."""
    ba = _resolve_billing_account(
        session,
        user_id=body.user_id,
        organization_id=body.organization_id,
    )
    try:
        BillingAccountDAO(session).set_payment_preferences(
            ba.id,
            preferred_payment_method_types=body.preferred_payment_method_types,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    session.commit()
    session.refresh(ba)
    return PaymentPreferencesResponse(
        billing_account_id=ba.id,
        preferred_payment_method_types=ba.preferred_payment_method_types,
    )


@router.post(
    "/billing/plans/set",
    summary="Admin: Set a billing account's active plan",
    description=(
        "Set the active plan for a billing account, atomically and "
        "uniformly. Replaces the previous assign/change/cancel triplet:\n"
        "* default → custom template (was assign)\n"
        "* template A → template B (was change_plan)\n"
        "* custom → DEFAULT_TEMPLATE_ID (was cancel — closes the "
        "active custom row and inserts a fresh default plan assignment "
        "row in the same transaction; the new row's `change_reason` "
        "documents *why* the cancellation happened)\n\n"
        "AT_BOUNDARY enforced: `effective_at` must be midnight UTC on "
        "the 1st of a month (defaults to next month). Settlement of "
        "in-flight balances is the operator's responsibility — call "
        "this AFTER any needed add_credits/deduct_credits adjustments. "
        "Idempotent: returns `status='noop'` when the account is "
        "already on `template_id`."
    ),
)
def admin_set_plan(
    body: SetPlanRequest,
    session=Depends(get_db_session),
) -> dict:
    """Set the active plan for a billing account."""
    from orchestra.db.dao.billing_plan_assignment_dao import (
        BillingPlanAssignmentDAO,
        ConcurrentPlanChangeError,
        PendingRechargesError,
        TemplateNotAssignableError,
    )
    from orchestra.db.dao.billing_plan_template_dao import BillingPlanTemplateDAO
    from orchestra.db.models.enums import BillingMode

    ba = _resolve_billing_account(
        session,
        user_id=body.user_id,
        organization_id=body.organization_id,
    )

    # METERED templates require a Stripe Customer on the account so the
    # metered invoicer can attach monthly invoices. CREDITS templates
    # don't need this — the wallet/checkout flow handles it. If the
    # template lookup misses, fall through and let set_plan() raise the
    # canonical 404.
    #
    # The implicit ``auto_create_stripe_customer`` escape hatch was
    # removed in 2026-05 (see ``AdminSetPlanRequest``); operators must
    # now provision a Stripe Customer up-front via
    # ``POST /v0/admin/billing/stripe-customer`` (the Business
    # Profile + Provision flow does this implicitly). We surface the
    # missing customer as a 409 with a "do this first" pointer rather
    # than silently doing it for them, so any state we set up via
    # ``ensure_stripe_customer`` lives in a single, explicit code
    # path.
    template = BillingPlanTemplateDAO(session).get_by_id(body.template_id)
    needs_stripe_customer = (
        template is not None
        and template.billing_mode == BillingMode.METERED
        and not ba.stripe_customer_id
    )
    if needs_stripe_customer:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Billing account {ba.id} has no Stripe Customer. "
                "METERED templates require one so the metered invoicer "
                "can attach monthly invoices. Provision the customer "
                "first via POST /v0/admin/billing/stripe-customer "
                "(or use the admin UI's Business Profile + Provision "
                "flow), then retry."
            ),
        )

    # ``_enforce_at_boundary`` handles ``None`` by returning the next
    # month boundary — honouring the AT_BOUNDARY proration policy by
    # default. Don't short-circuit on ``None``, or set_plan() falls back
    # to "now" and the new assignment starts mid-month.
    effective_at = _enforce_at_boundary(body.effective_at)
    plan_dao = BillingPlanAssignmentDAO(session)
    try:
        assignment = plan_dao.set_plan(
            billing_account_id=ba.id,
            template_id=body.template_id,
            created_by_user_id=body.created_by_user_id,
            change_reason=body.change_reason,
            effective_at=effective_at,
        )
    except TemplateNotAssignableError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except PendingRechargesError as exc:
        # 409 with a structured payload so the admin UI can list the
        # offending recharge ids and offer a "drain & retry" affordance
        # rather than the generic-error path.
        raise HTTPException(
            status_code=409,
            detail={
                "code": "pending_recharges",
                "message": str(exc),
                "billing_account_id": exc.billing_account_id,
                "pending_recharge_ids": exc.pending_recharge_ids,
            },
        )
    except ConcurrentPlanChangeError as exc:
        # Two writers raced on the same account; the partial unique
        # index rejected the second insert. The DAO already rolled
        # back, so the session is clean — just surface a structured
        # 409 the admin UI can recognise and retry after refetching
        # the active plan.
        raise HTTPException(
            status_code=409,
            detail={
                "code": "concurrent_plan_change",
                "message": str(exc),
                "billing_account_id": exc.billing_account_id,
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    session.commit()
    if assignment is None:
        return {
            "status": "noop",
            "billing_account_id": ba.id,
            "assignment": None,
            "message": (f"Account is already on template id={body.template_id}."),
        }
    return {
        "status": "ok",
        "billing_account_id": ba.id,
        "assignment": BillingPlanAssignmentResponse.from_orm_row(
            assignment,
        ).model_dump(),
    }


@router.get(
    "/billing/plans/active",
    summary="Admin: Get the active plan for a billing account",
    description=(
        "Return the active assignment for an account. Every account has "
        "an active assignment (the default plan), so the response "
        "always includes a real `active_assignment` object — `null` "
        "indicates an application-invariant violation that the daily "
        "reconciliation routine flags as critical."
    ),
)
def admin_get_active_plan(
    user_id: Optional[str] = None,
    organization_id: Optional[int] = None,
    session=Depends(get_db_session),
) -> dict:
    """Get the currently-active plan assignment for an account."""
    from orchestra.db.dao.billing_plan_assignment_dao import BillingPlanAssignmentDAO

    ba = _resolve_billing_account(
        session,
        user_id=user_id,
        organization_id=organization_id,
    )
    active = BillingPlanAssignmentDAO(session).get_active(ba.id)
    if active is None:
        # Application-invariant violation: every BA should have an
        # active assignment. Return a 500 with a clear pointer to the
        # reconciliation runbook so on-call doesn't have to guess.
        raise HTTPException(
            status_code=500,
            detail=(
                f"BillingAccount {ba.id} has no active "
                "BillingPlanAssignment. Application invariant violated "
                "(see managed-billing-runbook §6.1, "
                "plan_assignment_null_pointer)."
            ),
        )
    return {
        "billing_account_id": ba.id,
        "active_assignment": BillingPlanAssignmentResponse.from_orm_row(
            active,
        ).model_dump(),
    }


@router.get(
    "/billing/plans/history",
    response_model=PlanHistoryResponse,
    summary="Admin: List a billing account's plan history",
    description="Return all plan assignments (open + closed), newest first.",
)
def admin_list_plan_history(
    user_id: Optional[str] = None,
    organization_id: Optional[int] = None,
    limit: int = Query(100, ge=1, le=500),
    session=Depends(get_db_session),
) -> PlanHistoryResponse:
    """List the full plan history for an account."""
    from orchestra.db.dao.billing_plan_assignment_dao import BillingPlanAssignmentDAO

    ba = _resolve_billing_account(
        session,
        user_id=user_id,
        organization_id=organization_id,
    )
    rows = BillingPlanAssignmentDAO(session).list_history(ba.id, limit=limit)
    return PlanHistoryResponse(
        billing_account_id=ba.id,
        assignments=[BillingPlanAssignmentResponse.from_orm_row(r) for r in rows],
    )


@router.post(
    "/billing/invoice-metered-month/account",
    summary="Admin: Re-run the metered invoicer for one account/period",
    description=(
        "Per-account replay of the monthly metered-invoicer routine, "
        "for retrying one customer that failed in the scheduled bulk "
        "run without re-scanning every eligible account. The bulk "
        "run is owned by Cloud Scheduler "
        "(``orchestra-production-monthly-metered-invoicer``, "
        "``5 2 1 * *`` UTC — five minutes after the credits-mode "
        "scheduler) which fires the bulk endpoint "
        "``POST /admin/billing/invoice-metered-month``; this "
        "single-account endpoint is the narrow-blast-radius "
        "alternative for ops cases like \"we voided this customer's "
        'invoice in the Stripe dashboard, please regenerate it". '
        "Staging dry-runs invoke ``invoice_metered_month`` directly "
        "via the Python shell. See ``monthly_metered_invoicer`` "
        "module docstring for the full scheduling rationale."
        "\n\n"
        "``force=true`` deletes the existing ``Recharge`` row for the "
        "(account, period) before running so the routine doesn't "
        "short-circuit on the idempotency check — use only after voiding "
        "the corresponding Stripe invoice in the dashboard, otherwise the "
        "customer ends up with two invoices for the same month."
    ),
)
def admin_rerun_metered_invoicing_for_account(
    year: int,
    month: int,
    user_id: Optional[str] = None,
    organization_id: Optional[int] = None,
    force: bool = False,
    session: Session = Depends(get_db_session),
) -> dict:
    """Re-run the metered invoicer for a single billing account."""
    from orchestra.routines.monthly_metered_invoicer import (
        invoice_metered_month_for_account,
    )

    ba = _resolve_billing_account(
        session,
        user_id=user_id,
        organization_id=organization_id,
    )
    try:
        result = invoice_metered_month_for_account(
            ba.id,
            year,
            month,
            session=session,
            force=force,
        )
        session.commit()
        return {
            "status": "success",
            "billing_account_id": ba.id,
            "period": result.period,
            "accounts_invoiced": result.accounts_invoiced,
            "accounts_skipped": result.accounts_skipped,
            "accounts_failed": result.accounts_failed,
            "errors": result.errors,
        }
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        session.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Per-account metered re-run failed: {exc}",
        )


# ===========================================================================
# GET /v0/admin/invoices
#
# Cross-account invoice list for the admin console. Paginated over
# historical ``Recharge`` rows with optional synthesis of "upcoming"
# rows projected from active METERED assignments via
# ``monthly_metered_invoicer.estimate_in_progress_invoice``. The
# customer-scoped equivalent is ``GET /v0/billing/invoices`` in
# ``billing.views`` — that one is keyed off the API-key billing
# account; this one is unscoped admin tooling.
#
# Every historical row has a non-NULL ``stripe_invoice_id`` — this is
# strictly the "rows with a Stripe-side artefact" view. Wallet-only
# credits (admin ``payment``/``promo`` recharges) and stub
# ``PENDING_INVOICE`` rows whose Stripe invoice hasn't been created
# yet are excluded server-side: there's nothing to deep-link to and
# they aren't "invoices" in any operator-meaningful sense. UPCOMING
# rows are projections that will become Stripe invoices at period close.
#
# Currency is *not* converted: each row carries its literal contract
# currency (the template's ``currency`` field, falling back to
# ``USD`` for legacy rows where ``plan_id`` is NULL). Mixing
# currencies in a single total would be misleading; the FE renders
# the code beside each amount and any cross-currency rollup is
# intentionally out of scope here.
# ===========================================================================


def _org_owner_email_map(session, orgs) -> dict[str, str]:
    """Resolve org-owner login emails for a batch of ``Organization`` rows.

    An org-owned billing account has no ``User`` linked directly to the
    billing account (the org owns it), so the recipient email must come
    from the org owner. Billing PII (incl. any billing email) lives on the
    Stripe customer now, not locally, so the owner's login email is the
    reliable local signal for display.
    """
    owner_ids = {o.owner_id for o in orgs if o is not None}
    if not owner_ids:
        return {}
    rows = session.query(User.id, User.email).filter(User.id.in_(owner_ids)).all()
    return {uid: email for uid, email in rows if email}


@router.get(
    "/invoices",
    response_model=AdminInvoiceListResponse,
    summary="Admin: cross-account invoice list (historical + upcoming)",
    description=(
        "Returns historical invoice rows (``Recharge`` rows that have "
        "an associated Stripe invoice — every status from "
        "``INVOICE_CREATED`` through ``DISPUTED``) joined to recipient "
        "identity (org name when org-owned, user email otherwise) and "
        "plan metadata. Wallet-only admin recharges "
        "(``payment``/``promo``) and stub ``PENDING_INVOICE`` rows "
        "with no ``stripe_invoice_id`` yet are excluded — they have "
        "no Stripe-side artefact to deep-link to.\n\n"
        "When ``include_upcoming=true`` (default) and ``offset==0``, "
        "the response is prefixed with one synthesised ``UPCOMING`` "
        "row per active METERED assignment that hasn't been "
        "invoiced for the current calendar month yet. Synthesis "
        "calls ``estimate_in_progress_invoice`` per account; that "
        "fans out FX lookups so the route is intentionally not in "
        "the hot path of any customer-facing flow.\n\n"
        "Each row carries its literal contract currency (3-letter "
        "ISO). The endpoint never converts FX — common-denominator "
        "totals would hide cross-currency exposure and belong on a "
        "separate explicitly-priced rollup if ever needed."
    ),
)
def admin_list_invoices(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: Optional[str] = Query(
        None,
        description=(
            "Filter by status. Accepts any RechargeStatus value plus "
            "``UPCOMING`` to scope to projected rows only. "
            "Comma-separated list allowed (e.g. ``PAID,FAILED``)."
        ),
    ),
    currency: Optional[str] = Query(
        None,
        description="3-letter ISO. Filters historical rows by their plan's currency.",
    ),
    plan_template_id: Optional[int] = Query(
        None,
        description="Restrict to recharges whose assignment points at this template.",
    ),
    from_date: Optional[_dt.date] = Query(
        None,
        description="Inclusive lower bound on ``Recharge.invoice_group`` (period-end date).",
    ),
    to_date: Optional[_dt.date] = Query(
        None,
        description="Inclusive upper bound on ``Recharge.invoice_group``.",
    ),
    q: Optional[str] = Query(
        None,
        description=(
            "Recipient search — case-insensitive substring match against "
            "org name, user (login) email, or stripe_invoice_id. "
            "Numeric ``q`` also matches billing_account_id."
        ),
    ),
    include_upcoming: bool = Query(
        True,
        description="If true (default) and offset==0, prepend UPCOMING projections.",
    ),
    session: Session = Depends(get_db_session),
) -> AdminInvoiceListResponse:
    """List invoices across all billing accounts (admin)."""
    from sqlalchemy import func as sa_func
    from sqlalchemy import or_, select

    from orchestra.db.models.orchestra_models import (
        BillingPlanAssignment,
        BillingPlanTemplate,
    )
    from orchestra.routines.monthly_metered_invoicer import estimate_in_progress_invoice

    requested_statuses: Optional[set[str]] = None
    upcoming_only = False
    if status:
        requested_statuses = {s.strip().upper() for s in status.split(",") if s.strip()}
        if requested_statuses == {"UPCOMING"}:
            upcoming_only = True

    # ----- Build the historical (Recharge-backed) query --------------------
    # Surface every recharge except the internal-plumbing
    # PENDING_INVOICE bucket, which has no Stripe-side artefact yet
    # and exists only to mark "we owe this customer an invoice at
    # period close". Operators want to see it in the admin view too,
    # so we keep it under an explicit filter — opt-in via ``status``.
    default_visible = [
        RechargeStatus.INVOICE_CREATED.value,
        RechargeStatus.PAID.value,
        RechargeStatus.FAILED.value,
        RechargeStatus.DISPUTED.value,
        RechargeStatus.PENDING_INVOICE.value,
    ]

    # ``Organization`` and ``User`` both link *to* ``BillingAccount`` via
    # ``billing_account_id``. LEFT-OUTER-JOIN both and let the row-loop
    # pick whichever side resolved — exactly one will, by domain
    # invariant (a BA is owned by either a user or an org, never both).
    base_q = (
        select(
            Recharge,
            BillingAccount,
            Organization,
            User,
            BillingPlanAssignment,
            BillingPlanTemplate,
        )
        .join(BillingAccount, BillingAccount.id == Recharge.billing_account_id)
        .outerjoin(
            Organization,
            Organization.billing_account_id == BillingAccount.id,
        )
        .outerjoin(
            User,
            User.billing_account_id == BillingAccount.id,
        )
        .outerjoin(
            BillingPlanAssignment,
            BillingPlanAssignment.id == Recharge.plan_id,
        )
        .outerjoin(
            BillingPlanTemplate,
            BillingPlanTemplate.id == BillingPlanAssignment.template_id,
        )
    )

    if requested_statuses and not upcoming_only:
        # Drop the synthetic ``UPCOMING`` token before passing to SQL.
        sql_statuses = requested_statuses - {"UPCOMING"}
        if sql_statuses:
            base_q = base_q.where(Recharge.status.in_(sql_statuses))
        else:
            # User asked for UPCOMING + something invalid; degrade to default
            base_q = base_q.where(Recharge.status.in_(default_visible))
    else:
        base_q = base_q.where(Recharge.status.in_(default_visible))

    # Hard requirement for HISTORICAL rows: must have a Stripe invoice
    # behind them. Admin-driven wallet credits (``payment``/``promo``
    # types — manual top-ups and promos) never call Stripe.Invoice.create
    # so they have ``stripe_invoice_id IS NULL`` for life; ditto stub
    # ``PENDING_INVOICE`` rows that mark "we owe this customer money
    # at period close" before the invoicer has finalised anything. The
    # admin invoices view is strictly the "rows with a Stripe-side
    # artefact" surface — wallet-only adjustments live in their
    # respective transaction histories, not here. Forward-compatible
    # with any future no-invoice recharge type.
    base_q = base_q.where(Recharge.stripe_invoice_id.is_not(None))

    if currency:
        base_q = base_q.where(BillingPlanTemplate.currency == currency.upper())
    if plan_template_id is not None:
        base_q = base_q.where(BillingPlanAssignment.template_id == plan_template_id)
    if from_date is not None:
        base_q = base_q.where(Recharge.invoice_group >= from_date)
    if to_date is not None:
        base_q = base_q.where(Recharge.invoice_group <= to_date)
    if q:
        needle = f"%{q.strip()}%"
        clauses = [
            Organization.name.ilike(needle),
            User.email.ilike(needle),
            Recharge.stripe_invoice_id.ilike(needle),
        ]
        try:
            ba_id_match = int(q.strip())
            clauses.append(BillingAccount.id == ba_id_match)
        except ValueError:
            pass
        base_q = base_q.where(or_(*clauses))

    # Total count for pagination — issued before LIMIT/OFFSET so the FE
    # can render "showing 1-50 of N". Cheap because the same indexes
    # cover the WHERE clause.
    count_q = select(sa_func.count()).select_from(base_q.subquery())
    total = int(session.execute(count_q).scalar() or 0)

    historical_items: list[AdminInvoiceListItem] = []
    if not upcoming_only:
        rows = list(
            session.execute(
                base_q.order_by(Recharge.at.desc()).offset(offset).limit(limit),
            ).all(),
        )
        owner_emails = _org_owner_email_map(session, [r[2] for r in rows])
        for recharge, ba, org, user, _assignment, template in rows:
            ts = recharge.at
            if ts and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)

            # Resolve the displayed amount + currency.
            #
            # * METERED rows (produced by ``monthly_metered_invoicer``)
            #   carry ``detail.invoiced_local`` + ``detail.currency`` —
            #   the authoritative invoice face value in the contract
            #   currency, equal to ``commit_charge_local +
            #   overage_charge_local - grants_local``. This is the right
            #   number to show on the admin row because it matches what
            #   the customer was actually invoiced (see
            #   ``InvoicesTable.formatInvoiceAmount`` for the matching
            #   customer-side logic).
            # * Every other Stripe-backed row type (commit_topup for
            #   CREDITS COMMITMENT, legacy "auto" rows, …) is intrinsically
            #   USD-denominated — the plan template's ``currency`` is
            #   only meaningful for the METERED path, so we deliberately
            #   ignore it on the fallback and label the row USD to match
            #   ``Recharge.amount_usd``. (Manual top-ups / promos can't
            #   reach this code path — the stripe_invoice_id filter
            #   excludes them.)
            detail = recharge.detail or {}
            inv_local_raw = (
                detail.get("invoiced_local") if isinstance(detail, dict) else None
            )
            detail_ccy = detail.get("currency") if isinstance(detail, dict) else None
            if inv_local_raw is not None and detail_ccy:
                try:
                    display_amount = Decimal(str(inv_local_raw))
                    display_currency = str(detail_ccy)
                except (ArithmeticError, ValueError):
                    display_amount = Decimal(recharge.amount_usd)
                    display_currency = "USD"
            else:
                display_amount = Decimal(recharge.amount_usd)
                display_currency = "USD"

            historical_items.append(
                AdminInvoiceListItem(
                    kind="HISTORICAL",
                    id=recharge.id,
                    billing_account_id=ba.id,
                    recipient_kind="ORG" if org is not None else "USER",
                    recipient_id=(
                        str(org.id) if org is not None else (user.id if user else "")
                    ),
                    recipient_name=(org.name if org else (user.name if user else None)),
                    recipient_email=(
                        user.email
                        if user
                        else (owner_emails.get(org.owner_id) if org else None)
                    ),
                    at=ts.isoformat() if ts else "",
                    invoice_group=(
                        recharge.invoice_group.isoformat()
                        if recharge.invoice_group
                        else None
                    ),
                    type=recharge.type,
                    status=recharge.status,
                    amount=display_amount,
                    currency=display_currency,
                    stripe_invoice_id=recharge.stripe_invoice_id,
                    plan_assignment_id=recharge.plan_id,
                    plan_template_id=(template.id if template else None),
                    plan_template_name=(template.name if template else None),
                    plan_template_display_name=(
                        template.display_name
                        if template and getattr(template, "display_name", None)
                        else (template.name if template else None)
                    ),
                    billing_mode=(template.billing_mode if template else None),
                ),
            )

    # ----- Synthesise UPCOMING rows ---------------------------------------
    # Only on the first page, and only when the caller didn't filter
    # them out — otherwise pagination becomes ambiguous (UPCOMING rows
    # have no stable id to cursor against). One row per active METERED
    # assignment that hasn't been invoiced for the current period yet.
    upcoming_items: list[AdminInvoiceListItem] = []
    want_upcoming = (
        include_upcoming
        and (offset == 0 or upcoming_only)
        and (requested_statuses is None or "UPCOMING" in requested_statuses)
    )
    if want_upcoming:
        # Pull active METERED assignments, joined to BA + recipient.
        # ``billing_mode`` lives on the template, not the assignment.
        # ``period_end_label`` is the last day of the current calendar
        # month — the metered invoicer pins this on each Recharge as
        # ``invoice_group``, so it's also our key for "already
        # invoiced this period?" below.
        now_utc = datetime.now(timezone.utc)
        if now_utc.month == 12:
            next_month_start = now_utc.replace(
                year=now_utc.year + 1,
                month=1,
                day=1,
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
        else:
            next_month_start = now_utc.replace(
                month=now_utc.month + 1,
                day=1,
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
        period_end_label = next_month_start.date() - timedelta(days=1)
        metered_q = (
            select(
                BillingAccount,
                Organization,
                User,
                BillingPlanAssignment,
                BillingPlanTemplate,
            )
            .join(
                BillingPlanAssignment,
                BillingPlanAssignment.billing_account_id == BillingAccount.id,
            )
            .join(
                BillingPlanTemplate,
                BillingPlanTemplate.id == BillingPlanAssignment.template_id,
            )
            .outerjoin(
                Organization,
                Organization.billing_account_id == BillingAccount.id,
            )
            .outerjoin(
                User,
                User.billing_account_id == BillingAccount.id,
            )
            .where(
                BillingPlanTemplate.billing_mode == "METERED",
                BillingPlanAssignment.started_at <= now_utc,
                or_(
                    BillingPlanAssignment.ended_at.is_(None),
                    BillingPlanAssignment.ended_at > now_utc,
                ),
            )
        )
        if currency:
            metered_q = metered_q.where(
                BillingPlanTemplate.currency == currency.upper(),
            )
        if plan_template_id is not None:
            metered_q = metered_q.where(
                BillingPlanAssignment.template_id == plan_template_id,
            )
        if q:
            needle = f"%{q.strip()}%"
            mclauses = [
                Organization.name.ilike(needle),
                User.email.ilike(needle),
            ]
            try:
                ba_id_match = int(q.strip())
                mclauses.append(BillingAccount.id == ba_id_match)
            except ValueError:
                pass
            metered_q = metered_q.where(or_(*mclauses))

        metered_rows = list(session.execute(metered_q).all())
        upcoming_owner_emails = _org_owner_email_map(
            session,
            [r[1] for r in metered_rows],
        )
        for ba, org, user, assignment, template in metered_rows:
            try:
                est = estimate_in_progress_invoice(
                    session,
                    billing_account_id=ba.id,
                    as_of=now_utc,
                )
            except Exception:
                # Best-effort: a single account's projection failing
                # (FX provider down, malformed plan, etc.) shouldn't
                # take the whole admin page down. Skip and move on.
                continue
            if est is None:
                continue
            # Already invoiced for this period? Skip — the historical
            # row already represents it and double-counting on the
            # admin page would be confusing.
            already = session.execute(
                select(Recharge.id)
                .where(
                    Recharge.billing_account_id == ba.id,
                    Recharge.plan_id == assignment.id,
                    Recharge.invoice_group == period_end_label,
                )
                .limit(1),
            ).scalar()
            if already is not None:
                continue
            projected_amount = est.contract_usage_local
            # Subtract any in-period grants surfaced by the estimate;
            # ``raw_usage_local - grants`` is the "what we'd actually
            # bill today" view, which is what an operator wants to see.
            invoiced_local = getattr(est, "invoiced_local", None)
            if invoiced_local is not None:
                projected_amount = invoiced_local
            upcoming_items.append(
                AdminInvoiceListItem(
                    kind="UPCOMING",
                    id=None,
                    billing_account_id=ba.id,
                    recipient_kind="ORG" if org is not None else "USER",
                    recipient_id=(
                        str(org.id) if org is not None else (user.id if user else "")
                    ),
                    recipient_name=(org.name if org else (user.name if user else None)),
                    recipient_email=(
                        user.email
                        if user
                        else (upcoming_owner_emails.get(org.owner_id) if org else None)
                    ),
                    at=est.period_end_exclusive.isoformat(),
                    invoice_group=period_end_label.isoformat(),
                    type=None,
                    status="UPCOMING",
                    amount=Decimal(projected_amount),
                    currency=est.currency or "USD",
                    stripe_invoice_id=None,
                    plan_assignment_id=assignment.id,
                    plan_template_id=template.id,
                    plan_template_name=template.name,
                    plan_template_display_name=(
                        getattr(template, "display_name", None) or template.name
                    ),
                    billing_mode="METERED",
                ),
            )
        # Sort upcoming by projected amount desc — operators care about
        # exposure first, calendar second (all upcoming rows share the
        # same period-end date so date sorting wouldn't distinguish them).
        upcoming_items.sort(key=lambda it: it.amount, reverse=True)

    if upcoming_only:
        items = upcoming_items
    else:
        items = upcoming_items + historical_items

    return AdminInvoiceListResponse(
        invoices=items,
        limit=limit,
        offset=offset,
        total=total,
        upcoming_count=len(upcoming_items),
    )


# ===========================================================================
# Plan groups (admin)
# ===========================================================================
#
# Plan groups are catalog-scoping objects used by the customer-facing
# self-serve switch endpoint (``POST /v0/billing/plan``). The admin
# surface is intentionally minimal: groups have no domain logic of
# their own, and operators usually create one or two at platform
# launch and then forget about them. CRUD lives here so the admin UI
# has a single place to manage the catalog without spinning up a
# separate admin service.
#
# All policy decisions encoded here:
#   * No DAO unit tests (per product policy — surface is small enough
#     that the API tests cover the contract end-to-end);
#   * Group operations are admin-gated through the existing
#     /v0/admin/* router auth; no self-serve mutation;
#   * Member position rewrites use the DAO's clear-then-set so
#     re-ordering is atomic from the client's view.


# NOTE: Projection helpers (``_plan_group_to_schema`` /
# ``_plan_group_member_to_schema``) used to live here; they were
# inlined at every endpoint because the projection logic is
# lightweight (~10 lines, no shared invariants worth abstracting)
# and inlining keeps each endpoint readable as one self-contained
# unit. If a non-trivial transformation ever needs to be applied to
# every PlanGroup/Member projection, fold it into a DAO method on
# ``BillingPlanGroupDAO`` rather than reviving a private view-layer
# helper.


@router.post(
    "/billing/plans/groups",
    response_model=PlanGroupResponse,
    summary="Admin: Create a plan group",
    description=(
        "Create an empty plan group. Add members via "
        "``POST /v0/admin/billing/plans/groups/{id}/members``."
    ),
)
def admin_create_plan_group(
    body: PlanGroupCreateRequest,
    session: Session = Depends(get_db_session),
) -> PlanGroupResponse:
    from orchestra.db.dao.billing_plan_group_dao import (
        BillingPlanGroupDAO,
        PlanGroupMemberError,
    )

    dao = BillingPlanGroupDAO(session)
    try:
        group = dao.create_group(
            name=body.name,
            display_name=body.display_name,
            description=body.description,
            is_active=body.is_active,
            created_by_user_id=body.created_by_user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except PlanGroupMemberError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    session.commit()
    members = dao.list_members(group.id, include_inactive_templates=True)
    return PlanGroupResponse(
        id=group.id,
        name=group.name,
        display_name=group.display_name,
        description=group.description,
        is_active=bool(group.is_active),
        created_at=(
            group.created_at.isoformat()
            if group.created_at is not None
            else _dt.datetime.now(timezone.utc).isoformat()
        ),
        created_by_user_id=group.created_by_user_id,
        members=[
            PlanGroupMemberItem(
                template_id=m.template_id,
                template_name=m.template.name,
                template_display_name=m.template.display_name or m.template.name,
                is_active=bool(m.template.is_active),
                position=m.position,
                added_at=m.added_at.isoformat() if m.added_at is not None else None,
            )
            for m in members
        ],
    )


@router.get(
    "/billing/plans/groups",
    response_model=PlanGroupListResponse,
    summary="Admin: List all plan groups",
    description=(
        "Return a compact summary of every group (members are omitted "
        "for payload size). Use the per-group GET for the full member "
        "list. ``include_inactive=true`` includes deprecated groups."
    ),
)
def admin_list_plan_groups(
    include_inactive: bool = False,
    session: Session = Depends(get_db_session),
) -> PlanGroupListResponse:
    from orchestra.db.dao.billing_plan_group_dao import BillingPlanGroupDAO

    dao = BillingPlanGroupDAO(session)
    groups = dao.list_all(include_inactive=include_inactive)
    return PlanGroupListResponse(
        groups=[
            PlanGroupSummaryItem(
                id=g.id,
                name=g.name,
                display_name=g.display_name,
                is_active=bool(g.is_active),
                member_count=len(
                    dao.list_members(g.id, include_inactive_templates=True),
                ),
            )
            for g in groups
        ],
    )


@router.get(
    "/billing/plans/groups/{group_id}",
    response_model=PlanGroupResponse,
    summary="Admin: Get one plan group with its members",
)
def admin_get_plan_group(
    group_id: int,
    session: Session = Depends(get_db_session),
) -> PlanGroupResponse:
    from orchestra.db.dao.billing_plan_group_dao import BillingPlanGroupDAO

    dao = BillingPlanGroupDAO(session)
    group = dao.get_by_id(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail=f"plan_group id={group_id}")
    members = dao.list_members(group.id, include_inactive_templates=True)
    return PlanGroupResponse(
        id=group.id,
        name=group.name,
        display_name=group.display_name,
        description=group.description,
        is_active=bool(group.is_active),
        created_at=(
            group.created_at.isoformat()
            if group.created_at is not None
            else _dt.datetime.now(timezone.utc).isoformat()
        ),
        created_by_user_id=group.created_by_user_id,
        members=[
            PlanGroupMemberItem(
                template_id=m.template_id,
                template_name=m.template.name,
                template_display_name=m.template.display_name or m.template.name,
                is_active=bool(m.template.is_active),
                position=m.position,
                added_at=m.added_at.isoformat() if m.added_at is not None else None,
            )
            for m in members
        ],
    )


@router.patch(
    "/billing/plans/groups/{group_id}",
    response_model=PlanGroupResponse,
    summary="Admin: Update a plan group's metadata",
    description=(
        "Mutate any of ``display_name`` / ``description`` / ``is_active``. "
        "Pass empty string to clear a string field. Setting "
        "``is_active=false`` is refused with HTTP 409 if any account "
        "currently points at this group — reassign every account's "
        "``plan_group_id`` first."
    ),
)
def admin_update_plan_group(
    group_id: int,
    body: PlanGroupUpdateRequest,
    session: Session = Depends(get_db_session),
) -> PlanGroupResponse:
    from orchestra.db.dao.billing_plan_group_dao import (
        BillingPlanGroupDAO,
        PlanGroupInUseError,
        PlanGroupNotFoundError,
    )

    dao = BillingPlanGroupDAO(session)
    try:
        group = dao.update_group(
            group_id,
            display_name=body.display_name,
            description=body.description,
            is_active=body.is_active,
        )
    except PlanGroupNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PlanGroupInUseError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    session.commit()
    members = dao.list_members(group.id, include_inactive_templates=True)
    return PlanGroupResponse(
        id=group.id,
        name=group.name,
        display_name=group.display_name,
        description=group.description,
        is_active=bool(group.is_active),
        created_at=(
            group.created_at.isoformat()
            if group.created_at is not None
            else _dt.datetime.now(timezone.utc).isoformat()
        ),
        created_by_user_id=group.created_by_user_id,
        members=[
            PlanGroupMemberItem(
                template_id=m.template_id,
                template_name=m.template.name,
                template_display_name=m.template.display_name or m.template.name,
                is_active=bool(m.template.is_active),
                position=m.position,
                added_at=m.added_at.isoformat() if m.added_at is not None else None,
            )
            for m in members
        ],
    )


@router.post(
    "/billing/plans/groups/{group_id}/members",
    response_model=PlanGroupResponse,
    summary="Admin: Add a template to a plan group",
)
def admin_add_plan_group_member(
    group_id: int,
    body: PlanGroupAddMemberRequest,
    session: Session = Depends(get_db_session),
) -> PlanGroupResponse:
    from orchestra.db.dao.billing_plan_group_dao import (
        BillingPlanGroupDAO,
        PlanGroupMemberError,
        PlanGroupNotFoundError,
    )

    dao = BillingPlanGroupDAO(session)
    try:
        dao.add_member(
            group_id=group_id,
            template_id=body.template_id,
            position=body.position,
        )
    except PlanGroupNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PlanGroupMemberError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    session.commit()
    group = dao.get_by_id(group_id)
    members = dao.list_members(group.id, include_inactive_templates=True)
    return PlanGroupResponse(
        id=group.id,
        name=group.name,
        display_name=group.display_name,
        description=group.description,
        is_active=bool(group.is_active),
        created_at=(
            group.created_at.isoformat()
            if group.created_at is not None
            else _dt.datetime.now(timezone.utc).isoformat()
        ),
        created_by_user_id=group.created_by_user_id,
        members=[
            PlanGroupMemberItem(
                template_id=m.template_id,
                template_name=m.template.name,
                template_display_name=m.template.display_name or m.template.name,
                is_active=bool(m.template.is_active),
                position=m.position,
                added_at=m.added_at.isoformat() if m.added_at is not None else None,
            )
            for m in members
        ],
    )


@router.delete(
    "/billing/plans/groups/{group_id}/members/{template_id}",
    response_model=PlanGroupResponse,
    summary="Admin: Remove a template from a plan group",
)
def admin_remove_plan_group_member(
    group_id: int,
    template_id: int,
    session: Session = Depends(get_db_session),
) -> PlanGroupResponse:
    from orchestra.db.dao.billing_plan_group_dao import (
        BillingPlanGroupDAO,
        PlanGroupMemberError,
    )

    dao = BillingPlanGroupDAO(session)
    if dao.get_by_id(group_id) is None:
        raise HTTPException(status_code=404, detail=f"plan_group id={group_id}")
    try:
        dao.remove_member(group_id=group_id, template_id=template_id)
    except PlanGroupMemberError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    session.commit()
    group = dao.get_by_id(group_id)
    members = dao.list_members(group.id, include_inactive_templates=True)
    return PlanGroupResponse(
        id=group.id,
        name=group.name,
        display_name=group.display_name,
        description=group.description,
        is_active=bool(group.is_active),
        created_at=(
            group.created_at.isoformat()
            if group.created_at is not None
            else _dt.datetime.now(timezone.utc).isoformat()
        ),
        created_by_user_id=group.created_by_user_id,
        members=[
            PlanGroupMemberItem(
                template_id=m.template_id,
                template_name=m.template.name,
                template_display_name=m.template.display_name or m.template.name,
                is_active=bool(m.template.is_active),
                position=m.position,
                added_at=m.added_at.isoformat() if m.added_at is not None else None,
            )
            for m in members
        ],
    )


@router.put(
    "/billing/plans/groups/{group_id}/positions",
    response_model=PlanGroupResponse,
    summary="Admin: Re-order plan group members",
    description=(
        "Atomically rewrite the position column for the listed members. "
        "Members not listed retain their existing position. "
        "Two members cannot share a position; the DAO uses a "
        "clear-then-set pass so admins can swap rungs in a single "
        "request without an intermediate collision."
    ),
)
def admin_set_plan_group_positions(
    group_id: int,
    body: PlanGroupSetPositionsRequest,
    session: Session = Depends(get_db_session),
) -> PlanGroupResponse:
    from orchestra.db.dao.billing_plan_group_dao import (
        BillingPlanGroupDAO,
        PlanGroupMemberError,
        PlanGroupNotFoundError,
    )

    dao = BillingPlanGroupDAO(session)
    try:
        dao.set_positions(
            group_id=group_id,
            positions=[(e.template_id, e.position) for e in body.positions],
        )
    except PlanGroupNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PlanGroupMemberError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    session.commit()
    group = dao.get_by_id(group_id)
    members = dao.list_members(group.id, include_inactive_templates=True)
    return PlanGroupResponse(
        id=group.id,
        name=group.name,
        display_name=group.display_name,
        description=group.description,
        is_active=bool(group.is_active),
        created_at=(
            group.created_at.isoformat()
            if group.created_at is not None
            else _dt.datetime.now(timezone.utc).isoformat()
        ),
        created_by_user_id=group.created_by_user_id,
        members=[
            PlanGroupMemberItem(
                template_id=m.template_id,
                template_name=m.template.name,
                template_display_name=m.template.display_name or m.template.name,
                is_active=bool(m.template.is_active),
                position=m.position,
                added_at=m.added_at.isoformat() if m.added_at is not None else None,
            )
            for m in members
        ],
    )


@router.put(
    "/billing/accounts/plan-group",
    response_model=AssignPlanGroupResponse,
    summary="Admin: Assign a plan group on a billing account",
    description=(
        "Set ``BillingAccount.plan_group_id`` to ``group_id``. Every "
        "account is always on some group (NULL is no longer a valid "
        "state — see DEFAULT_PLAN_GROUP_ID); to revert an account to "
        "the platform default, pass ``group_id=1``. Setting a group "
        "does NOT change the active plan — call "
        "``POST /v0/admin/billing/plan`` separately. The customer-side "
        "switcher hides itself automatically when the active template "
        "isn't a member of the assigned group, so picking a group "
        "without first reassigning the customer's plan is harmless."
    ),
)
def admin_assign_plan_group(
    body: AssignPlanGroupRequest,
    user_id: Optional[str] = None,
    organization_id: Optional[int] = None,
    session: Session = Depends(get_db_session),
) -> AssignPlanGroupResponse:
    from orchestra.db.dao.billing_plan_group_dao import BillingPlanGroupDAO

    ba = _resolve_billing_account(
        session,
        user_id=user_id,
        organization_id=organization_id,
    )
    group = BillingPlanGroupDAO(session).get_by_id(body.group_id)
    if group is None:
        raise HTTPException(
            status_code=404,
            detail=f"plan_group id={body.group_id} not found",
        )
    ba.plan_group_id = group.id
    session.commit()
    return AssignPlanGroupResponse(
        billing_account_id=ba.id,
        plan_group_id=ba.plan_group_id,
        plan_group_name=group.display_name or group.name,
    )
