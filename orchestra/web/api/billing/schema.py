"""Pydantic schemas for billing endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from orchestra.db.dao.billing_plan_assignment_dao import EffectivePlan

# ---------------------------------------------------------------------------
# Checkout / Portal / Status (original billing schemas)
# ---------------------------------------------------------------------------


class CheckoutSessionResponse(BaseModel):
    """Response from the checkout-session endpoint."""

    url: str
    session_id: str


class PortalSessionResponse(BaseModel):
    """Response from the portal-session endpoint."""

    url: str


class CheckoutStatusResponse(BaseModel):
    """Response from the checkout-status endpoint."""

    status: Optional[str] = None
    payment_status: Optional[str] = None


class AutoRechargeResponse(BaseModel):
    """
    Combined auto-recharge settings and eligibility.

    Returned by ``GET /billing/auto-recharge``.
    """

    # Current settings
    enabled: bool = False
    threshold: float = 0.0
    qty: float = 25.0

    # Validation constraints (so frontends don't hardcode them)
    min_recharge_amount: float = 25.0

    # Eligibility (fraud-prevention spending gate)
    eligible: bool = False
    total_spending: float = 0.0
    minimum_spend_required: float = 0.0
    remaining_spend_needed: float = 0.0

    # Whether the Stripe customer has a default payment method on file
    has_payment_method: bool = False

    # If non-null, auto-recharge cannot be enabled and this explains why.
    # Possible values:
    #   "unpaid_invoice" – outstanding auto-recharge invoice being retried
    #   "account_status" – account is SUSPENDED / CLOSED
    #   "spending"       – spending threshold not met
    #   "payment_method" – no default payment method
    blocked_reason: Optional[str] = None


class AutoRechargeUpdateRequest(BaseModel):
    """
    Request body for ``PUT /billing/auto-recharge``.

    Only ``enabled`` is required.  ``threshold`` and ``qty`` are optional
    so callers can toggle the feature on/off without re-sending the amounts.
    """

    enabled: bool
    threshold: Optional[float] = None
    qty: Optional[float] = None


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
    credit balance, billing history indicator, auto-recharge settings,
    and account status.  Context (personal vs org) is derived from
    the API key.
    """

    billing_account_id: int
    credits: float = 0.0
    account_status: str = "ACTIVE"
    last_recharge_at: Optional[str] = None

    # Auto-recharge settings (mirrors AutoRechargeResponse subset)
    autorecharge: bool = False
    autorecharge_threshold: float = 0.0
    autorecharge_qty: float = 25.0

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
    of the account's current ``plan_group``, and must be active. The
    move always lands on the next-month boundary (AT_BOUNDARY policy);
    no client-supplied effective date is accepted to keep the rule
    rigid (any future flexibility — "switch immediately" — would
    require deliberate carve-outs, not silent client overrides).

    Optional ``change_reason`` is recorded on the new
    ``BillingPlanAssignment`` row for audit clarity.
    """

    template_id: int
    change_reason: Optional[str] = None


class SwitchPlanResponse(BaseModel):
    """Response from ``POST /v0/billing/plan``.

    Two-step language so the UI can render a "scheduled" state until
    the period boundary lands. ``status`` is ``"scheduled"`` whenever
    a new assignment row is created (always with a future
    ``effective_at`` under AT_BOUNDARY) and ``"noop"`` when the
    request asked for the template the account is already on.
    """

    status: str  # scheduled | noop
    billing_account_id: int
    template_id: int
    effective_at: Optional[str] = None
    classification: str  # upgrade | downgrade | sidegrade | current


class InvoiceListItem(BaseModel):
    """One historical invoice for the workspace.

    Surfaced to customers via ``GET /v0/billing/invoices`` so they can
    see what they were billed (independent of Stripe portal access).
    Only INVOICE_CREATED / PAID / FAILED rows are returned — the
    PENDING_INVOICE bucket is internal plumbing for the autorecharge +
    monthly invoicer pipelines.
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
    plan_template_name: Optional[str] = None
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
    name: Optional[str] = None
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
