# Organization Billing Architecture & Implementation Plan
# Version 2.0 - Comprehensive Prescriptive Guide

## 1. Executive Summary

This document serves as the comprehensive architectural blueprint for implementing Organization-level billing in Unify. It covers the full lifecycle of an organization—from signup and member management to billing settings and closure—ensuring financial integrity and security at every step.

**Core Philosophy:** An Organization is a first-class financial entity ("Wallet") distinct from its users. While users *administer* the organization, the organization *owns* the funds, the payment methods, and the data.

---

## 2. Current State Analysis

### 2.1 How Billing Works Today

1.  **API Key Authentication** (`orchestra/web/api/dependencies.py`):
    ```python
    # Line 52-65: auth_api_key extracts organization_id from ApiKey
    request_fastapi.state.organization_id = db_response[0][4]
    ```
2.  **Billing Routing** (`orchestra/lib/billing.py`):
    ```python
    # Line 127-165: get_billing_user_id
    if organization_id is None:
        return user_id  # Personal billing
    return org.billing_user_id  # Org billing (but bills a USER's wallet)
    ```
3.  **Credit Deduction** (`orchestra/web/api/utils/bg_tasks.py`):
    ```python
    # Line 183-190: Credits deducted from billing_user's wallet
    billing_user_id = get_billing_user_id(session, user_id, organization_id)
    users_dao.recharge_credit(billing_user_id, -cost)
    ```

**Problem**: Organization billing currently charges a *personal user's wallet* (via `billing_user_id`), not an organization-owned wallet.

### 2.2 Current Frontend Structure

| Route | Component | Purpose |
|-------|-----------|---------|
| `/onboarding` | `OnboardingWorkflow.tsx` | User profile + Tax setup |
| `/profile` | `Profile/Main.tsx` | API Key + Account Info |
| `/billing` | `Billing/Main.tsx` | Balance + Tax + Auto-refill |
| `/keys` | `Keys/` | API Key management |

**Gap**: No Organization-specific pages for billing, API keys, or settings.

---

## 3. Data Model Changes

### 3.1 Organization Table (`orchestra/db/models/orchestra_models.py`)

**File**: `orchestra/db/models/orchestra_models.py`
**Location**: Line ~591 (`class Organization`)

```python
class Organization(Base):
    __tablename__ = "organization"

    # --- Existing Fields ---
    id = Column(Integer, primary_key=True)
    owner_id = Column(String, ForeignKey("auth_user.id", ondelete="CASCADE"), nullable=False)
    billing_user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False) # DEPRECATED - Keep for migration
    name = Column(String, unique=True, nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now())

    # --- NEW: Billing Wallet ---
    credits = Column(Numeric, nullable=False, default=0, server_default="0")
    stripe_customer_id = Column(String, nullable=True)
    billing_state = Column(String, default="OK", server_default="OK") # OK, PAST_DUE, SUSPENDED

    # --- NEW: Auto-recharge Settings ---
    autorecharge = Column(Boolean, nullable=False, default=False, server_default="false")
    autorecharge_threshold = Column(Numeric, nullable=False, default=0, server_default="0")
    autorecharge_qty = Column(Numeric, nullable=False, default=25, server_default="25")

    # --- NEW: Invoicing Profile ---
    billing_email = Column(String, nullable=True)
    tax_id = Column(String, nullable=True)
    billing_address = Column(JSONB, nullable=True)

    # --- NEW: Lifecycle ---
    is_active = Column(Boolean, default=True, server_default="t")
    archived_at = Column(TIMESTAMP, nullable=True)
```

### 3.2 Recharge Table (`orchestra/db/models/orchestra_models.py`)

**File**: `orchestra/db/models/orchestra_models.py`
**Location**: Line ~219 (`class Recharge`)

```python
class Recharge(Base):
    __tablename__ = "recharge"

    id = Column(Integer(), primary_key=True)
    at = Column(TIMESTAMP, ...)
    user_id = Column(String(), ForeignKey("users.id"), nullable=True)  # CHANGE: Make nullable
    organization_id = Column(Integer, ForeignKey("organization.id"), nullable=True)  # NEW
    quantity = Column(Numeric(), nullable=False)
    amount_usd = Column(Numeric(), nullable=False)
    # ... rest unchanged

    __table_args__ = (
        # NEW: Ensure exactly one owner
        sa.CheckConstraint(
            "(user_id IS NOT NULL AND organization_id IS NULL) OR (user_id IS NULL AND organization_id IS NOT NULL)",
            name="ck_recharge_owner_xor"
        ),
        # ... existing constraints
    )
```

### 3.3 ApiKey Table (No Schema Change Needed)

The `ApiKey` table already has `organization_id`. No changes required.

---

## 4. Backend Implementation

### 4.1 Database Migration

**New File**: `orchestra/db/migrations/versions/YYYY-MM-DD_add_org_billing.py`

```python
"""Add organization billing columns"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'xxxx'
down_revision = 'previous_revision'

def upgrade():
    # 1. Add columns to Organization
    op.add_column('organization', sa.Column('credits', sa.Numeric(), server_default='0', nullable=False))
    op.add_column('organization', sa.Column('stripe_customer_id', sa.String(), nullable=True))
    op.add_column('organization', sa.Column('billing_state', sa.String(), server_default='OK', nullable=False))
    op.add_column('organization', sa.Column('autorecharge', sa.Boolean(), server_default='false', nullable=False))
    op.add_column('organization', sa.Column('autorecharge_threshold', sa.Numeric(), server_default='0', nullable=False))
    op.add_column('organization', sa.Column('autorecharge_qty', sa.Numeric(), server_default='25', nullable=False))
    op.add_column('organization', sa.Column('billing_email', sa.String(), nullable=True))
    op.add_column('organization', sa.Column('tax_id', sa.String(), nullable=True))
    op.add_column('organization', sa.Column('billing_address', postgresql.JSONB(), nullable=True))
    op.add_column('organization', sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False))
    op.add_column('organization', sa.Column('archived_at', sa.TIMESTAMP(), nullable=True))

    # 2. Update Recharge table
    op.add_column('recharge', sa.Column('organization_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_recharge_organization', 'recharge', 'organization', ['organization_id'], ['id'], ondelete='CASCADE')
    
    # Make user_id nullable
    op.alter_column('recharge', 'user_id', existing_type=sa.String(), nullable=True)
    
    # Add XOR constraint
    op.execute("""
        ALTER TABLE recharge ADD CONSTRAINT ck_recharge_owner_xor 
        CHECK ((user_id IS NOT NULL AND organization_id IS NULL) OR (user_id IS NULL AND organization_id IS NOT NULL))
    """)

def downgrade():
    # Remove constraint first
    op.execute("ALTER TABLE recharge DROP CONSTRAINT IF EXISTS ck_recharge_owner_xor")
    op.alter_column('recharge', 'user_id', existing_type=sa.String(), nullable=False)
    op.drop_constraint('fk_recharge_organization', 'recharge', type_='foreignkey')
    op.drop_column('recharge', 'organization_id')
    
    op.drop_column('organization', 'archived_at')
    op.drop_column('organization', 'is_active')
    op.drop_column('organization', 'billing_address')
    op.drop_column('organization', 'tax_id')
    op.drop_column('organization', 'billing_email')
    op.drop_column('organization', 'autorecharge_qty')
    op.drop_column('organization', 'autorecharge_threshold')
    op.drop_column('organization', 'autorecharge')
    op.drop_column('organization', 'billing_state')
    op.drop_column('organization', 'stripe_customer_id')
    op.drop_column('organization', 'credits')
```

### 4.2 Billing Library Refactor

**File**: `orchestra/lib/billing.py`

Replace `get_billing_user_id` with a comprehensive function:

```python
from typing import Optional, Tuple, Literal
from sqlalchemy.orm import Session
from orchestra.db.models.orchestra_models import Organization, Users as User, Recharge, RechargeStatus

BillingTarget = Literal["user", "organization"]

def process_cost_deduction(
    session: Session,
    user_id: str,
    organization_id: Optional[int],
    cost: float
) -> Tuple[BillingTarget, str | int]:
    """
    Deduct cost from the appropriate wallet.
    
    Returns:
        Tuple of (target_type, target_id) for logging purposes.
    """
    if organization_id:
        org = session.query(Organization).get(organization_id)
        if not org:
            raise ValueError(f"Organization {organization_id} not found")
        
        # CASE A: Organization Direct Billing (New Model)
        if org.stripe_customer_id:
            if org.billing_state != "OK":
                raise BillingSuspendedError(f"Organization billing is suspended: {org.billing_state}")
            
            org.credits = float(org.credits) - cost
            
            # Check auto-recharge
            if org.autorecharge and org.credits <= float(org.autorecharge_threshold):
                queue_org_auto_recharge(session, org, int(org.autorecharge_qty))
            
            return ("organization", org.id)
        
        # CASE B: Legacy Delegated Billing (Transition)
        elif org.billing_user_id:
            _deduct_user_credits(session, org.billing_user_id, cost)
            return ("user", org.billing_user_id)
        else:
            raise ValueError(f"Organization {organization_id} has no billing method configured")
    
    # CASE C: Personal Billing
    _deduct_user_credits(session, user_id, cost)
    return ("user", user_id)


def _deduct_user_credits(session: Session, user_id: str, cost: float):
    """Deduct credits from a user's personal wallet."""
    user = session.query(User).get(user_id)
    if not user:
        raise ValueError(f"User {user_id} not found")
    
    user.credits = float(user.credits) - cost
    
    if user.autorecharge and float(user.credits) <= float(user.autorecharge_threshold):
        queue_auto_recharge(session, user, int(user.autorecharge_qty))


def queue_org_auto_recharge(session: Session, org: Organization, credits: int):
    """Queue auto-recharge for an Organization."""
    import stripe
    import os
    from datetime import datetime, timezone
    from decimal import Decimal
    from orchestra.lib.time import month_end_utc
    
    now = datetime.now(timezone.utc)
    invoice_group = month_end_utc(now)
    
    # Create Recharge record for Organization
    recharge = Recharge(
        organization_id=org.id,
        user_id=None,  # Important: NULL for org recharges
        type="auto",
        quantity=Decimal(credits),
        amount_usd=Decimal(credits),
        invoice_group=invoice_group,
        status=RechargeStatus.PENDING_INVOICE,
    )
    session.add(recharge)
    
    # Optimistically credit the org
    org.credits = float(org.credits) + credits
    
    # Create Stripe Invoice Item
    if org.stripe_customer_id:
        stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
        stripe.InvoiceItem.create(
            customer=org.stripe_customer_id,
            amount=int(credits * 100),
            currency="usd",
            description=f"{credits} credits (auto-recharge)",
            metadata={
                "recharge_type": "auto",
                "organization_id": str(org.id),
                "invoice_group": str(invoice_group),
            },
        )
```

### 4.3 Background Tasks Update

**File**: `orchestra/web/api/utils/bg_tasks.py`

**Change**: Replace call to `get_billing_user_id` with `process_cost_deduction`.

```python
# Line ~181-190: Replace this block
# OLD:
# billing_user_id = get_billing_user_id(session, user_id, organization_id)
# users_dao.recharge_credit(billing_user_id, -cost)

# NEW:
from orchestra.lib.billing import process_cost_deduction

if not os.environ.get("ON_PREM") and status_code == 200:
    target_type, target_id = process_cost_deduction(
        session=session,
        user_id=user_id,
        organization_id=organization_id,
        cost=cost
    )
    session.commit()
    logger.info(f"[BILLING] Charged {cost} to {target_type}:{target_id}")
```

### 4.4 Stripe Webhook Handler Update

**File**: `orchestra/web/api/webhooks/stripe.py`

**Change**: Update `process_checkout_session_event` to handle `organization_id`.

```python
def process_checkout_session_event(event: Dict, session: Session) -> Response:
    # ... existing idempotency logic ...

    if event["type"] == "checkout.session.completed":
        if data.get("subscription"):
            session.commit()
            return Response(status_code=200)

        # Extract metadata
        metadata = data.get("metadata", {})
        user_id = data.get("client_reference_id")
        organization_id = metadata.get("organization_id")  # NEW
        amount_total = data.get("amount_total")
        credits = amount_total / 100

        if organization_id:
            # --- ORGANIZATION CREDIT ---
            org_dao = OrganizationDAO(session)
            org = org_dao.get(int(organization_id))
            if not org:
                logger.error(f"Org {organization_id} not found for checkout")
                session.commit()
                return Response(status_code=404)
            
            org.credits = float(org.credits) + credits
            
            # Create Recharge record
            recharge = Recharge(
                organization_id=org.id,
                user_id=None,
                quantity=credits,
                amount_usd=credits,
                type="payment",
                status=RechargeStatus.PAID,
                transaction_id=data.get("payment_intent"),
            )
            session.add(recharge)
            logger.info(f"Credited Org {org.id} with {credits}")
        
        elif user_id:
            # --- USER CREDIT (Existing Logic) ---
            users_dao = UsersDAO(session)
            users_dao.recharge_credit(user_id, credits)
            logger.info(f"Credited User {user_id} with {credits}")
        
        else:
            logger.error("Checkout completed without user_id or organization_id")
            session.commit()
            return Response(status_code=400)

    session.commit()
    return Response(status_code=200)
```

### 4.5 New API Router: Organization Billing

**New File**: `orchestra/web/api/organization/billing.py`

```python
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from decimal import Decimal

from orchestra.db.dependencies import get_db_session
from orchestra.db.dao.organization_dao import OrganizationDAO
from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.db.dao.recharge_dao import RechargeDAO

router = APIRouter()

class BillingOverviewResponse(BaseModel):
    credits: float
    stripe_customer_id: Optional[str]
    billing_state: str
    autorecharge_enabled: bool
    autorecharge_threshold: float
    autorecharge_qty: float
    billing_email: Optional[str]

class BillingSettingsUpdate(BaseModel):
    autorecharge_enabled: Optional[bool] = None
    autorecharge_threshold: Optional[float] = None
    autorecharge_qty: Optional[float] = None
    billing_email: Optional[str] = None
    tax_id: Optional[str] = None
    billing_address: Optional[dict] = None


@router.get("/{organization_id}/billing", response_model=BillingOverviewResponse)
async def get_billing_overview(
    request: Request,
    organization_id: int,
    session: Session = Depends(get_db_session),
):
    """Get billing overview for an organization."""
    user_id = request.state.user_id
    resource_access_dao = ResourceAccessDAO(session)
    
    # Check permission
    if not resource_access_dao.check_user_permission(user_id, "org", organization_id, "org:billing:read"):
        raise HTTPException(status_code=403, detail="Permission denied")
    
    org_dao = OrganizationDAO(session)
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    
    return BillingOverviewResponse(
        credits=float(org.credits),
        stripe_customer_id=org.stripe_customer_id[:10] + "..." if org.stripe_customer_id else None,
        billing_state=org.billing_state,
        autorecharge_enabled=org.autorecharge,
        autorecharge_threshold=float(org.autorecharge_threshold),
        autorecharge_qty=float(org.autorecharge_qty),
        billing_email=org.billing_email,
    )


@router.put("/{organization_id}/billing")
async def update_billing_settings(
    request: Request,
    organization_id: int,
    settings: BillingSettingsUpdate,
    session: Session = Depends(get_db_session),
):
    """Update billing settings for an organization."""
    user_id = request.state.user_id
    resource_access_dao = ResourceAccessDAO(session)
    
    if not resource_access_dao.check_user_permission(user_id, "org", organization_id, "org:billing:write"):
        raise HTTPException(status_code=403, detail="Permission denied")
    
    org_dao = OrganizationDAO(session)
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    
    # Update fields
    if settings.autorecharge_enabled is not None:
        org.autorecharge = settings.autorecharge_enabled
    if settings.autorecharge_threshold is not None:
        org.autorecharge_threshold = Decimal(str(settings.autorecharge_threshold))
    if settings.autorecharge_qty is not None:
        org.autorecharge_qty = Decimal(str(settings.autorecharge_qty))
    if settings.billing_email is not None:
        org.billing_email = settings.billing_email
    if settings.tax_id is not None:
        org.tax_id = settings.tax_id
    if settings.billing_address is not None:
        org.billing_address = settings.billing_address
    
    session.commit()
    return {"message": "Settings updated"}


@router.post("/{organization_id}/billing/checkout-session")
async def create_checkout_session(
    request: Request,
    organization_id: int,
    amount: int,  # in dollars
    session: Session = Depends(get_db_session),
):
    """Create a Stripe Checkout session to add funds."""
    import stripe
    import os
    
    user_id = request.state.user_id
    resource_access_dao = ResourceAccessDAO(session)
    
    if not resource_access_dao.check_user_permission(user_id, "org", organization_id, "org:billing:write"):
        raise HTTPException(status_code=403, detail="Permission denied")
    
    org_dao = OrganizationDAO(session)
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    
    # Ensure Stripe Customer exists
    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
    if not org.stripe_customer_id:
        # Create customer
        customer = stripe.Customer.create(
            name=org.name,
            email=org.billing_email,
            metadata={"organization_id": str(org.id)},
        )
        org.stripe_customer_id = customer.id
        session.commit()
    
    # Create Checkout Session
    checkout_session = stripe.checkout.Session.create(
        mode="payment",
        customer=org.stripe_customer_id,
        line_items=[{
            "price_data": {
                "currency": "usd",
                "product_data": {"name": "Unify Credits"},
                "unit_amount": amount * 100,
            },
            "quantity": 1,
        }],
        metadata={"organization_id": str(organization_id)},
        success_url=f"{os.environ.get('NEXTAUTH_URL')}/org/{organization_id}/billing?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{os.environ.get('NEXTAUTH_URL')}/org/{organization_id}/billing",
    )
    
    return {"checkout_url": checkout_session.url}


@router.post("/{organization_id}/billing/portal-session")
async def create_portal_session(
    request: Request,
    organization_id: int,
    session: Session = Depends(get_db_session),
):
    """Create a Stripe Billing Portal session to manage payment methods."""
    import stripe
    import os
    
    user_id = request.state.user_id
    resource_access_dao = ResourceAccessDAO(session)
    
    if not resource_access_dao.check_user_permission(user_id, "org", organization_id, "org:billing:write"):
        raise HTTPException(status_code=403, detail="Permission denied")
    
    org_dao = OrganizationDAO(session)
    org = org_dao.get(organization_id)
    if not org or not org.stripe_customer_id:
        raise HTTPException(status_code=404, detail="Organization or Stripe customer not found")
    
    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
    portal_session = stripe.billing_portal.Session.create(
        customer=org.stripe_customer_id,
        return_url=f"{os.environ.get('NEXTAUTH_URL')}/org/{organization_id}/billing",
    )
    
    return {"portal_url": portal_session.url}
```

---

## 5. Frontend Implementation

### 5.1 New Routes Required

| Route | Component | Purpose |
|-------|-----------|---------|
| `/org/[id]/settings` | `OrgSettings.tsx` | General Org Settings |
| `/org/[id]/billing` | `OrgBilling.tsx` | Org Billing Page |
| `/org/[id]/members` | `OrgMembers.tsx` | Member Management |
| `/org/[id]/keys` | `OrgApiKeys.tsx` | Org-scoped API Keys |

### 5.2 Organization Billing Page

**New File**: `console/src/app/(home)/org/[id]/billing/page.tsx`

```tsx
import { Suspense } from "react";
import OrgBillingMain from "@/components/Pages/OrgBilling/Main";
import SkeletonLoader from "@/components/Common/Loaders/SkeletonLoader";

interface Props {
  params: { id: string };
}

export default function OrgBillingPage({ params }: Props) {
  return (
    <Suspense fallback={<SkeletonLoader />}>
      <OrgBillingMain organizationId={parseInt(params.id)} />
    </Suspense>
  );
}
```

**New File**: `console/src/components/Pages/OrgBilling/Main.tsx`

```tsx
"use client";

import { useState, useEffect } from "react";
import { Button } from "@/components/UI/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/UI/card";
import { Separator } from "@/components/UI/separator";
import { Alert, AlertDescription, AlertTitle } from "@/components/UI/alert";
import { AlertCircle, CreditCard, Settings, DollarSign } from "lucide-react";

interface OrgBillingOverview {
  credits: number;
  billing_state: string;
  autorecharge_enabled: boolean;
  autorecharge_threshold: number;
  autorecharge_qty: number;
  billing_email: string | null;
}

export default function OrgBillingMain({ organizationId }: { organizationId: number }) {
  const [billing, setBilling] = useState<OrgBillingOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch(`/api/organizations/${organizationId}/billing`)
      .then((res) => {
        if (!res.ok) throw new Error("Failed to load billing");
        return res.json();
      })
      .then(setBilling)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [organizationId]);

  const handleAddFunds = async () => {
    const res = await fetch(`/api/organizations/${organizationId}/billing/checkout-session`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ amount: 50 }), // Default $50
    });
    const data = await res.json();
    if (data.checkout_url) {
      window.location.href = data.checkout_url;
    }
  };

  const handleManagePayments = async () => {
    const res = await fetch(`/api/organizations/${organizationId}/billing/portal-session`, {
      method: "POST",
    });
    const data = await res.json();
    if (data.portal_url) {
      window.location.href = data.portal_url;
    }
  };

  if (loading) return <div>Loading...</div>;
  if (error) return <Alert variant="destructive"><AlertCircle /><AlertTitle>Error</AlertTitle><AlertDescription>{error}</AlertDescription></Alert>;
  if (!billing) return null;

  return (
    <div className="space-y-6 p-8">
      <div>
        <h1 className="text-h1">Organization Billing</h1>
        <p className="text-subtitle">Manage credits and payment methods for your organization.</p>
      </div>

      {billing.billing_state !== "OK" && (
        <Alert variant="destructive">
          <AlertCircle className="h-4 w-4" />
          <AlertTitle>Billing Issue</AlertTitle>
          <AlertDescription>
            Your organization billing is {billing.billing_state}. Please update your payment method.
          </AlertDescription>
        </Alert>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <DollarSign className="h-5 w-5" /> Credit Balance
          </CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-3xl font-bold">${billing.credits.toFixed(2)}</p>
          <div className="mt-4 flex gap-2">
            <Button onClick={handleAddFunds}>Add Funds</Button>
            <Button variant="outline" onClick={handleManagePayments}>
              <CreditCard className="mr-2 h-4 w-4" /> Manage Payment Methods
            </Button>
          </div>
        </CardContent>
      </Card>

      <Separator />

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Settings className="h-5 w-5" /> Auto-Recharge
          </CardTitle>
        </CardHeader>
        <CardContent>
          <p>Status: {billing.autorecharge_enabled ? "Enabled" : "Disabled"}</p>
          <p>Threshold: ${billing.autorecharge_threshold}</p>
          <p>Recharge Amount: ${billing.autorecharge_qty}</p>
          {/* Add settings form here */}
        </CardContent>
      </Card>
    </div>
  );
}
```

### 5.3 Profile Page Updates

**File**: `console/src/components/Pages/Profile/Main.tsx`

Add section to show Organization memberships:

```tsx
// After UnifyKey component, add:
<div className="xl:w-[900px] w-full h-full mt-6">
  <h1 className="text-h2">Organizations</h1>
  <p className="text-title mt-2">You are a member of the following organizations:</p>
  <OrganizationList userId={user.id} />
</div>
```

**New Component**: `console/src/components/Pages/Profile/OrganizationList.tsx`

```tsx
"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { Card, CardContent } from "@/components/UI/card";
import { Badge } from "@/components/UI/badge";

interface Org {
  id: number;
  name: string;
  level: string; // owner, admin, user
}

export default function OrganizationList({ userId }: { userId: string }) {
  const [orgs, setOrgs] = useState<Org[]>([]);

  useEffect(() => {
    fetch("/api/user/organizations")
      .then((res) => res.json())
      .then(setOrgs);
  }, []);

  if (orgs.length === 0) {
    return <p className="text-muted-foreground mt-2">You are not part of any organization.</p>;
  }

  return (
    <div className="grid gap-4 mt-4">
      {orgs.map((org) => (
        <Card key={org.id}>
          <CardContent className="flex justify-between items-center p-4">
            <div>
              <Link href={`/org/${org.id}/billing`} className="font-medium hover:underline">
                {org.name}
              </Link>
              <Badge className="ml-2" variant="outline">{org.level}</Badge>
            </div>
            {(org.level === "owner" || org.level === "admin") && (
              <Link href={`/org/${org.id}/billing`} className="text-sm text-primary">
                Manage Billing →
              </Link>
            )}
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
```

### 5.4 API Key Management Updates

**File**: `console/src/components/Pages/Keys/UserAPIKeyPanel/APIKeyPanel.tsx`

Users should see:
1.  **Personal API Key** (bills their personal wallet)
2.  **Organization API Keys** (bills the org wallet) - one per org they belong to

```tsx
// Modify to show both personal and org keys
<div>
  <h2>Personal API Key</h2>
  <p className="text-sm text-muted-foreground">Usage is billed to your personal credit balance.</p>
  <UnifyKey initialApiKey={personalKey} />
</div>

{orgs.map((org) => (
  <div key={org.id} className="mt-6">
    <h2>Organization: {org.name}</h2>
    <p className="text-sm text-muted-foreground">Usage is billed to the organization&#39;s balance.</p>
    <UnifyKey initialApiKey={org.apiKey} orgId={org.id} />
  </div>
))}
```

### 5.5 Onboarding Flow (Organization Creation)

**New File**: `console/src/components/Pages/Organization/CreateOrgDialog.tsx`

```tsx
"use client";
import { useState } from "react";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/UI/dialog";
import { Input } from "@/components/UI/input";
import { Button } from "@/components/UI/button";
import { Label } from "@/components/UI/label";

export default function CreateOrgDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [name, setName] = useState("");
  const [loading, setLoading] = useState(false);

  const handleCreate = async () => {
    setLoading(true);
    const res = await fetch("/api/organizations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    
    if (res.ok) {
      const org = await res.json();
      // Redirect to org billing setup
      window.location.href = `/org/${org.id}/billing`;
    }
    setLoading(false);
  };

  return (
    <Dialog open={open} onOpenChange={onClose}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Create Organization</DialogTitle>
        </DialogHeader>
        <div className="space-y-4">
          <div>
            <Label>Organization Name</Label>
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Acme Corp" />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button onClick={handleCreate} disabled={loading || !name}>
            {loading ? "Creating..." : "Create Organization"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
```

---

## 6. Lifecycle Workflows

### 6.1 Organization Creation

1.  **User Action**: Clicks "Create Organization".
2.  **Backend** (`POST /organizations`):
    *   Create `Organization` record.
    *   Create Stripe Customer.
    *   Store `stripe_customer_id`.
    *   Add user as `Owner` member.
    *   Create Org API Key for user.
3.  **Frontend**: Redirect to `/org/{id}/billing` for initial setup.

### 6.2 Adding Member

1.  **Admin Action**: Invites user by email.
2.  **Backend** (`POST /organizations/{id}/members`):
    *   Add `OrganizationMember` record.
    *   Create Org API Key for new member.
3.  **Billing Impact**: None. New member can now use Org API Key (bills Org wallet).

### 6.3 Removing Member

1.  **Admin Action**: Removes user.
2.  **Backend** (`DELETE /organizations/{id}/members/{user_id}`):
    *   Delete `OrganizationMember` record.
    *   **Revoke Org API Keys** for that user.
3.  **Billing Impact**: Outstanding queries finish and bill the Org. Future queries blocked.

### 6.4 Closing Organization

1.  **Owner Action**: Initiates closure.
2.  **Validation**:
    *   Check `billing_state != PAST_DUE`.
    *   Check no outstanding invoices.
3.  **Stripe Cleanup**:
    *   Cancel subscriptions.
    *   Detach payment methods.
4.  **DB Update**:
    *   Set `is_active=False`, `archived_at=Now`.
5.  **Credit Handling**: Remaining credits forfeited (or manual refund via support).

---

## 7. Security & Access Control

### 7.1 Permission Definitions

| Permission | Description |
|------------|-------------|
| `org:read` | View org info, members |
| `org:write` | Update org settings, add/remove members |
| `org:delete` | Delete organization |
| `org:billing:read` | View credits, invoices |
| `org:billing:write` | Add funds, manage cards, update settings |

### 7.2 Role Mapping

| Role | Permissions |
|------|-------------|
| Owner | `org:*` |
| Admin | `org:read`, `org:write`, `org:billing:*` |
| Member | `org:read` |
| Viewer | `org:read` |

---

## 8. Implementation Checklist

### Phase 1: Database & Models
- [ ] Create Alembic migration for `Organization` columns
- [ ] Create Alembic migration for `Recharge` columns
- [ ] Update SQLAlchemy models
- [ ] Update `OrganizationDAO`
- [ ] Update `RechargeDAO`

### Phase 2: Backend Logic
- [ ] Refactor `orchestra/lib/billing.py`
- [ ] Update `orchestra/web/api/utils/bg_tasks.py`
- [ ] Create `orchestra/web/api/organization/billing.py`
- [ ] Register new router in `orchestra/web/api/router.py`

### Phase 3: Stripe Webhooks
- [ ] Update `process_checkout_session_event` for `organization_id`
- [ ] Update `process_invoice_event` for Org billing state
- [ ] Add `process_customer_updated` for payment method sync

### Phase 4: Frontend - API Layer
- [ ] Create `/api/organizations/[id]/billing` proxy route
- [ ] Create `/api/user/organizations` endpoint
- [ ] Update `/api/stripe/checkoutSession` for org context

### Phase 5: Frontend - UI
- [ ] Create `/org/[id]/billing/page.tsx`
- [ ] Create `OrgBillingMain.tsx` component
- [ ] Update Profile page with Org list
- [ ] Update API Key panel for Org keys
- [ ] Create Org creation dialog

### Phase 6: Testing
- [ ] Unit tests for `process_cost_deduction`
- [ ] Integration tests for Org checkout flow
- [ ] Integration tests for Org auto-recharge
- [ ] E2E tests for Org billing UI

---

## 9. Appendix: File Change Summary

| File | Action | Description |
|------|--------|-------------|
| `orchestra/db/models/orchestra_models.py` | MODIFY | Add Org billing columns, update Recharge |
| `orchestra/db/migrations/versions/xxxx.py` | CREATE | Migration script |
| `orchestra/lib/billing.py` | MODIFY | Add `process_cost_deduction`, `queue_org_auto_recharge` |
| `orchestra/web/api/utils/bg_tasks.py` | MODIFY | Use new billing function |
| `orchestra/web/api/webhooks/stripe.py` | MODIFY | Handle `organization_id` |
| `orchestra/web/api/organization/billing.py` | CREATE | New billing endpoints |
| `orchestra/web/api/router.py` | MODIFY | Register billing router |
| `console/src/app/(home)/org/[id]/billing/page.tsx` | CREATE | Org billing page |
| `console/src/components/Pages/OrgBilling/Main.tsx` | CREATE | Org billing component |
| `console/src/components/Pages/Profile/Main.tsx` | MODIFY | Add Org list |
| `console/src/components/Pages/Profile/OrganizationList.tsx` | CREATE | Org membership list |
| `console/src/components/Pages/Keys/*` | MODIFY | Show personal + org keys |
