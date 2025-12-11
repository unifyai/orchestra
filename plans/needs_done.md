# Backend Implementation Plan: Organization Billing with Stripe

## Executive Summary

This document provides a prescriptive implementation plan for adding organization-level billing to the Unify backend (`orchestra`). The goal is to make organizations first-class billing entities with their own Stripe Customer, credit wallet, and business profile—completely separate from individual user accounts.

---

## Current State Analysis

### How Billing Works Today

| Component | Location | Current Behavior |
|-----------|----------|------------------|
| **User Wallet** | `Users` table | `credits`, `stripe_customer_id`, `autorecharge`, `autorecharge_threshold`, `autorecharge_qty` |
| **Business Profile** | `AuthUser` table | `account_type`, `business_name`, `tax_id`, `business_address_*` fields |
| **Organization Billing** | `Organization` table | Delegates to `billing_user_id` (a user's personal wallet) |
| **Cost Deduction** | `lib/billing.py` | Looks up `billing_user_id` and deducts from that user's credits |
| **Stripe Webhooks** | `webhooks/stripe.py` | Credits `Users` table based on `user_id` in metadata |

### The Problem

1. **Business accounts are tied to individual users** - A user marks themselves as "business" but their Stripe Customer is still personal
2. **Organizations don't have their own Stripe identity** - They borrow a user's wallet via `billing_user_id`
3. **No separation of concerns** - Personal and business billing are conflated
4. **Invoicing limitations** - B2B invoices go to individual users, not legal business entities

---

## Target State

### Organizations Become First-Class Billing Entities

```
┌─────────────────────────────────────────────────────────────────┐
│                        ORGANIZATION                              │
├─────────────────────────────────────────────────────────────────┤
│ Wallet:     credits, stripe_customer_id, autorecharge, etc.     │
│ Profile:    business_name, tax_id, billing_email, address, etc. │
│ Members:    Users with roles (Owner, Admin, Member, Viewer)     │
│ Stripe:     Own Stripe Customer entity with payment methods     │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ Members work under org context
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                           USER                                   │
├─────────────────────────────────────────────────────────────────┤
│ Wallet:     Personal credits, personal stripe_customer_id       │
│ Profile:    Individual account (no business fields needed)      │
│ Usage:      Can use personal wallet OR org wallet based on ctx  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Implementation Tasks

### Phase 1: Database Schema Changes

#### Task 1.1: Create Migration for Organization Billing Columns

**File**: `orchestra/db/migrations/versions/YYYY-MM-DD-HH-MM_add_organization_billing_fields.py`

**Add to `organization` table**:

```python
# === WALLET FIELDS (mirror Users table) ===
credits = Column(Numeric, nullable=False, default=0, server_default="0")
stripe_customer_id = Column(String, nullable=True)  # NULL = not set up yet
autorecharge = Column(Boolean, nullable=False, default=False, server_default="false")
autorecharge_threshold = Column(Numeric, nullable=False, default=10, server_default="10")
autorecharge_qty = Column(Numeric, nullable=False, default=100, server_default="100")
billing_state = Column(String, nullable=False, default="OK", server_default="'OK'")

# === BUSINESS PROFILE FIELDS (move from AuthUser concept) ===
billing_email = Column(String, nullable=True)  # Invoice recipient email
business_name = Column(String(255), nullable=True)  # Legal entity name
tax_id = Column(String(100), nullable=True)  # VAT/Tax ID
business_type = Column(String(50), nullable=True)  # corporation, llc, etc.

# Address fields
billing_address_line1 = Column(String(255), nullable=True)
billing_address_line2 = Column(String(255), nullable=True)
billing_city = Column(String(100), nullable=True)
billing_state = Column(String(100), nullable=True)  # Note: conflicts with billing_state above
billing_country = Column(String(100), nullable=True)
billing_postal_code = Column(String(20), nullable=True)

# Tax compliance
tax_exempt = Column(Boolean, nullable=False, default=False, server_default="false")
tax_jurisdiction = Column(String(100), nullable=True)

# Status tracking
billing_setup_complete = Column(Boolean, nullable=False, default=False, server_default="false")
```

**Indexes to add**:
```python
Index("idx_organization_stripe_customer_id", "stripe_customer_id")
Index("idx_organization_billing_state", "billing_state")
```

**Notes**:
- Rename the status column to `account_status` to avoid conflict with `billing_state` (state as in US state)
- The `billing_user_id` column remains for backward compatibility (fallback)

#### Task 1.2: Create Migration for Recharge Table Updates

**File**: Same migration or separate file

**Modify `recharge` table**:

```python
# Make user_id nullable (NULL = org recharge)
op.alter_column("recharge", "user_id", nullable=True)

# Add organization_id column
op.add_column(
    "recharge",
    Column(
        "organization_id",
        Integer,
        ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=True,
    ),
)

# Add check constraint: exactly one of user_id or organization_id must be set
op.execute("""
    ALTER TABLE recharge
    ADD CONSTRAINT ck_recharge_entity
    CHECK (
        (user_id IS NOT NULL AND organization_id IS NULL) OR
        (user_id IS NULL AND organization_id IS NOT NULL)
    )
""")

# Add index for organization lookups
op.create_index("idx_recharge_organization_id", "recharge", ["organization_id"])
```

---

### Phase 2: Update Database Models

#### Task 2.1: Update Organization Model

**File**: `orchestra/db/models/orchestra_models.py`

```python
class Organization(Base):
    __tablename__ = "organization"

    id = Column(Integer, primary_key=True)
    owner_id = Column(String, ForeignKey("auth_user.id", ondelete="CASCADE"), nullable=False)
    name = Column(String, unique=True, nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now())
    
    # === LEGACY FIELD (for backward compatibility) ===
    billing_user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    # NOTE: Make nullable - NULL means org uses its own wallet
    
    # === NEW WALLET FIELDS ===
    credits = Column(Numeric, nullable=False, default=0, server_default="0")
    stripe_customer_id = Column(String, nullable=True)
    autorecharge = Column(Boolean, nullable=False, default=False, server_default="false")
    autorecharge_threshold = Column(Numeric, nullable=False, default=10, server_default="10")
    autorecharge_qty = Column(Numeric, nullable=False, default=100, server_default="100")
    account_status = Column(String, nullable=False, default="ACTIVE", server_default="'ACTIVE'")
    # Values: ACTIVE, SUSPENDED, PAST_DUE, CLOSED
    
    # === NEW BUSINESS PROFILE FIELDS ===
    billing_email = Column(String, nullable=True)
    business_name = Column(String(255), nullable=True)
    tax_id = Column(String(100), nullable=True)
    business_type = Column(String(50), nullable=True)
    billing_address_line1 = Column(String(255), nullable=True)
    billing_address_line2 = Column(String(255), nullable=True)
    billing_city = Column(String(100), nullable=True)
    billing_state_province = Column(String(100), nullable=True)  # Renamed to avoid conflict
    billing_country = Column(String(100), nullable=True)
    billing_postal_code = Column(String(20), nullable=True)
    tax_exempt = Column(Boolean, nullable=False, default=False, server_default="false")
    tax_jurisdiction = Column(String(100), nullable=True)
    billing_setup_complete = Column(Boolean, nullable=False, default=False, server_default="false")
    
    # Relationships
    recharges = relationship(
        "Recharge",
        back_populates="organization",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
```

#### Task 2.2: Update Recharge Model

**File**: `orchestra/db/models/orchestra_models.py`

```python
class Recharge(Base):
    __tablename__ = "recharge"
    
    # ... existing fields ...
    
    # Make user_id nullable
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    
    # Add organization_id
    organization_id = Column(Integer, ForeignKey("organization.id", ondelete="CASCADE"), nullable=True)
    
    # Add relationship
    organization = relationship("Organization", back_populates="recharges")
```

---

### Phase 3: Create Organization Billing DAO

#### Task 3.1: Create New DAO File

**File**: `orchestra/db/dao/organization_billing_dao.py`

```python
class OrganizationBillingDAO:
    """DAO for organization billing operations."""
    
    def __init__(self, session: Session):
        self.session = session
    
    def get_billing_details(self, organization_id: int) -> Optional[dict]:
        """Get organization billing details."""
        pass
    
    def update_stripe_customer_id(self, organization_id: int, stripe_customer_id: str) -> None:
        """Set the Stripe Customer ID for an organization."""
        pass
    
    def add_credits(self, organization_id: int, amount: Decimal, transaction_id: str) -> None:
        """Add credits to organization wallet."""
        pass
    
    def deduct_credits(self, organization_id: int, amount: Decimal) -> bool:
        """Deduct credits from organization. Returns False if insufficient."""
        pass
    
    def get_credits(self, organization_id: int) -> Decimal:
        """Get current credit balance."""
        pass
    
    def update_autorecharge_settings(
        self,
        organization_id: int,
        enabled: bool,
        threshold: Decimal,
        amount: Decimal,
    ) -> None:
        """Update auto-recharge settings."""
        pass
    
    def update_business_profile(
        self,
        organization_id: int,
        billing_email: Optional[str] = None,
        business_name: Optional[str] = None,
        tax_id: Optional[str] = None,
        # ... other fields
    ) -> None:
        """Update business profile for invoicing."""
        pass
    
    def set_account_status(self, organization_id: int, status: str) -> None:
        """Set account status (ACTIVE, SUSPENDED, PAST_DUE, CLOSED)."""
        pass
    
    def has_own_billing(self, organization_id: int) -> bool:
        """Check if org has set up its own billing (stripe_customer_id is not NULL)."""
        pass
    
    def create_recharge(
        self,
        organization_id: int,
        amount: Decimal,
        recharge_type: str,
        transaction_id: str,
    ) -> Recharge:
        """Create a recharge record for the organization."""
        pass
```

---

### Phase 4: Update Billing Logic

#### Task 4.1: Update `lib/billing.py`

**File**: `orchestra/lib/billing.py`

**Current function**:
```python
def get_billing_user_id(session, organization_id: int) -> str:
    # Returns the user_id to bill
```

**Replace with**:
```python
from dataclasses import dataclass
from typing import Literal

@dataclass
class BillingEntity:
    """Represents the entity to bill (either a user or organization)."""
    entity_type: Literal["user", "organization"]
    entity_id: str | int
    credits: Decimal
    stripe_customer_id: Optional[str]
    autorecharge: bool
    autorecharge_threshold: Decimal
    autorecharge_qty: Decimal

def get_billing_entity(session, organization_id: Optional[int], user_id: str) -> BillingEntity:
    """
    Determine which entity to bill based on context.
    
    Priority:
    1. If organization_id is provided AND org has its own billing → bill org
    2. If organization_id is provided but org uses delegated billing → bill billing_user_id
    3. If no organization_id → bill the user directly
    """
    if organization_id:
        org_dao = OrganizationDAO(session)
        org = org_dao.get(organization_id)
        
        if org and org.stripe_customer_id:
            # Org has its own billing
            return BillingEntity(
                entity_type="organization",
                entity_id=org.id,
                credits=org.credits,
                stripe_customer_id=org.stripe_customer_id,
                autorecharge=org.autorecharge,
                autorecharge_threshold=org.autorecharge_threshold,
                autorecharge_qty=org.autorecharge_qty,
            )
        elif org and org.billing_user_id:
            # Legacy: Delegated billing to a user
            user = get_user_by_id(session, org.billing_user_id)
            return BillingEntity(
                entity_type="user",
                entity_id=user.id,
                credits=user.credits,
                stripe_customer_id=user.stripe_customer_id,
                autorecharge=user.autorecharge,
                autorecharge_threshold=user.autorecharge_threshold,
                autorecharge_qty=user.autorecharge_qty,
            )
    
    # Personal billing
    user = get_user_by_id(session, user_id)
    return BillingEntity(
        entity_type="user",
        entity_id=user.id,
        credits=user.credits,
        stripe_customer_id=user.stripe_customer_id,
        autorecharge=user.autorecharge,
        autorecharge_threshold=user.autorecharge_threshold,
        autorecharge_qty=user.autorecharge_qty,
    )


def deduct_credits(session, billing_entity: BillingEntity, amount: Decimal) -> bool:
    """
    Deduct credits from the appropriate entity.
    Returns True if successful, False if insufficient credits.
    """
    if billing_entity.entity_type == "organization":
        org_billing_dao = OrganizationBillingDAO(session)
        return org_billing_dao.deduct_credits(billing_entity.entity_id, amount)
    else:
        user_dao = UserDAO(session)
        return user_dao.deduct_credits(billing_entity.entity_id, amount)


def add_credits(session, billing_entity: BillingEntity, amount: Decimal, transaction_id: str) -> None:
    """Add credits to the appropriate entity."""
    if billing_entity.entity_type == "organization":
        org_billing_dao = OrganizationBillingDAO(session)
        org_billing_dao.add_credits(billing_entity.entity_id, amount, transaction_id)
    else:
        user_dao = UserDAO(session)
        user_dao.add_credits(billing_entity.entity_id, amount, transaction_id)
```

#### Task 4.2: Update Background Tasks

**File**: `orchestra/web/api/utils/bg_tasks.py`

Update the auto-recharge logic to work with `BillingEntity`:

```python
async def trigger_autorecharge(billing_entity: BillingEntity):
    """Trigger auto-recharge for a user or organization."""
    if billing_entity.entity_type == "organization":
        # Use org-specific Stripe checkout
        await create_org_autorecharge_invoice(
            organization_id=billing_entity.entity_id,
            amount=billing_entity.autorecharge_qty,
        )
    else:
        # Existing user auto-recharge logic
        await create_user_autorecharge_invoice(
            user_id=billing_entity.entity_id,
            amount=billing_entity.autorecharge_qty,
        )
```

---

### Phase 5: Update Stripe Webhook Handler

#### Task 5.1: Update Webhook to Handle Organization Payments

**File**: `orchestra/web/api/webhooks/stripe.py`

**Current behavior**: Extracts `user_id` from session metadata and credits that user.

**Required changes**:

```python
@router.post("/stripe/webhook")
async def stripe_webhook(request: Request, session: Session = Depends(get_db_session)):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    
    # ... signature verification ...
    
    if event["type"] == "checkout.session.completed":
        checkout_session = event["data"]["object"]
        metadata = checkout_session.get("metadata", {})
        
        # NEW: Check for organization_id first
        organization_id = metadata.get("organization_id")
        user_id = metadata.get("user_id")
        amount = checkout_session.get("amount_total", 0) / 100  # Convert from cents
        transaction_id = checkout_session.get("payment_intent")
        
        if organization_id:
            # Credit the organization
            org_billing_dao = OrganizationBillingDAO(session)
            org_billing_dao.add_credits(
                organization_id=int(organization_id),
                amount=Decimal(str(amount)),
                transaction_id=transaction_id,
            )
            org_billing_dao.create_recharge(
                organization_id=int(organization_id),
                amount=Decimal(str(amount)),
                recharge_type="payment",
                transaction_id=transaction_id,
            )
        elif user_id:
            # Credit the user (existing logic)
            user_dao = UserDAO(session)
            user_dao.add_credits(user_id, Decimal(str(amount)), transaction_id)
            # ... create recharge record ...
        
        session.commit()
    
    # Handle invoice.payment_failed for organization auto-recharge failures
    elif event["type"] == "invoice.payment_failed":
        invoice = event["data"]["object"]
        metadata = invoice.get("metadata", {})
        
        organization_id = metadata.get("organization_id")
        if organization_id:
            # Set org status to PAST_DUE
            org_billing_dao = OrganizationBillingDAO(session)
            org_billing_dao.set_account_status(int(organization_id), "PAST_DUE")
            session.commit()
    
    return {"status": "success"}
```

---

### Phase 6: Create Organization Billing API Endpoints

#### Task 6.1: Create New Router

**File**: `orchestra/web/api/organization_billing/views.py`

```python
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

router = APIRouter()

# === BILLING DETAILS ===

@router.get("/organizations/{organization_id}/billing")
async def get_organization_billing(
    request: Request,
    organization_id: int,
    session: Session = Depends(get_db_session),
):
    """
    Get organization billing details.
    Requires: org:read permission
    """
    # Check permission
    # Return: credits, autorecharge settings, billing profile, account status
    pass


# === STRIPE SETUP ===

@router.post("/organizations/{organization_id}/billing/setup")
async def setup_organization_billing(
    request: Request,
    organization_id: int,
    body: OrganizationBillingSetupRequest,
    session: Session = Depends(get_db_session),
):
    """
    Set up Stripe Customer for organization.
    Creates a new Stripe Customer and stores the ID.
    Requires: org:write permission (Owner/Admin only)
    
    Request body:
    - billing_email: Email for invoices
    - business_name: Legal entity name
    - tax_id: Optional VAT/Tax ID
    - billing_address: Address object
    """
    # 1. Verify user has org:write permission
    # 2. Create Stripe Customer with business metadata
    # 3. Store stripe_customer_id on organization
    # 4. Update business profile fields
    # 5. Set billing_setup_complete = True
    pass


@router.get("/organizations/{organization_id}/billing/portal")
async def get_billing_portal_url(
    request: Request,
    organization_id: int,
    session: Session = Depends(get_db_session),
):
    """
    Get Stripe Billing Portal URL for the organization.
    Allows admins to manage payment methods, view invoices, etc.
    Requires: org:write permission
    """
    # 1. Verify org has stripe_customer_id
    # 2. Create Stripe Billing Portal session
    # 3. Return portal URL
    pass


# === CHECKOUT / ADD CREDITS ===

@router.post("/organizations/{organization_id}/billing/checkout")
async def create_checkout_session(
    request: Request,
    organization_id: int,
    body: CheckoutSessionRequest,
    session: Session = Depends(get_db_session),
):
    """
    Create Stripe Checkout Session to add credits to organization.
    Requires: org:write permission
    
    Request body:
    - amount: Credit amount to purchase
    - success_url: Redirect URL on success
    - cancel_url: Redirect URL on cancel
    """
    # 1. Verify org has stripe_customer_id (or create one)
    # 2. Create Stripe Checkout Session with organization_id in metadata
    # 3. Return checkout URL
    pass


# === AUTO-RECHARGE SETTINGS ===

@router.get("/organizations/{organization_id}/billing/auto-recharge")
async def get_autorecharge_settings(
    request: Request,
    organization_id: int,
    session: Session = Depends(get_db_session),
):
    """Get auto-recharge settings. Requires: org:read permission"""
    pass


@router.patch("/organizations/{organization_id}/billing/auto-recharge")
async def update_autorecharge_settings(
    request: Request,
    organization_id: int,
    body: AutoRechargeSettingsRequest,
    session: Session = Depends(get_db_session),
):
    """
    Update auto-recharge settings.
    Requires: org:write permission
    
    Request body:
    - enabled: bool
    - threshold: Decimal (trigger when credits fall below)
    - amount: Decimal (amount to recharge)
    """
    # 1. Verify org has stripe_customer_id and payment method
    # 2. Update settings
    pass


# === BUSINESS PROFILE ===

@router.get("/organizations/{organization_id}/billing/profile")
async def get_billing_profile(
    request: Request,
    organization_id: int,
    session: Session = Depends(get_db_session),
):
    """Get business profile for invoicing. Requires: org:read permission"""
    pass


@router.patch("/organizations/{organization_id}/billing/profile")
async def update_billing_profile(
    request: Request,
    organization_id: int,
    body: BillingProfileUpdateRequest,
    session: Session = Depends(get_db_session),
):
    """
    Update business profile.
    Requires: org:write permission
    
    Also updates corresponding Stripe Customer metadata.
    """
    pass


# === RECHARGE HISTORY ===

@router.get("/organizations/{organization_id}/billing/recharges")
async def list_recharges(
    request: Request,
    organization_id: int,
    limit: int = 50,
    offset: int = 0,
    session: Session = Depends(get_db_session),
):
    """List recharge/payment history. Requires: org:read permission"""
    pass
```

#### Task 6.2: Create Request/Response Schemas

**File**: `orchestra/web/api/organization_billing/schema.py`

```python
from pydantic import BaseModel, EmailStr
from typing import Optional
from decimal import Decimal

class BillingAddress(BaseModel):
    line1: str
    line2: Optional[str] = None
    city: str
    state_province: Optional[str] = None
    country: str
    postal_code: Optional[str] = None

class OrganizationBillingSetupRequest(BaseModel):
    billing_email: EmailStr
    business_name: str
    tax_id: Optional[str] = None
    business_type: Optional[str] = None
    billing_address: BillingAddress

class CheckoutSessionRequest(BaseModel):
    amount: Decimal
    success_url: str
    cancel_url: str

class AutoRechargeSettingsRequest(BaseModel):
    enabled: Optional[bool] = None
    threshold: Optional[Decimal] = None
    amount: Optional[Decimal] = None

class BillingProfileUpdateRequest(BaseModel):
    billing_email: Optional[EmailStr] = None
    business_name: Optional[str] = None
    tax_id: Optional[str] = None
    business_type: Optional[str] = None
    billing_address: Optional[BillingAddress] = None
    tax_exempt: Optional[bool] = None

class OrganizationBillingResponse(BaseModel):
    credits: Decimal
    stripe_customer_id: Optional[str]
    billing_setup_complete: bool
    account_status: str
    autorecharge: bool
    autorecharge_threshold: Decimal
    autorecharge_qty: Decimal
    billing_email: Optional[str]
    business_name: Optional[str]
    tax_id: Optional[str]

class CheckoutSessionResponse(BaseModel):
    checkout_url: str
    session_id: str

class BillingPortalResponse(BaseModel):
    portal_url: str
```

#### Task 6.3: Register Router

**File**: `orchestra/web/api/router.py`

```python
from orchestra.web.api import organization_billing

# Add to api_router
api_router.include_router(
    organization_billing.router,
    tags=["Organization Billing"],
    dependencies=API_KEY_AUTH,
)
```

---

### Phase 7: Update Cost Deduction Points

#### Task 7.1: Identify All Cost Deduction Points

Search for all places where credits are deducted:

```bash
grep -r "deduct" orchestra/orchestra/lib/
grep -r "credits" orchestra/orchestra/web/api/
grep -r "get_billing_user_id" orchestra/
```

**Known locations**:
1. `lib/billing.py` - Main billing logic
2. `web/api/utils/bg_tasks.py` - Background cost processing
3. Query processing endpoints

#### Task 7.2: Update Each Location

For each location, replace:
```python
# OLD
billing_user_id = get_billing_user_id(session, organization_id)
user = get_user(session, billing_user_id)
user.credits -= cost
```

With:
```python
# NEW
billing_entity = get_billing_entity(session, organization_id, user_id)
success = deduct_credits(session, billing_entity, cost)
if not success:
    # Handle insufficient credits
```

---

## Migration Strategy

### Backward Compatibility

The implementation maintains full backward compatibility:

1. **Existing organizations** continue to use `billing_user_id` (delegated billing)
2. **New organizations** can choose to set up their own billing
3. **Migration path**: Existing orgs can "upgrade" to own billing by setting up Stripe

### Rollout Plan

| Phase | Description | Risk |
|-------|-------------|------|
| 1 | Deploy schema changes with feature flag | Low |
| 2 | Enable org billing setup for new orgs only | Low |
| 3 | Allow existing orgs to migrate to own billing | Medium |
| 4 | Deprecate `billing_user_id` (future) | High |

---

## Testing Requirements

### Unit Tests

1. `test_organization_billing_dao.py`
   - Test credit operations (add, deduct, get)
   - Test autorecharge settings
   - Test business profile updates

2. `test_billing_entity.py`
   - Test `get_billing_entity()` with various scenarios
   - Test org with own billing vs delegated billing
   - Test personal billing

### Integration Tests

1. `test_organization_billing_endpoints.py`
   - Test all new API endpoints
   - Test permission checks (org:read, org:write)

2. `test_stripe_webhook_org.py`
   - Test checkout.session.completed with organization_id
   - Test invoice.payment_failed for org

### End-to-End Tests

1. Complete flow: Create org → Setup billing → Add credits → Use credits
2. Auto-recharge trigger and processing
3. Stripe portal access

---

## Files to Create/Modify Summary

### New Files

| File | Purpose |
|------|---------|
| `db/migrations/versions/YYYY-MM-DD_add_organization_billing_fields.py` | Schema migration |
| `db/dao/organization_billing_dao.py` | Billing DAO |
| `web/api/organization_billing/__init__.py` | Router init |
| `web/api/organization_billing/views.py` | API endpoints |
| `web/api/organization_billing/schema.py` | Pydantic schemas |

### Modified Files

| File | Changes |
|------|---------|
| `db/models/orchestra_models.py` | Add columns to Organization, update Recharge |
| `lib/billing.py` | Add `BillingEntity`, `get_billing_entity()`, update deduction logic |
| `web/api/webhooks/stripe.py` | Handle organization_id in metadata |
| `web/api/utils/bg_tasks.py` | Update auto-recharge for orgs |
| `web/api/router.py` | Register new router |

---

## ✅ CRITICAL BUGS FIXED

### 1. Monthly Invoicer Doesn't Handle Organization Recharges ~~(HIGH PRIORITY)~~ **FIXED**

**File:** `orchestra/routines/monthly_invoicer.py`

**Problem:** The monthly invoicer only groups recharges by `user_id`. Organization recharges have `user_id=NULL` and `organization_id` set, so they are never invoiced.

**Impact:** Organizations with direct billing can use credits via auto-recharge but will NEVER be invoiced at month end. Credits used without payment!

**Fix Implemented (2025-12-10):**
1. ✅ Separated user recharges from org recharges
2. ✅ Group org recharges by `organization_id`
3. ✅ Create invoices on org's Stripe customer account
4. ✅ Apply org's tax settings via `_get_tax_id_type_for_country()` helper
5. ✅ 5 new tests added to verify org invoicing

**See:** `plans/devlog.md` entry for 2025-12-10 for full details.

### 2. Other Critical Review Fixes **FIXED**

All items from the critical review have been addressed:

| Issue | Status | Fix |
|-------|--------|-----|
| Missing index on `Organization.stripe_customer_id` | ✅ | Added `index=True` in model, included in migration |
| Negative credit balance allowed | ✅ | Warning logged in `deduct_credits` |
| Prometheus label pollution | ✅ | Changed to `entity_type`/`entity_id` labels |
| No minimum autorecharge guard for orgs | ✅ | $25 minimum enforced in DAO |
| Missing billing address validation | ✅ | `country` required in `BillingAddress` schema |
| Multiple migration files | ✅ | Squashed to single `04602c1e5141` migration |
| Race condition in auto-recharge | ✅ | Idempotency check added in `bg_tasks.py` |
| Tax ID not validated | ✅ | Integrated `TaxIDValidator` in business profile endpoint |

---

## ✅ HIGH PRIORITY: Fixed (2025-12-11)

### 1. Frozen Org Billing Block ✅ FIXED
- **Fix:** Added `account_status` check in `get_billing_entity()`, raises ValueError if not ACTIVE

### 2. Webhook Sets `stripe_customer_id` ✅ FIXED
- **Fix:** Checkout webhook now sets `stripe_customer_id` from session for new orgs

### 3. Unique Constraint on `stripe_customer_id` ✅ FIXED
- **Fix:** Added `unique=True` to column in model

### 4. XOR Constraint in Migration ✅ FIXED
- **Fix:** New migration `2025-12-11-12-11_6747520f3e1b.py` includes both constraints

---

## ✅ MEDIUM PRIORITY: Fixed (2025-12-11)

| Issue | Status | Fix Applied |
|-------|--------|-------------|
| `account_status` not validated | ✅ | Added CheckConstraint in model |
| `set_account_status` accepts any string | ✅ | Validates against VALID_ACCOUNT_STATUSES |
| Race condition uses `skip_locked` | ✅ | Changed to `nowait=True` with OperationalError handling |
| No idempotency for auto-recharges | ✅ | Checks for existing PENDING_INVOICE before queueing |

## ⏳ LOW PRIORITY: Pending

| Issue | Location | Fix |
|-------|----------|-----|
| Duplicate queries in billing flow | `lib/billing.py` | Pass entity object |
| Commits inside loop | `bg_tasks.py` | Move to end |
| No Stripe retry logic | `monthly_invoicer.py` | Add exponential backoff |
| Hardcoded tax ID mapping | `monthly_invoicer.py` | Reuse TaxIDValidator |
| No rate limiting on billing | `organization/views.py` | Add middleware |
| No audit logging | `organization/views.py` | Add audit table |
| Cache not thread-safe | `tax_id_validator.py` | Add threading.Lock |

---

## Open Questions

1. **Should we allow switching from org billing back to delegated billing?**
   - Recommendation: No, this creates accounting complexity

2. **What happens to existing recharge history when org sets up own billing?**
   - Recommendation: Keep separate - old recharges stay with user, new ones go to org

3. **Should org billing require a payment method before enabling auto-recharge?**
   - Recommendation: Yes, via Stripe Billing Portal

4. **How do we handle tax ID validation for different countries?**
   - Recommendation: Reuse existing tax ID validation from AuthUser flow

