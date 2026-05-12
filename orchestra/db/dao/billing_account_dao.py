"""Data Access Object for BillingAccount operations."""

from __future__ import annotations

import decimal
import logging
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    RECHARGE_TYPE_PROMO,
    BillingAccount,
    BillingMode,
    BillingPlanAssignment,
    BillingPlanTemplate,
    Organization,
    Recharge,
    RechargeStatus,
    User,
)

logger = logging.getLogger(__name__)


# Minimum auto-recharge amount ($25 to avoid tiny invoices)
MIN_AUTORECHARGE_AMOUNT = decimal.Decimal("25")

# Minimum cumulative spending (in USD) required before a billing account
# can enable auto-recharge.  This is a fraud-prevention measure to stop
# bot accounts from setting up very low, repeated automatic top-ups and
# then disputing the charges.
MIN_SPEND_FOR_AUTO_RECHARGE = decimal.Decimal("1000")

# Valid account status values
VALID_ACCOUNT_STATUSES = {"ACTIVE", "SUSPENDED", "CLOSED"}


class BillingAccountDAO:
    """
    DAO for BillingAccount operations.

    Provides a unified interface for all billing operations that works
    identically for User and Organization billing accounts.
    """

    def __init__(self, session: Session):
        self.session = session

    # =========================================================================
    # CRUD
    # =========================================================================

    def create(self, **kwargs) -> BillingAccount:
        """
        Create a new billing account with its initial default plan.

        Establishes the v2 application invariant: every ``BillingAccount``
        gets an active default ``BillingPlanAssignment`` and a
        matching ``plan_assignment_id`` in the same flush window, so the
        account is never observably plan-less to a concurrent
        transaction. The DB column is nullable (PostgreSQL ``NOT NULL``
        is not deferrable, which would create a chicken-and-egg with
        the assignment row's FK back to the BA), but in production the
        only path to a NULL ``plan_assignment_id`` is bypassing this
        factory — the daily reconciliation routine flags any such row
        as ``plan_assignment_null_pointer`` (critical).

        :param kwargs: Optional initial field values (credits, etc.)
        :return: The created BillingAccount instance.
        """
        # Local import to avoid a top-level cycle: BillingPlanAssignmentDAO
        # references the same model module as BillingAccount and we keep
        # the import lazy so DAO load order stays stable.
        from orchestra.db.dao.billing_plan_assignment_dao import (
            BillingPlanAssignmentDAO,
        )

        billing_account = BillingAccount(**kwargs)
        self.session.add(billing_account)
        self.session.flush()  # Get the BA id

        BillingPlanAssignmentDAO(
            self.session,
        ).assign_default_at_signup(billing_account.id)
        # `assign_default_at_signup` syncs plan_assignment_id via
        # `_insert_active_assignment`; nothing further to do here.
        return billing_account

    def get(self, billing_account_id: int) -> Optional[BillingAccount]:
        """
        Get a billing account by ID.

        :param billing_account_id: BillingAccount ID.
        :return: BillingAccount object or None.
        """
        return (
            self.session.query(BillingAccount)
            .filter(BillingAccount.id == billing_account_id)
            .first()
        )

    def get_for_update(self, billing_account_id: int) -> Optional[BillingAccount]:
        """
        Get a billing account by ID, acquiring a ``FOR UPDATE`` row lock.

        Use this when you intend to read-then-write a mutable field
        (e.g. ``credits``, ``account_status``). The lock prevents
        concurrent transactions from reading the same row until this
        transaction commits or rolls back, eliminating lost-update races.

        :param billing_account_id: BillingAccount ID.
        :return: BillingAccount object or None.
        """
        return (
            self.session.query(BillingAccount)
            .filter(BillingAccount.id == billing_account_id)
            .with_for_update()
            .first()
        )

    def get_by_stripe_customer_id(
        self,
        stripe_customer_id: str,
    ) -> Optional[BillingAccount]:
        """
        Get a billing account by its Stripe customer ID.

        :param stripe_customer_id: The Stripe customer ID.
        :return: BillingAccount object or None.
        """
        query = select(BillingAccount).where(
            BillingAccount.stripe_customer_id == stripe_customer_id,
        )
        return self.session.execute(query).scalars().first()

    # =========================================================================
    # RESOLUTION (user / org → BillingAccount)
    # =========================================================================

    def resolve_for_user(self, user_id: str) -> Optional[BillingAccount]:
        """
        Look up a user's billing account.

        :param user_id: User ID.
        :return: BillingAccount, or None if user or BA not found.
        """
        user = self.session.query(User).filter(User.id == user_id).first()
        if not user or not user.billing_account_id:
            return None
        return self.get(user.billing_account_id)

    def resolve_for_org(self, organization_id: int) -> Optional[BillingAccount]:
        """
        Look up an organization's billing account.

        :param organization_id: Organization ID.
        :return: BillingAccount, or None if org or BA not found.
        """
        org = (
            self.session.query(Organization)
            .filter(Organization.id == organization_id)
            .first()
        )
        if not org or not org.billing_account_id:
            return None
        return self.get(org.billing_account_id)

    def resolve(
        self,
        user_id: str,
        organization_id: Optional[int] = None,
    ) -> Optional[BillingAccount]:
        """
        Resolve the billing account for a request context.

        If ``organization_id`` is given, returns the org's billing account.
        Otherwise returns the user's personal billing account.

        :param user_id: User ID.
        :param organization_id: Organization ID (None = personal context).
        :return: BillingAccount, or None if not found.
        """
        if organization_id is not None:
            return self.resolve_for_org(organization_id)
        return self.resolve_for_user(user_id)

    # =========================================================================
    # CREDITS
    # =========================================================================

    def get_credits(self, billing_account_id: int) -> decimal.Decimal:
        """
        Get the current credit balance.

        :param billing_account_id: BillingAccount ID.
        :return: Credit balance or 0.
        """
        ba = self.get(billing_account_id)
        if ba is None:
            return decimal.Decimal("0")
        return ba.credits

    def resolve_billing_mode(self, billing_account: BillingAccount) -> BillingMode:
        """Return the billing mode currently in force for an account.

        Single source of truth: ``BillingMode`` is intentionally not
        denormalised on ``BillingAccount`` (no cache to drift out of
        sync). Callers branching on mode (credit guards, response
        serialisers, invoicers) should always use this.

        One indexed JOIN to fetch the active assignment's template
        ``billing_mode``. Pristine accounts hit the seeded default
        template (``billing_mode='CREDITS'``) — no special case in
        Python; the row exists for them because both the migration
        backfill and ``BillingAccountDAO.create`` insert a default
        assignment.

        Falls back to ``CREDITS`` if the lookup returns nothing — that
        path is reachable only when ``plan_assignment_id IS NULL`` (a
        schema-invariant violation that the reconciliation routine
        flags as ``plan_assignment_null_pointer``, critical) or when
        the FK target was deleted out of band. CREDITS is the
        conservative fallback — it won't accidentally bypass a credit
        guard while the corruption is being investigated.
        """
        mode = self.session.execute(
            select(BillingPlanTemplate.billing_mode)
            .join(
                BillingPlanAssignment,
                BillingPlanAssignment.template_id == BillingPlanTemplate.id,
            )
            .where(BillingPlanAssignment.id == billing_account.plan_assignment_id),
        ).scalar_one_or_none()
        if mode is None:
            return BillingMode.CREDITS
        return BillingMode(mode)

    def set_payment_preferences(
        self,
        billing_account_id: int,
        *,
        preferred_payment_method_types: list[str] | None,
    ) -> BillingAccount:
        """Set (or clear) the per-customer payment-method override.

        Pass ``None`` to clear and fall back to the invoicer's per-
        ``CollectionMethod`` defaults. Pass a list of
        ``PaymentMethodType`` values (``"card"``, ``"customer_balance"``)
        to restrict / change what the customer sees on the hosted
        invoice page.

        Validation lives here (vs. the endpoint) so any caller — admin
        endpoint, internal script, future self-serve UI — gets the
        same guarantees:

        * Each entry must be a recognised ``PaymentMethodType`` value;
          unknown methods would silently break ``Invoice.create``.
        * The list must be non-empty (an empty list would leave the
          customer with literally no way to pay; clear with ``None``
          instead, which the invoicer interprets as "use the default").
        * No duplicates — Stripe rejects duplicate
          ``payment_method_types`` entries.

        Returns the refreshed ORM row so callers can immediately
        serialise it.
        """
        from orchestra.db.models.enums import PaymentMethodType

        ba = self.get(billing_account_id)
        if ba is None:
            raise ValueError(f"BillingAccount {billing_account_id} not found")

        if preferred_payment_method_types is not None:
            if len(preferred_payment_method_types) == 0:
                raise ValueError(
                    "preferred_payment_method_types cannot be empty — pass "
                    "None to clear and fall back to invoicer defaults.",
                )
            if len(set(preferred_payment_method_types)) != len(
                preferred_payment_method_types,
            ):
                raise ValueError(
                    "preferred_payment_method_types contains duplicates: "
                    f"{preferred_payment_method_types!r}",
                )
            allowed = {m.value for m in PaymentMethodType}
            unknown = [
                m for m in preferred_payment_method_types if m not in allowed
            ]
            if unknown:
                raise ValueError(
                    f"Unsupported payment method(s) {unknown!r}; "
                    f"allowed values are {sorted(allowed)!r}.",
                )

        ba.preferred_payment_method_types = preferred_payment_method_types
        self.session.flush()
        return ba

    def add_credits(
        self,
        billing_account_id: int,
        quantity: float,
        *,
        category: str = "recharge",
        assistant_id: int | None = None,
        user_id: str | None = None,
        organization_id: int | None = None,
        description: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> Optional[decimal.Decimal]:
        """
        Add credits to a billing account, dispatching on billing-mode.

        Acquires a ``FOR UPDATE`` row lock to prevent lost-update races
        when multiple transactions add/deduct credits concurrently.

        A :class:`CreditTransaction` ledger row is inserted atomically
        in the same transaction. The active plan-assignment in force at
        write time is captured on the ledger row from
        ``BillingAccount.plan_assignment_id`` (no caller-supplied override).

        Behaviour by mode (resolved from the account's active plan
        template):

        * **CREDITS** mode (default PAYG, future Pro plans): the wallet
          (``BillingAccount.credits``) is mutated. Returns the new wallet
          balance.
        * **METERED** mode (enterprise contracts): the wallet is left
          untouched and the ``monthly_metered_invoicer`` sums signed
          amounts at month-end (a grant on a METERED account is
          effectively a discount on the next invoice). Returns ``None``.

        In both modes a :class:`CreditTransaction` ledger row with the
        same shape is appended.

        Returns ``None`` if the account is not found.

        :param billing_account_id: BillingAccount ID.
        :param quantity: Positive number of credits to add.
        :param category: Ledger category.  Public inflows use ``'recharge'``,
            ``'promo'``, ``'refund'``, ``'dispute'``.  Internal reconciliation
            routines may use free-form diagnostic categories.
        :param assistant_id: Optional assistant context.
        :param user_id: Optional acting user.
        :param organization_id: Optional organization context.
        :param description: Human-readable description.
        :param detail: Category-specific JSONB metadata.
        :return: New credit balance for CREDITS mode, ``None`` for
            METERED or if the account is missing.
        """
        from orchestra.lib.billing_events import (
            track_balance_after,
            track_balance_before,
        )

        ba = self.get_for_update(billing_account_id)
        if ba is None:
            return None

        amount = decimal.Decimal(str(quantity))
        is_metered = self.resolve_billing_mode(ba) == BillingMode.METERED

        if not is_metered:
            track_balance_before(self.session, billing_account_id, ba.credits)
            ba.credits = ba.credits + amount
            track_balance_after(self.session, billing_account_id, ba.credits)

        self._record_transaction(
            billing_account_id=billing_account_id,
            amount=amount,
            category=category,
            assistant_id=assistant_id,
            user_id=user_id,
            organization_id=organization_id,
            description=description,
            detail=detail,
            plan_assignment_id=ba.plan_assignment_id,
        )

        return None if is_metered else ba.credits

    def deduct_credits(
        self,
        billing_account_id: int,
        quantity: float,
        *,
        category: str = "other",
        assistant_id: int | None = None,
        user_id: str | None = None,
        organization_id: int | None = None,
        description: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> Optional[decimal.Decimal]:
        """
        Deduct credits from a billing account, dispatching on billing-mode.

        Acquires a ``FOR UPDATE`` row lock to prevent lost-update races
        when multiple transactions add/deduct credits concurrently.

        A :class:`CreditTransaction` ledger row is inserted atomically
        in the same transaction. The active plan-assignment in force at
        write time is captured on the ledger row from
        ``BillingAccount.plan_assignment_id`` (no caller-supplied override).

        Behaviour by mode (resolved from the account's active plan
        template):

        * **CREDITS** mode: wallet is mutated (allowed to go negative
          so the spending-limit hook can block subsequent calls). Returns
          the new wallet balance.
        * **METERED** mode: wallet is left untouched; the monthly metered
          invoicer sums these debits at period end and produces a Stripe
          invoice. Returns ``None``.

        In both modes a :class:`CreditTransaction` ledger row with the
        same shape is appended.

        Returns ``None`` if the account is not found.

        :param billing_account_id: BillingAccount ID.
        :param quantity: Positive number of credits to deduct.
        :param category: Ledger category.  Public spending uses ``'llm'``,
            ``'hire'``, ``'resources'``, ``'media'``.  Internal reconciliation
            routines may use free-form diagnostic categories.
        :param assistant_id: Optional assistant context.
        :param user_id: Optional acting user.
        :param organization_id: Optional organization context.
        :param description: Human-readable description.
        :param detail: Category-specific JSONB metadata.
        :return: New credit balance for CREDITS mode, ``None`` for
            METERED or if the account is missing.
        """
        from orchestra.lib.billing_events import (
            track_balance_after,
            track_balance_before,
        )

        ba = self.get_for_update(billing_account_id)
        if ba is None:
            return None

        amount = decimal.Decimal(str(quantity))
        is_metered = self.resolve_billing_mode(ba) == BillingMode.METERED

        if not is_metered:
            track_balance_before(self.session, billing_account_id, ba.credits)
            ba.credits = ba.credits - amount

            if ba.credits < 0:
                logger.warning(
                    f"BillingAccount {billing_account_id} credits went negative: "
                    f"{ba.credits}. Deducted {quantity}.",
                )

            track_balance_after(self.session, billing_account_id, ba.credits)

        self._record_transaction(
            billing_account_id=billing_account_id,
            amount=-amount,
            category=category,
            assistant_id=assistant_id,
            user_id=user_id,
            organization_id=organization_id,
            description=description,
            detail=detail,
            plan_assignment_id=ba.plan_assignment_id,
        )

        return None if is_metered else ba.credits

    # ------------------------------------------------------------------
    # Ledger helpers
    # ------------------------------------------------------------------

    def _record_transaction(
        self,
        *,
        billing_account_id: int,
        amount: decimal.Decimal,
        category: str,
        assistant_id: int | None = None,
        user_id: str | None = None,
        organization_id: int | None = None,
        description: str | None = None,
        detail: dict[str, Any] | None = None,
        plan_assignment_id: int | None = None,
    ) -> None:
        """Insert a :class:`CreditTransaction` row."""
        from orchestra.db.dao.credit_transaction_dao import CreditTransactionDAO

        txn_dao = CreditTransactionDAO(self.session)
        txn_dao.insert(
            billing_account_id=billing_account_id,
            amount=amount,
            category=category,
            assistant_id=assistant_id,
            user_id=user_id,
            organization_id=organization_id,
            description=description,
            detail=detail,
            plan_assignment_id=plan_assignment_id,
        )

    # =========================================================================
    # STRIPE
    # =========================================================================

    def set_stripe_customer_id(
        self,
        billing_account_id: int,
        stripe_customer_id: str,
    ) -> bool:
        """
        Set the Stripe customer ID for a billing account.

        :param billing_account_id: BillingAccount ID.
        :param stripe_customer_id: Stripe customer ID.
        :return: True if successful, False if not found.
        """
        ba = self.get(billing_account_id)
        if ba is None:
            return False
        ba.stripe_customer_id = stripe_customer_id
        return True

    # =========================================================================
    # AUTORECHARGE
    # =========================================================================

    def get_autorecharge_settings(
        self,
        billing_account_id: int,
    ) -> Optional[dict]:
        """
        Get autorecharge settings.

        :param billing_account_id: BillingAccount ID.
        :return: Dict with autorecharge settings, or None.
        """
        ba = self.get(billing_account_id)
        if ba is None:
            return None

        return {
            "autorecharge": ba.autorecharge,
            "autorecharge_threshold": float(ba.autorecharge_threshold),
            "autorecharge_qty": float(ba.autorecharge_qty),
        }

    def set_autorecharge(
        self,
        billing_account_id: int,
        enabled: bool,
    ) -> bool:
        """Enable or disable autorecharge."""
        ba = self.get(billing_account_id)
        if ba is None:
            return False
        ba.autorecharge = enabled
        return True

    def set_autorecharge_threshold(
        self,
        billing_account_id: int,
        threshold: float,
    ) -> bool:
        """Set the autorecharge threshold."""
        ba = self.get(billing_account_id)
        if ba is None:
            return False
        ba.autorecharge_threshold = decimal.Decimal(str(threshold))
        return True

    def set_autorecharge_qty(
        self,
        billing_account_id: int,
        qty: float,
    ) -> bool:
        """
        Set the autorecharge quantity.

        :raises ValueError: If qty is below minimum.
        """
        qty_decimal = decimal.Decimal(str(qty))
        if qty_decimal < MIN_AUTORECHARGE_AMOUNT:
            raise ValueError(
                f"Minimum auto-recharge amount is "
                f"${MIN_AUTORECHARGE_AMOUNT}. Got ${qty_decimal}.",
            )

        ba = self.get(billing_account_id)
        if ba is None:
            return False
        ba.autorecharge_qty = qty_decimal
        return True

    # =========================================================================
    # ACCOUNT STATUS
    # =========================================================================

    def set_account_status(
        self,
        billing_account_id: int,
        status: str,
    ) -> bool:
        """
        Set the account status.

        :param billing_account_id: BillingAccount ID.
        :param status: Must be ACTIVE, SUSPENDED, or CLOSED.
        :raises ValueError: If status is invalid.
        """
        if status not in VALID_ACCOUNT_STATUSES:
            raise ValueError(
                f"Invalid account status: '{status}'. "
                f"Must be one of: {', '.join(sorted(VALID_ACCOUNT_STATUSES))}",
            )

        ba = self.get(billing_account_id)
        if ba is None:
            return False
        ba.account_status = status
        return True

    # =========================================================================
    # AUTO-RECHARGE ELIGIBILITY (fraud prevention)
    # =========================================================================

    def get_total_spending(self, billing_account_id: int) -> decimal.Decimal:
        """
        Calculate the cumulative amount (USD) a billing account has spent.

        Only considers recharges with status PAID and types 'payment' and
        'auto' (i.e. real money transactions – not promos).

        :param billing_account_id: BillingAccount ID.
        :return: Total spending in USD.
        """
        result = (
            self.session.query(func.coalesce(func.sum(Recharge.amount_usd), 0))
            .filter(
                Recharge.billing_account_id == billing_account_id,
                Recharge.status == RechargeStatus.PAID,
                Recharge.type.in_(["payment", "auto", "invoice"]),
            )
            .scalar()
        )
        return decimal.Decimal(str(result))

    def can_enable_auto_recharge(self, billing_account_id: int) -> bool:
        """
        Check whether a billing account is eligible to enable auto-recharge.

        The account must have spent at least ``MIN_SPEND_FOR_AUTO_RECHARGE``
        in real-money transactions.  This prevents bot accounts from setting
        up very low, repeated automatic top-ups and then disputing the
        charges.

        :param billing_account_id: BillingAccount ID.
        :return: True if cumulative spending meets the threshold.
        """
        total = self.get_total_spending(billing_account_id)
        return total >= MIN_SPEND_FOR_AUTO_RECHARGE

    def has_unpaid_auto_recharges(self, billing_account_id: int) -> bool:
        """Return True if the account has auto-recharge credits that
        are still awaiting payment.

        Checks for ``PENDING_INVOICE`` (invoice not yet created by
        Stripe) and ``INVOICE_CREATED`` (invoice created, collection
        in progress).

        ``FAILED`` is intentionally excluded: by the time a recharge
        reaches FAILED, the credits have already been voided and the
        Stripe invoice has been voided — the debt is settled.  Keeping
        FAILED here would permanently block auto-recharge after a
        single payment failure with no self-service recovery path.
        """
        return (
            self.session.query(Recharge)
            .filter(
                Recharge.billing_account_id == billing_account_id,
                Recharge.type == "auto",
                Recharge.status.in_(
                    [RechargeStatus.PENDING_INVOICE, RechargeStatus.INVOICE_CREATED],
                ),
            )
            .first()
            is not None
        )

    # =========================================================================
    # BILLING PROFILE
    # =========================================================================

    def update_billing_profile(
        self,
        billing_account_id: int,
        billing_email: Optional[str] = None,
        name: Optional[str] = None,
        tax_id: Optional[str] = None,
        tax_id_type: Optional[str] = None,
        billing_address: Optional[dict] = None,
    ) -> bool:
        """
        Update the business profile.

        Only updates fields that are provided (not None).
        Works identically for personal users and organizations.

        :param billing_account_id: BillingAccount ID.
        :param billing_email: Email for invoices.
        :param name: Display name (individual or business).
        :param tax_id: Tax identification number.
        :param tax_id_type: Stripe tax ID type code.
        :param billing_address: JSONB address dict.
        :return: True if successful, False if not found.
        """
        ba = self.get(billing_account_id)
        if ba is None:
            return False

        if billing_email is not None:
            ba.billing_email = billing_email
        if name is not None:
            ba.name = name
        if tax_id is not None:
            ba.tax_id = tax_id
        if tax_id_type is not None:
            ba.tax_id_type = tax_id_type
        if billing_address is not None:
            # Merge with existing address if partial update
            existing = ba.billing_address or {}
            ba.billing_address = {**existing, **billing_address}

        return True

    def apply_credit_grant(
        self,
        billing_account_id: int,
        credit_amount: float,
    ) -> Recharge:
        """
        Apply a promotional credit grant to a billing account.

        Adds credits to the account and creates a ``PAID`` promo
        :class:`Recharge` record so the account has billing history.

        Acquires a ``FOR UPDATE`` row lock so the credit addition is
        atomic with respect to concurrent deductions.

        Balance transitions are tracked automatically — a billing event
        is published after the session commits if the balance crossed
        zero in either direction.

        :param billing_account_id: BillingAccount ID.
        :param credit_amount: Amount of credits to grant.
        :return: The created Recharge record.
        :raises ValueError: If the billing account is not found.
        """
        from orchestra.lib.billing_events import (
            track_balance_after,
            track_balance_before,
        )

        ba = self.get_for_update(billing_account_id)
        if ba is None:
            raise ValueError(
                f"BillingAccount {billing_account_id} not found.",
            )

        track_balance_before(self.session, billing_account_id, ba.credits)
        amount = decimal.Decimal(str(credit_amount))
        ba.credits = ba.credits + amount
        track_balance_after(self.session, billing_account_id, ba.credits)

        self._record_transaction(
            billing_account_id=billing_account_id,
            amount=amount,
            category="promo",
            description="Promotional credit grant",
            plan_assignment_id=ba.plan_assignment_id,
        )

        recharge = Recharge(
            billing_account_id=billing_account_id,
            type=RECHARGE_TYPE_PROMO,
            quantity=amount,
            amount_usd=decimal.Decimal("0"),
            status=RechargeStatus.PAID,
        )
        self.session.add(recharge)
        self.session.flush()
        return recharge

    def grant_signup_credits(
        self,
        user_id: str,
        selected_type: str,
        organization_id: Optional[int] = None,
    ) -> Optional[Recharge]:
        """
        Grant one-time signup promo credits to the appropriate billing account.

        Called when a user completes the onboarding workspace-selection step.
        Credits go to the user's personal billing account when *selected_type*
        is ``"personal"``, or to the organization's billing account when it is
        ``"organization"``.

        Idempotent: silently returns ``None`` if the target billing account
        already has any promo recharge, so the grant is safe to call on
        retries, auto-complete, or when multiple org members complete
        onboarding for the same organization.

        :param user_id: The user completing onboarding.
        :param selected_type: ``"personal"`` or ``"organization"``.
        :param organization_id: Required when *selected_type* is
            ``"organization"``.
        :return: The created Recharge, or ``None`` if skipped.
        """
        from orchestra.settings import settings

        credit_amount = settings.signup_credit_grant
        if credit_amount <= 0:
            return None

        if selected_type == "organization":
            if organization_id is None:
                return None
            org = (
                self.session.query(Organization)
                .filter(Organization.id == organization_id)
                .first()
            )
            if not org or not org.billing_account_id:
                return None
            target_ba_id = org.billing_account_id
        else:
            user = self.session.query(User).filter(User.id == user_id).first()
            if not user or not user.billing_account_id:
                return None
            target_ba_id = user.billing_account_id

        existing_promo = (
            self.session.query(Recharge)
            .filter(
                Recharge.billing_account_id == target_ba_id,
                Recharge.type == RECHARGE_TYPE_PROMO,
            )
            .first()
        )
        if existing_promo:
            return None

        return self.apply_credit_grant(target_ba_id, credit_amount)

    def get_billing_profile(self, billing_account_id: int) -> Optional[dict]:
        """
        Get the billing profile.

        :param billing_account_id: BillingAccount ID.
        :return: Dict with billing profile data, or None.
            The ``name`` key is entity-agnostic; callers should map it
            to ``individual_name`` or ``business_name`` as appropriate.
        """
        ba = self.get(billing_account_id)
        if ba is None:
            return None

        return {
            "billing_email": ba.billing_email,
            "name": ba.name,
            "tax_id": ba.tax_id,
            "tax_id_type": ba.tax_id_type,
            "billing_address": ba.billing_address or {},
            "billing_setup_complete": ba.billing_setup_complete,
        }
