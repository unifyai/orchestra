"""Pydantic schemas for billing endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field

from orchestra.web.api.utils.safe_text import OptionalSafeLabel

if TYPE_CHECKING:
    from orchestra.db.dao.billing_plan_assignment_dao import EffectivePlan

# ---------------------------------------------------------------------------
# Portal (original billing schemas)
# ---------------------------------------------------------------------------


class PortalSessionResponse(BaseModel):
    """Response from the portal-session endpoint."""

    url: str


class SetupIntentResponse(BaseModel):
    """Client secret for confirming a new card via Stripe Elements.

    The secret authorizes the browser to attach exactly one payment method to
    the customer; the Stripe secret key stays on the backend.
    """

    client_secret: str


class PaymentMethodCard(BaseModel):
    """One saved card on the Stripe customer."""

    id: str
    brand: Optional[str] = None
    last4: Optional[str] = None
    exp_month: Optional[int] = None
    exp_year: Optional[int] = None
    # True for the card backing subscription renewals
    # (customer ``invoice_settings.default_payment_method``).
    is_default: bool = False


class PaymentMethodListResponse(BaseModel):
    """The customer's saved cards (newest-first as Stripe returns them)."""

    payment_methods: list[PaymentMethodCard] = Field(default_factory=list)


class CurrentPlanSummary(BaseModel):
    """Compact summary of the active plan for a billing account.

    Returned as part of ``AccountInfoResponse``. Always populated:
    every account has an active assignment from signup (default by
    default) so the UI can render a single uniform card backed by a
    real assignment row.

    ``plan_type`` is *derived* server-side from ``commit_amount`` —
    positive amount = ``"COMMITMENT"``, NULL/zero = ``"PAY_AS_YOU_GO"``
    — so client code that switches on this string still works without
    knowing the rule.
    """

    assignment_id: int
    template_id: int
    template_name: str
    # Customer-facing label, used in dashboard plan cards. Always populated
    # (falls back to ``template_name`` server-side when no explicit label).
    template_display_name: str
    plan_type: str  # derived: "PAY_AS_YOU_GO" | "COMMITMENT"
    billing_mode: str  # CREDITS | METERED
    commit_amount: Optional[float] = None
    currency: str = "USD"
    commit_period: Optional[str] = None  # MONTHLY | QUARTERLY | ANNUAL
    # When/how the customer is invoiced for the commit fee. ``None`` for
    # PAYG plans where there is no commitment to schedule.
    commit_schedule: Optional[str] = None  # MONTHLY | QUARTERLY | ANNUAL | UPFRONT
    collection_method: str = "AUTO_CARD"
    started_at: Optional[str] = None
    ended_at: Optional[str] = None

    @classmethod
    def from_effective_plan(cls, plan: "EffectivePlan") -> "CurrentPlanSummary":
        """Project a resolved ``EffectivePlan`` (Decimals) onto the JSON schema (floats)."""
        plan_type = (
            "COMMITMENT"
            if plan.commit_amount is not None and plan.commit_amount > 0
            else "PAY_AS_YOU_GO"
        )
        return cls(
            assignment_id=plan.assignment_id,
            template_id=plan.template_id,
            template_name=plan.template_name,
            template_display_name=plan.template_display_name,
            plan_type=plan_type,
            billing_mode=plan.billing_mode,
            commit_amount=(
                float(plan.commit_amount) if plan.commit_amount is not None else None
            ),
            currency=plan.currency,
            commit_period=plan.commit_period,
            commit_schedule=plan.commit_schedule,
            collection_method=plan.collection_method,
            started_at=plan.started_at.isoformat() if plan.started_at else None,
            ended_at=plan.ended_at.isoformat() if plan.ended_at else None,
        )


class AccountInfoResponse(BaseModel):
    """
    Response from ``GET /billing/account-info``.

    Returns the key billing account fields needed by the frontend:
    credit balance, billing history indicator, and account status.
    Context (personal vs org) is derived from the API key.
    """

    billing_account_id: int
    credits: float = 0.0
    account_status: str = "ACTIVE"
    last_recharge_at: Optional[str] = None

    # Managed-billing: surfaces the active plan so the UI can render
    # a CREDITS or METERED variant of the billing page from a single
    # endpoint. ``billing_mode`` is the primary discriminator the
    # frontend should branch on.
    billing_mode: str = "CREDITS"
    plan: Optional[CurrentPlanSummary] = None
    # Self-serve switch catalog id, NULL if the account has no
    # ``plan_group_id`` (no self-serve switching exposed; only admins
    # can call ``set_plan``). The frontend uses this as the gate for
    # rendering the "Switch plan" section — empty list from
    # ``GET /billing/available-plans`` is treated the same as missing.
    plan_group_id: Optional[int] = None

    # === SELF-SERVE SUBSCRIPTION (console subscription billing page) ===
    # ``is_subscribed`` is true only when the account has an active Stripe
    # subscription backing a self-serve CREDITS tier — the single flag the
    # console branches on to render the "subscribed" vs "free/unsubscribed"
    # variant of the page. METERED enterprise accounts are never
    # ``is_subscribed`` (they invoice in arrears, no subscription).
    is_subscribed: bool = False
    # NOTE: the monthly credit allowance is intentionally NOT duplicated
    # here — it is exactly ``plan.commit_amount`` (1 credit = $1), so the
    # console reads it off the nested ``plan`` summary instead.
    # ISO-8601 UTC expiry of the unconsumed signup *trial* grant lot
    # (from the expiring-grant ledger). NULL once the trial grant is fully
    # consumed/forfeited or the account has subscribed (subscribers no
    # longer have a live trial grant to surface).
    trial_expires_at: Optional[str] = None
    # ISO-8601 UTC end of the current Stripe subscription period (the next
    # renewal/credit-reset date), mirrored from Stripe onto the billing
    # account. NULL for unsubscribed/free and METERED accounts.
    next_renewal_at: Optional[str] = None
    # Whether the active subscription is scheduled to cancel at the end of the
    # current period. When true the console shows a persistent "cancels on
    # {next_renewal_at}" indicator instead of "renews on". Always false for
    # unsubscribed accounts.
    subscription_cancel_at_period_end: bool = False


class AvailablePlanItem(BaseModel):
    """One entry in the customer-facing ``GET /billing/available-plans`` response.

    Mirrors :class:`PlanGroupAvailableMember` from the DAO, projected
    onto the JSON-friendly schema. ``position`` doubles as the rung
    order (lower = smaller tier; absent = unordered alternative). The
    ``classification`` field is server-derived so the UI can render a
    correct "Upgrade" / "Downgrade" / "Side-grade" label without
    re-implementing the rule client-side.

    ``effective_at`` is the moment the switch would take effect if
    confirmed *now* — always the next AT_BOUNDARY, surfaced so the UI
    can render "starts on Mar 1" without doing calendar math.
    """

    template_id: int
    template_name: str
    template_display_name: str
    billing_mode: str  # CREDITS | METERED
    commit_amount: Optional[float] = None
    currency: str
    commit_period: Optional[str] = None
    commit_schedule: Optional[str] = None
    base_pricing_factor: float = 1.0
    overage_pricing_factor: float = 1.0
    position: Optional[int] = None
    is_current: bool = False
    # Server-derived label for the switch direction. ``"current"`` for
    # the rung the account is on; ``"upgrade"`` / ``"downgrade"`` when
    # both positions are populated and an order can be derived;
    # ``"sidegrade"`` for unordered groups or when one of the positions
    # is NULL.
    classification: str
    # ISO-8601 next-month boundary timestamp the switch would land on.
    effective_at: str


class AvailablePlansResponse(BaseModel):
    """Response from ``GET /v0/billing/available-plans``.

    Always returns a list — empty when the account has no
    ``plan_group_id`` set (no switching catalog) or when the group has
    zero active members. The frontend uses the empty case to hide the
    "Switch plan" section entirely.

    ``next_period_start`` is the AT_BOUNDARY timestamp that every
    member's ``effective_at`` resolves to. Surfaced once at the top
    level (and again per-member for client-side convenience) so the
    confirmation modal can show one consistent date.
    """

    billing_account_id: int
    plan_group_id: Optional[int] = None
    plan_group_display_name: Optional[str] = None
    next_period_start: str
    available: list[AvailablePlanItem]


class SwitchPlanRequest(BaseModel):
    """Body for ``POST /v0/billing/plan`` (customer-facing self-serve switch).

    The customer asks to be moved to ``template_id`` — must be a member
    of the account's current ``plan_group``, and must be active. No
    client-supplied effective date is accepted; the timing is server-
    determined by the account's billing model (see
    :class:`SwitchPlanResponse`):

    * Self-serve subscription tiers (the account is on a Stripe
      subscription) apply **immediately** with Stripe proration — the
      cycle anchor resets to now, upgrades grant the credit delta on the
      spot, downgrades take effect now without clawing back consumed
      credits (``status="switched"``).
    * METERED / non-subscription accounts still land on the next-month
      boundary under the legacy AT_BOUNDARY policy (``status="scheduled"``).

    Optional ``change_reason`` is recorded on the new
    ``BillingPlanAssignment`` row for audit clarity.
    """

    template_id: int
    change_reason: Optional[str] = None


class SwitchPlanResponse(BaseModel):
    """Response from ``POST /v0/billing/plan``.

    Three states:

    * ``"switched"`` — immediate change applied. Self-serve subscription
      accounts change tiers on the spot (anniversary-anchored, with
      Stripe proration); ``effective_at`` is *now*.
    * ``"scheduled"`` — a new assignment row was created for a future
      ``effective_at`` (legacy AT_BOUNDARY path for METERED / non-
      subscription accounts).
    * ``"noop"`` — the request asked for the template the account is
      already on.
    """

    status: str  # switched | scheduled | noop
    billing_account_id: int
    template_id: int
    effective_at: Optional[str] = None
    classification: str  # upgrade | downgrade | sidegrade | current


class SubscribeRequest(BaseModel):
    """Body for ``POST /v0/billing/subscribe`` (self-serve subscription).

    The customer subscribes to ``template_id`` — must be an active
    self-serve tier (CREDITS / STRIPE_SUBSCRIPTION) that is a member of
    the account's plan group. Creates the backing Stripe Subscription
    and activates the plan immediately; the monthly credits are granted
    once Stripe collects the first invoice.
    """

    template_id: int


class SubscribeResponse(BaseModel):
    """Response from ``POST /v0/billing/subscribe``.

    ``client_secret`` / ``hosted_invoice_url`` let the frontend complete
    payment when the customer has no usable default payment method yet
    (Stripe ``default_incomplete`` flow). Credits are not granted until
    the resulting ``invoice.paid`` webhook fires.
    """

    status: str  # subscribed
    billing_account_id: int
    template_id: int
    stripe_subscription_id: str
    subscription_status: Optional[str] = None
    client_secret: Optional[str] = None
    hosted_invoice_url: Optional[str] = None


class CancelSubscriptionResponse(BaseModel):
    """Response from ``DELETE /v0/billing/subscription``.

    Default cancellation is scheduled at the end of the current billing
    period (``status="canceling"``): the customer keeps credits + service
    until ``effective_at``, when Stripe deletes the subscription and the
    webhook reverts the account to the free tier. An immediate cancel
    returns ``status="canceled"`` with a null ``effective_at``.
    """

    status: str  # canceling | canceled
    billing_account_id: int
    effective_at: Optional[str] = None


class AutoIncrementResponse(BaseModel):
    """Response from ``GET`` / ``PUT`` ``/v0/billing/auto-increment``.

    ``auto_increment`` controls opt-in auto-upgrade-on-depletion: when
    the wallet hits zero the subscription is bumped to the next tier up
    the ladder (capped at the top tier; never auto-downgrades). When
    disabled, depletion is a hard stop until the customer upgrades
    manually.
    """

    enabled: bool = False
    # True when the account is on a self-serve subscription tier (the
    # only state where auto-increment is meaningful). The UI hides the
    # toggle otherwise.
    is_subscribed: bool = False
    # True when the account is already on the top tier of its ladder —
    # auto-increment can be enabled but will hard-stop at depletion.
    at_top_tier: bool = False


class AutoIncrementUpdateRequest(BaseModel):
    """Body for ``PUT /v0/billing/auto-increment``."""

    enabled: bool


class InvoiceListItem(BaseModel):
    """One historical invoice for the workspace.

    Surfaced to customers via ``GET /v0/billing/invoices`` so they can
    see what they were billed (independent of Stripe portal access).
    Only INVOICE_CREATED / PAID / FAILED rows are returned — the
    PENDING_INVOICE bucket is internal plumbing for the monthly metered
    invoicer pipeline.
    """

    id: int
    at: str
    type: str
    amount_usd: float
    quantity: float
    status: str
    invoice_group: Optional[str] = None
    stripe_invoice_id: Optional[str] = None
    plan_assignment_id: Optional[int] = None
    # Backend identifier (e.g. ``tier_50_annual``) — kept for correlation.
    plan_template_name: Optional[str] = None
    # Customer-facing label (e.g. ``$600 / yr``); falls back to the
    # backend name server-side. Preferred for display in the invoices table.
    plan_template_display_name: Optional[str] = None
    detail: Optional[Dict[str, Any]] = None


class InvoiceListResponse(BaseModel):
    """Paginated wrapper for ``GET /v0/billing/invoices``."""

    billing_account_id: int
    invoices: list[InvoiceListItem]
    limit: int
    offset: int


class InvoiceUrlsResponse(BaseModel):
    """Stripe-hosted view + PDF URLs for a single invoice.

    Returned by ``GET /v0/billing/invoices/{recharge_id}/urls`` so the
    frontend can render proper "View" and "Download" buttons that point
    at customer-facing Stripe URLs (rather than the internal
    dashboard.stripe.com URL the invoice id resolves to). URLs may be
    ``None`` when Stripe has yet to finalise the invoice.
    """

    recharge_id: int
    stripe_invoice_id: Optional[str] = None
    hosted_invoice_url: Optional[str] = None
    invoice_pdf_url: Optional[str] = None


class CurrentPeriodUsageResponse(BaseModel):
    """Mid-period usage snapshot for a METERED billing account.

    Drives the in-progress progress bar on the customer billing page.
    All ``_local`` quantities are in the contract currency
    (``CurrentPlanSummary.currency``); USD-template accounts have
    ``_local`` == raw USD.

    Mid-period FX is best-effort: LOCKED templates use the locked rate;
    SPOT and PERIOD_AVERAGE fall back to today's spot quote so the
    estimate moves smoothly throughout the month. The finalized invoice
    will use the policy-correct rate at period close, which can differ
    from this preview by a small amount.
    """

    period_start: str  # ISO date (UTC, inclusive)
    period_end: str  # ISO date (UTC, exclusive)
    currency: str
    raw_usage_local: float
    contract_usage_local: float
    commit_amount: Optional[float] = None
    # Estimated invoice line for the period so far:
    #   PAYG       → contract_usage_local
    #   COMMITMENT → max(commit_amount, contract_usage_local)
    invoiced_estimate_local: float
    # Amount above the commit (COMMITMENT only; 0 for PAYG and for
    # COMMITMENT periods that haven't burned through the floor yet).
    overage_local: float


# ---------------------------------------------------------------------------
# Unified Billing Profile Schemas
# ---------------------------------------------------------------------------


class BillingProfileResponse(BaseModel):
    """
    Unified billing profile response for both personal and org contexts.

    Returned by ``GET /billing/billing-profile``.
    Context (personal vs org) is derived from the API key.
    """

    billing_email: Optional[str] = None
    name: Optional[str] = None
    tax_id: Optional[str] = None
    tax_id_type: Optional[str] = None
    billing_address: Dict[str, Any] = Field(default_factory=dict)
    billing_setup_complete: bool = False
    is_business: bool = False


class BillingAddress(BaseModel):
    """Structured billing address — only known fields are accepted."""

    model_config = ConfigDict(extra="forbid")

    line1: Optional[str] = None
    line2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    postal_code: Optional[str] = None
    country: Optional[str] = None


class BillingProfileUpdate(BaseModel):
    """
    Unified billing profile update for both personal and org contexts.

    Accepted by ``PATCH /billing/billing-profile``.
    """

    billing_email: Optional[str] = None
    name: OptionalSafeLabel = None
    tax_id: Optional[str] = None
    tax_id_type: Optional[str] = None
    billing_address: Optional[BillingAddress] = None


# ---------------------------------------------------------------------------
# Tax Validation Schemas
# ---------------------------------------------------------------------------


class TaxIdValidationRequest(BaseModel):
    """Request body for ``POST /billing/validate-tax-id``."""

    tax_id: str = Field(..., description="Tax ID to validate")
    country: str = Field(..., description="Two-letter country code")


# ---------------------------------------------------------------------------
# Card-Gated Trial Schemas
# ---------------------------------------------------------------------------


class TrialCheckoutResponse(BaseModel):
    """Response for ``POST /billing/trial-checkout``."""

    checkout_url: str = Field(
        ...,
        description="Stripe-hosted Checkout page collecting the card and "
        "starting the auto-enrolled trial subscription.",
    )


class AccessGateResponse(BaseModel):
    """Response for ``GET /billing/access-gate``."""

    allowed: bool = Field(
        ...,
        description="Whether the account may use metered platform features.",
    )
    reason: Optional[str] = Field(
        None,
        description="Why access is denied (``card_required``) when not allowed.",
    )
    trial_end_at: Optional[str] = Field(
        None,
        description="ISO timestamp of the trial's first charge, when known.",
    )
    subscription_active: bool = Field(
        False,
        description="Whether a subscription (trialing or active) is linked.",
    )
