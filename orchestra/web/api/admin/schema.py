from __future__ import annotations

import datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Optional

from pydantic import BaseModel, PlainSerializer

if TYPE_CHECKING:
    from orchestra.db.models.orchestra_models import (
        BillingPlanAssignment,
        BillingPlanTemplate,
    )


# ---------------------------------------------------------------------------
# Money / decimal annotation
# ---------------------------------------------------------------------------
# Internally we want ``Decimal`` everywhere so admin tooling can pass
# bit-exact contract values straight through to the DB ``Numeric``
# columns without a binary-float round-trip mangling the cents. On the
# wire we still emit JSON numbers so existing FE consumers (typed as
# ``number`` in TypeScript) see no change. ``PlainSerializer`` runs at
# JSON-encoding time only — Python-side ``model_dump()`` callers and
# attribute access still see the underlying ``Decimal``.
Money = Annotated[
    Decimal,
    PlainSerializer(lambda v: float(v) if v is not None else None, when_used="json"),
]


class RechargeModelRequest(BaseModel):
    """
    Request model for creating a new recharge.

    Provide exactly one of ``user_id`` or ``organization_id`` to identify the
    billing account to credit.

    Attributes:
        user_id: User ID (for personal billing accounts).
        organization_id: Organization ID (for org billing accounts).
        quantity: The number of credits to add.
        type: Recharge type ("payment", "auto", "promo").
        transaction_id: Stripe transaction id (required for "payment" type).
        target_month: Target month for invoice grouping (format: "YYYY-MM").
                      Defaults to current month if not specified.
    """

    user_id: Optional[str] = None
    organization_id: Optional[int] = None
    quantity: Money
    type: str
    transaction_id: Optional[str] = None
    target_month: Optional[str] = None


class RechargeTypeModelRequest(BaseModel):
    """
    Request model for creating new recharge_type model.

    Attributes:
        type (str): The type of the recharge_type.
    """

    type: str


class UsersModelResponse(BaseModel):
    """
    Response model for users models.

    Attributes:
        id (str): The id of the users.
        billing_account_id (Optional[int]): The billing account ID.
    """

    model_config = {"from_attributes": True}

    id: str
    billing_account_id: Optional[int] = None


class RechargeTypeModelResponse(BaseModel):
    """
    Response model for recharge_type models.

    Attributes:
        type (str): The type of the recharge_type.
    """

    type: str


class RechargeModelResponse(BaseModel):
    """
    Response model for recharge models.

    Attributes:
        id (int): The id of the recharge.
        billing_account_id (int): The billing account ID.
        at (datetime): The time of the recharge.
        quantity (float): The quantity of the recharge.
        type (str): The type of the recharge.
    """

    model_config = {"from_attributes": True}

    id: int
    at: datetime.datetime
    billing_account_id: int
    quantity: Money
    type: str


# Organization list schemas for admin endpoint
class OrganizationListItem(BaseModel):
    """Response model for a single organization in the list."""

    id: int
    name: str
    owner_id: str
    owner_email: Optional[str] = None
    created_at: Optional[datetime.datetime]
    member_count: int


class OrganizationListResponse(BaseModel):
    """Response model for listing all organizations."""

    organizations: list[OrganizationListItem]
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Contact-type cost schemas
# ---------------------------------------------------------------------------


class AssistantContactCostRead(BaseModel):
    """Response model for a single contact-type cost row."""

    model_config = {"from_attributes": True}

    id: int
    contact_type: str
    provider: Optional[str] = None
    country_code: Optional[str] = None
    monthly_cost: Money
    one_time_cost: Money


class AssistantContactCostWrite(BaseModel):
    """Request model for creating or updating a contact-type cost row."""

    contact_type: str
    provider: Optional[str] = None
    country_code: Optional[str] = None
    monthly_cost: Money
    one_time_cost: Money = Decimal("0")


# ---------------------------------------------------------------------------
# Suspension reason
# ---------------------------------------------------------------------------


class SuspensionReason(StrEnum):
    """Valid reasons for suspending a billing account."""

    ADMIN_FREEZE = "admin_freeze"
    DISPUTE = "dispute"


# ---------------------------------------------------------------------------
# Managed-billing: plan templates + plan assignments
#
# Templates are immutable, named billing configurations. Assignments
# bind a template to a billing account over a time window. Both are
# admin-managed because contracts (commit amount, currency, overage
# policy) are negotiated, not self-served. See ``managed_billing.md``.
# ---------------------------------------------------------------------------


class BillingPlanTemplateCreate(BaseModel):
    """Request body for ``POST /v0/admin/billing/plans/templates``.

    Mirrors :meth:`BillingPlanTemplateDAO.create_template`. Enum-shaped
    fields accept the literal string value (e.g. ``"CREDITS"``) for HTTP
    transport ergonomics; the endpoint converts them.

    Plan-type ("PAYG" vs "COMMITMENT") is *derived* from
    ``commit_amount`` and is not a separate field. The platform never
    blocks usage based on plan terms (no overage policy, no usage cap)
    so those fields are absent too.
    """

    name: str
    # Optional customer-facing label; falls back to ``name`` when omitted.
    # This is what gets printed on Stripe invoice line items and dashboard
    # plan summaries — keep it short, capitalised, no internal jargon.
    display_name: Optional[str] = None
    billing_mode: str  # BillingMode: CREDITS | METERED
    description: Optional[str] = None
    # Catalog placement — two orthogonal booleans (replaces the legacy
    # ``availability`` enum). ``is_custom=True`` hides from the public
    # catalog (per-customer bespoke contract). ``is_active=False``
    # deprecates the template (no new assignments accepted).
    is_custom: bool = False
    is_active: bool = True
    commit_amount: Optional[Money] = None  # NULL/zero = pay-as-you-go
    currency: str = "USD"  # Invoice currency for the whole template
    commit_period: Optional[str] = None  # CommitPeriod: MONTHLY | QUARTERLY | ANNUAL
    commit_schedule: Optional[str] = None
    # Two-rate pricing that **stacks** on overage:
    # * ``base_pricing_factor`` applies to ALL usage (commit-included
    #   + overage + PAYG). 0.80 = 20% discount, 1.10 = 10% premium, etc.
    # * ``overage_pricing_factor`` is an ADDITIONAL multiplier on top
    #   of base, only for the overage portion. 1.0 = "no overage
    #   penalty" (base discount continues above commit); >1.0 = uplift
    #   (1.25 = 25% premium over the base rate above commit).
    # Effective above-commit rate = base × overage. Defaults of 1.0/1.0
    # reproduce list-price behaviour with no overage uplift. Both must
    # be > 0 (DB check constraint).
    base_pricing_factor: Money = Decimal("1.0")
    overage_pricing_factor: Money = Decimal("1.0")
    collection_method: str = "AUTO_CARD"
    proration_policy: str = "PRORATE"
    # Unused-credits behaviour at period-end. Only meaningful for
    # COMMITMENT+CREDITS plans (positive commit_amount + billing_mode=CREDITS);
    # the DAO + DB check constraint reject any other combination.
    credits_rollover_policy: Optional[str] = None
    # FX policy fields. ``fx_policy`` is NULL for USD templates (no
    # conversion needed) and required for non-USD templates. The DAO
    # rejects mismatched combinations (USD + fx_policy set, or non-USD +
    # fx_policy NULL) with a 400 before the DB check constraint fires.
    fx_policy: Optional[str] = None
    fx_locked_rate: Optional[Money] = None  # required iff LOCKED_RATE
    supersedes_template_id: Optional[int] = None
    created_by_user_id: Optional[str] = None


class BillingPlanTemplateResponse(BaseModel):
    """Single template row, as returned by the admin catalog endpoints."""

    id: int
    name: str
    # ``display_name`` may be NULL when no explicit customer label was
    # set; admin UIs should fall back to ``name`` in that case.
    display_name: Optional[str] = None
    description: Optional[str] = None
    billing_mode: str
    is_custom: bool
    is_active: bool
    commit_amount: Optional[Money] = None
    currency: str
    commit_period: Optional[str] = None
    commit_schedule: Optional[str] = None
    base_pricing_factor: Money
    overage_pricing_factor: Money
    collection_method: str
    proration_policy: str
    credits_rollover_policy: Optional[str] = None
    fx_policy: Optional[str] = None
    fx_locked_rate: Optional[Money] = None
    supersedes_template_id: Optional[int] = None
    created_at: datetime.datetime
    created_by_user_id: Optional[str] = None

    @classmethod
    def from_orm_row(
        cls,
        template: "BillingPlanTemplate",
    ) -> "BillingPlanTemplateResponse":
        """Project a ``BillingPlanTemplate`` ORM row onto the API schema.

        ORM ``Numeric`` columns surface as ``Decimal`` and flow
        straight through — the schema's :data:`Money` annotation
        carries the JSON-time float coercion so wire format is
        preserved while Python-side callers keep the bit-exact
        decimal value.
        """
        return cls(
            id=template.id,
            name=template.name,
            display_name=template.display_name,
            description=template.description,
            billing_mode=template.billing_mode,
            is_custom=bool(template.is_custom),
            is_active=bool(template.is_active),
            commit_amount=template.commit_amount,
            currency=template.currency,
            commit_period=template.commit_period,
            commit_schedule=template.commit_schedule,
            base_pricing_factor=template.base_pricing_factor,
            overage_pricing_factor=template.overage_pricing_factor,
            collection_method=template.collection_method,
            proration_policy=template.proration_policy,
            credits_rollover_policy=template.credits_rollover_policy,
            fx_policy=template.fx_policy,
            fx_locked_rate=template.fx_locked_rate,
            supersedes_template_id=template.supersedes_template_id,
            created_at=template.created_at,
            created_by_user_id=template.created_by_user_id,
        )


class SetPlanRequest(BaseModel):
    """Request body for ``POST /v0/admin/billing/plans/set``.

    Single endpoint covering the three plan transitions (formerly
    ``assign`` / ``change_plan`` / ``cancel``):

    * pristine → any template
    * template A → template B
    * non-default → ``DEFAULT_TEMPLATE_ID`` ("cancel")

    Identifies the target billing account by ``user_id`` OR
    ``organization_id`` (exactly one), matching the rest of the admin
    surface. ``effective_at`` is enforced to AT_BOUNDARY (midnight UTC
    on the 1st of a month) — explicit non-boundary values are rejected
    (PRORATE_IMMEDIATELY is deferred). Omit ``effective_at`` to apply
    immediately; rare and only useful for fresh accounts that have
    never had a non-default plan.

    The endpoint is idempotent: re-issuing the same call when the
    account is already on ``template_id`` returns ``status='noop'``.
    """

    user_id: Optional[str] = None
    organization_id: Optional[int] = None
    template_id: int
    change_reason: Optional[str] = None
    created_by_user_id: Optional[str] = None
    effective_at: Optional[datetime.datetime] = None
    # NOTE: ``auto_create_stripe_customer`` was removed in 2026-05.
    # METERED assignments now require the operator to have already
    # provisioned a Stripe Customer via
    # ``POST /v0/admin/billing/stripe-customer`` (the Business Profile
    # + Provision flow in the admin UI does this implicitly), so the
    # implicit-create escape hatch was dead code on the wire — the FE
    # never set the flag to ``true``. Keep it out of the request
    # schema so a future caller doesn't reintroduce a half-baked
    # implicit-customer path.


class BillingPlanAssignmentResponse(BaseModel):
    """One assignment row — the plan-history list item.

    There are no separate cancellation columns: when an account is
    moved off a non-default plan back to default, the *next* row's
    ``change_reason`` and ``created_by_user_id`` document the why and
    by-whom. Reading history newest-first reconstructs the narrative.

    ``template_plan_type`` is *derived* from the template's commit
    amount (``"COMMITMENT"`` for positive ``commit_amount``,
    ``"PAY_AS_YOU_GO"`` otherwise) so existing UI clients that switch
    on this string don't need to recompute it client-side.
    """

    id: int
    billing_account_id: int
    template_id: int
    template_name: str
    # Customer-facing label; falls back to ``template_name`` when the
    # template has no explicit ``display_name`` set.
    template_display_name: str
    template_billing_mode: str
    template_plan_type: str  # derived: "COMMITMENT" | "PAY_AS_YOU_GO"
    started_at: datetime.datetime
    ended_at: Optional[datetime.datetime] = None
    created_by_user_id: Optional[str] = None
    change_reason: Optional[str] = None

    @classmethod
    def from_orm_row(
        cls,
        assignment: "BillingPlanAssignment",
    ) -> "BillingPlanAssignmentResponse":
        """Project a ``BillingPlanAssignment`` ORM row onto the API schema.

        The template is eagerly accessed via the ORM relationship so the
        response carries the human-readable template name + key flags
        without a second round-trip.
        """
        template = assignment.template
        plan_type = (
            "COMMITMENT"
            if template.commit_amount is not None and template.commit_amount > 0
            else "PAY_AS_YOU_GO"
        )
        return cls(
            id=assignment.id,
            billing_account_id=assignment.billing_account_id,
            template_id=assignment.template_id,
            template_name=template.name,
            template_display_name=template.display_name or template.name,
            template_billing_mode=template.billing_mode,
            template_plan_type=plan_type,
            started_at=assignment.started_at,
            ended_at=assignment.ended_at,
            created_by_user_id=assignment.created_by_user_id,
            change_reason=assignment.change_reason,
        )


class PlanHistoryResponse(BaseModel):
    """List wrapper for plan history; mirrors the org list pattern."""

    billing_account_id: int
    assignments: list[BillingPlanAssignmentResponse]


# ---------------------------------------------------------------------------
# Stripe Customer provisioning (admin)
# ---------------------------------------------------------------------------


class EnsureStripeCustomerRequest(BaseModel):
    """Request body for ``POST /v0/admin/billing/stripe-customer``.

    Identifies the target billing account by ``user_id`` OR
    ``organization_id`` (exactly one). Optional ``fallback_email`` /
    ``fallback_name`` are used only when those fields are missing on
    the BillingAccount profile.

    ``is_business`` defaults to ``True`` for org-backed accounts and
    ``False`` for personal — pass an explicit value to override.
    """

    user_id: Optional[str] = None
    organization_id: Optional[int] = None
    fallback_email: Optional[str] = None
    fallback_name: Optional[str] = None
    is_business: Optional[bool] = None


class EnsureStripeCustomerResponse(BaseModel):
    """Response for the provisioning endpoint.

    ``created`` is ``True`` when a new Stripe Customer was created in
    this call; ``False`` when an existing ``stripe_customer_id`` was
    returned unchanged (idempotent).
    """

    billing_account_id: int
    stripe_customer_id: str
    created: bool


class BillingProfileUpdateRequest(BaseModel):
    """Request body for ``PUT /v0/admin/billing/profile``.

    Identifies the target billing account by ``user_id`` OR
    ``organization_id`` (exactly one). Every profile field is optional
    — only fields explicitly provided are updated (matching the
    ``BillingAccountDAO.update_billing_profile`` partial-update
    semantics). ``billing_address`` is merged with the existing dict
    rather than replaced wholesale, so the caller can update a single
    line without re-sending the whole address.

    If the BillingAccount already has a ``stripe_customer_id``, the
    updated fields are also pushed to Stripe via
    ``sync_billing_profile_to_stripe`` so the next invoice picks them
    up. Sync is best-effort (failures are logged, not raised) — same
    contract as the customer-facing profile-update endpoint.
    """

    user_id: Optional[str] = None
    organization_id: Optional[int] = None
    billing_email: Optional[str] = None
    name: Optional[str] = None
    tax_id: Optional[str] = None
    tax_id_type: Optional[str] = None
    billing_address: Optional[dict] = None
    # Hint forwarded to ``build_stripe_customer_name`` when syncing
    # ``name``. Defaults to ``True`` for org-backed accounts and
    # ``False`` for personal — pass an explicit value to override.
    is_business: Optional[bool] = None


class BillingProfileResponse(BaseModel):
    """Response shape for billing-profile reads/writes.

    Mirrors ``BillingAccountDAO.get_billing_profile`` plus the
    ``billing_account_id`` so callers can link the response back to
    the account.
    """

    billing_account_id: int
    billing_email: Optional[str]
    name: Optional[str]
    tax_id: Optional[str]
    tax_id_type: Optional[str]
    billing_address: dict


# ---------------------------------------------------------------------------
# Per-customer payment-method preferences (admin)
# ---------------------------------------------------------------------------


class PaymentPreferencesRequest(BaseModel):
    """Body for ``PATCH /v0/admin/billing/payment-preferences``.

    Identifies the target billing account by ``user_id`` OR
    ``organization_id`` (exactly one — same convention as the freeze
    endpoint).

    ``preferred_payment_method_types`` accepts:

    * ``None`` — clear the override and fall back to the invoicer's
      defaults (``['card']`` for AUTO_CARD, ``['card', 'customer_balance']``
      for SEND_INVOICE_NET_30).
    * A non-empty list of Stripe payment-method type strings drawn from
      :class:`PaymentMethodType` (today: ``"card"``, ``"customer_balance"``).
      Validated server-side; an empty list, duplicates, or unsupported
      values are 400s.
    """

    user_id: Optional[str] = None
    organization_id: Optional[int] = None
    preferred_payment_method_types: Optional[list[str]] = None


class PaymentPreferencesResponse(BaseModel):
    """Response for the payment-preferences endpoint."""

    billing_account_id: int
    preferred_payment_method_types: Optional[list[str]] = None


# ---------------------------------------------------------------------------
# Plan groups (curated bundles of switchable templates)
# ---------------------------------------------------------------------------


class PlanGroupCreateRequest(BaseModel):
    """Body for ``POST /v0/admin/billing/plans/groups``.

    ``name`` is the operator-facing slug (unique catalog-wide); the
    customer-facing label is ``display_name`` and falls back to
    ``name`` server-side when omitted. Pass an empty member list for
    a starter group, then add templates via the membership endpoints.
    """

    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    is_active: bool = True
    created_by_user_id: Optional[str] = None


class PlanGroupUpdateRequest(BaseModel):
    """Body for ``PATCH /v0/admin/billing/plans/groups/{id}``.

    All fields optional — only the keys present are applied. Pass
    ``display_name`` / ``description`` as ``""`` to clear (the DAO
    coerces empty strings to NULL so the column stays in its
    documented "unset" shape).
    """

    display_name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


class PlanGroupMemberItem(BaseModel):
    """One member row in a group, for admin listings."""

    template_id: int
    template_name: str
    template_display_name: str
    is_active: bool  # template's is_active (not the group's)
    position: Optional[int] = None
    added_at: Optional[str] = None  # ISO-8601 UTC


class PlanGroupResponse(BaseModel):
    """Full group payload returned by the admin endpoints."""

    id: int
    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    is_active: bool
    created_at: str  # ISO-8601 UTC
    created_by_user_id: Optional[str] = None
    members: list[PlanGroupMemberItem] = []


class PlanGroupListResponse(BaseModel):
    """Listing wrapper for ``GET /v0/admin/billing/plans/groups``.

    Returns groups in name order. Members are NOT inlined (would
    blow up payload size for catalogs with many groups); the
    ``member_count`` summary is enough for the admin list view, and
    the detail endpoint returns the full member list.
    """

    groups: list["PlanGroupSummaryItem"]


class PlanGroupSummaryItem(BaseModel):
    """Compact row for the group-listing endpoint."""

    id: int
    name: str
    display_name: Optional[str] = None
    is_active: bool
    member_count: int


PlanGroupListResponse.model_rebuild()


class PlanGroupAddMemberRequest(BaseModel):
    """Body for ``POST /v0/admin/billing/plans/groups/{id}/members``.

    ``position`` is optional: NULL = unordered alternative (cards UX),
    integer = ladder rung. Positions must be unique within the group
    when set — the DAO converts the resulting DB collision into a
    readable 409.
    """

    template_id: int
    position: Optional[int] = None


class PlanGroupSetPositionsRequest(BaseModel):
    """Body for ``PUT /v0/admin/billing/plans/groups/{id}/positions``.

    Atomically rewrite positions for a subset of members. Members not
    listed retain their current position. The DAO does a clear-then-set
    pass so intermediate values never violate the partial unique
    index — admins can swap two rungs in a single round-trip.
    """

    positions: list["PlanGroupSetPositionEntry"]


class PlanGroupSetPositionEntry(BaseModel):
    """One ``(template_id, position)`` pair in a re-ordering request."""

    template_id: int
    position: Optional[int] = None  # NULL clears the rung (back to unordered)


PlanGroupSetPositionsRequest.model_rebuild()


class AssignPlanGroupRequest(BaseModel):
    """Body for ``PUT /v0/admin/billing/accounts/plan-group``.

    ``group_id`` is required (non-null) — every account must be on
    some plan group at all times (see ``DEFAULT_PLAN_GROUP_ID``).
    To revert an account to the platform default, pass
    ``group_id=1`` explicitly. Setting a group does NOT change the
    active plan — that still requires a separate ``set_plan`` call.
    Useful for staging: "give this customer the catalog this week,
    switch them onto the starter plan next week".
    """

    group_id: int


class AssignPlanGroupResponse(BaseModel):
    """Response from the assign-group endpoint."""

    billing_account_id: int
    plan_group_id: int
    plan_group_name: Optional[str] = None


# NOTE: There are no FX-rate admin schemas. Multi-currency is driven by
# per-template ``FxPolicy``: templates carry ``fx_policy`` and
# ``fx_locked_rate`` directly in their create/read schemas above; the
# metered invoicer resolves rates at invoice time (Frankfurter for
# SPOT/PERIOD_AVERAGE) and pins them in ``Recharge.detail`` for re-run
# determinism.


# ---------------------------------------------------------------------------
# Admin invoice list
# ---------------------------------------------------------------------------
# Cross-account invoice surface for the admin console. Unlike the
# customer-scoped ``GET /v0/billing/invoices`` endpoint which lives in
# ``billing.views`` and is keyed off the caller's API-key billing
# account, this surface is unscoped: it fans out across every
# ``billing_account`` row, joins recipient identity, and synthesises an
# "upcoming" projection per active METERED assignment so operators can
# see end-of-month exposure without hand-running the invoicer.
#
# Currency is *not* converted: each row carries the literal contract
# currency (``USD`` / ``GBP`` / ``EUR`` / ...) so a totals row would be
# misleading. The console renders amounts with the currency code; if a
# common-denominator total is ever needed it should sit on a separate
# admin-only endpoint that prices the FX risk explicitly rather than
# blurring it inline.


class AdminInvoiceListItem(BaseModel):
    """One invoice row in the admin invoice list.

    Two flavours, discriminated by ``kind``:

    * ``HISTORICAL`` — backed by a real :class:`Recharge` row that has
      a non-NULL ``stripe_invoice_id``. ``id`` is the recharge id;
      ``status`` follows :class:`RechargeStatus` (``INVOICE_CREATED``
      / ``PAID`` / ``FAILED`` / ``DISPUTED`` — also ``PENDING_INVOICE``
      in the rare transitional case where Stripe assigned an id before
      the orchestra-side status flipped); ``at`` is the recharge
      timestamp (the ``invoice_group`` end-of-period date is exposed
      separately for grouping). Wallet-only admin recharges
      (``payment``/``promo``) and stub PENDING rows with no Stripe
      artefact are filtered out at the SQL layer — this surface is
      strictly "rows with a corresponding Stripe invoice".
    * ``UPCOMING`` — synthesised from
      :func:`monthly_metered_invoicer.estimate_in_progress_invoice`
      for an active METERED assignment that hasn't been invoiced for
      the current period yet. ``id`` is ``None`` (no recharge row
      exists), ``status`` is the literal ``"UPCOMING"``, and ``at`` is
      the projected month-end invoice date. ``amount`` is the
      mid-period projection; ``stripe_invoice_id`` is always ``None``
      (the projection becomes a real Stripe invoice at period close).

    Amount semantics
    ----------------
    ``amount`` is **the full invoice face value**, in the contract
    currency named by ``currency``. For COMMITMENT METERED rows that's
    ``commit_charge_local + overage_charge_local - grants_local`` (the
    invoicer's ``invoiced_local``); for PAYG METERED rows it's
    ``raw_usage_local * base_pricing_factor``. Equivalent to what the
    customer is actually billed for the period — the historical path
    reads it from ``Recharge.detail.invoiced_local`` (METERED) and
    falls back to ``Recharge.amount_usd`` labelled ``USD`` for every
    other Stripe-backed row type (auto-recharge, CREDITS commit fee,
    …). UPCOMING rows take it from the in-progress estimate, which
    uses the same decomposition.

    Customer-facing identity (``recipient_*``) is the org name when the
    BA is org-owned, else the user email — chosen at the SQL layer so
    the FE can render a single "Recipient" column without re-resolving.
    """

    kind: str  # 'HISTORICAL' | 'UPCOMING'
    id: Optional[int] = None  # recharge.id for HISTORICAL, None for UPCOMING
    billing_account_id: int
    recipient_kind: str  # 'USER' | 'ORG'
    recipient_id: str  # user.id or str(organization.id)
    recipient_name: Optional[str] = None  # org.name OR user.name
    recipient_email: Optional[str] = None  # billing_email (BA) or user.email fallback
    at: str  # ISO-8601 — invoice timestamp (HISTORICAL) or projected month-end (UPCOMING)
    invoice_group: Optional[str] = None  # ISO date — period-end label (YYYY-MM-DD)
    type: Optional[str] = None  # recharge.type for HISTORICAL, None for UPCOMING
    status: str  # RechargeStatus value or 'UPCOMING'
    amount: Money
    currency: str  # Plan/template currency (3-letter ISO); falls back to 'USD'
    stripe_invoice_id: Optional[str] = None
    plan_assignment_id: Optional[int] = None
    plan_template_id: Optional[int] = None
    plan_template_name: Optional[str] = None
    plan_template_display_name: Optional[str] = None
    billing_mode: Optional[str] = None  # 'CREDITS' | 'METERED' | None for legacy rows


class AdminInvoiceListResponse(BaseModel):
    """Paginated admin invoice list."""

    invoices: list[AdminInvoiceListItem]
    limit: int
    offset: int
    total: int  # Total HISTORICAL count for the active filters (UPCOMING is fixed-size and not paginated separately)
    upcoming_count: int  # Number of UPCOMING rows merged into ``invoices`` (subset of the page when offset==0)
