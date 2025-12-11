# Organization Work Overview
## Summary of Completed Work by Julia (Orchestra) and Nassim (Console)

This document provides a comprehensive overview of the organization-related work that has already been implemented in both the `orchestra` backend and `console` frontend. Your task is to add **billing and Stripe integration** on top of this foundation.

---

## Part 1: Orchestra Backend (Julia's Work)

### 1.1 Database Schema & Migrations

Julia has implemented a complete RBAC (Role-Based Access Control) system for organizations. The following migrations have been applied:

#### Migration 1: `2025-11-12-16-00_add_org_billing.py`
**Purpose**: Foundation for organization billing

| Change | Description |
|--------|-------------|
| `organization.billing_user_id` | Added column pointing to `users.id` - this determines which USER's wallet pays for org usage |
| `query.organization_id` | Added column to track whether a query belongs to an org or personal account |
| Dropped `owner_id` unique constraint | Allows users to own multiple organizations |

**Current State**: Billing is delegated to a specific user's personal wallet (`billing_user_id`). This is the mechanism **you need to replace** with direct organization billing.

#### Migration 2: `2025-11-12-17-00_add_rbac_foundation.py`
**Purpose**: Create Permission and Role tables

| Table | Columns | Notes |
|-------|---------|-------|
| `permission` | `id`, `name`, `description`, `resource_type`, `action` | Atomic permissions like `project:read`, `org:write` |
| `role` | `id`, `name`, `description`, `organization_id`, `is_system_role` | `organization_id=NULL` means system role |
| `role_permission` | `id`, `role_id`, `permission_id` | Many-to-many join table |

**Seeded Permissions** (6 total):
```
project:read   - View project details
project:write  - Edit project
project:delete - Delete project
org:read       - View organization details
org:write      - Edit organization settings, billing, and members
org:delete     - Delete organization
```

**Seeded System Roles** (4 roles):
```
Owner  → 6 permissions (all)
Admin  → 5 permissions (all except org:delete)
Member → 3 permissions (project read/write, org read)
Viewer → 2 permissions (project + org read only)
```

#### Migration 3: `2025-11-12-18-00_add_rbac_teams_resource_access.py`
**Purpose**: Teams and granular resource-level permissions

| Table | Columns | Notes |
|-------|---------|-------|
| `team` | `id`, `name`, `description`, `organization_id` | Groups of users within an org |
| `team_member` | `id`, `team_id`, `user_id` | Many-to-many join |
| `resource_access` | `id`, `resource_type`, `resource_id`, `role_id`, `grantee_type`, `grantee_id` | Explicit RBAC grants |

**Key Insight**: `resource_access` allows granting specific roles to users/teams on specific resources (projects or organizations). If no explicit grant exists, the system falls back to implicit organization membership permissions.

#### Migration 4: `2025-11-12-19-00_add_member_roles.py`
**Purpose**: Connect organization membership with RBAC roles

| Change | Description |
|--------|-------------|
| `organization_member.role_id` | Added NOT NULL column linking to `role.id` |
| Backfill | Existing members → "Member" role; Owners → "Owner" role |

---

### 1.2 Database Models (`orchestra/db/models/orchestra_models.py`)

```python
class Organization(Base):
    __tablename__ = "organization"
    id = Column(Integer, primary_key=True)
    owner_id = Column(String, ForeignKey("auth_user.id"), nullable=False)
    billing_user_id = Column(String, ForeignKey("users.id"), nullable=False)  # ← DELEGATED BILLING
    name = Column(String, unique=True, nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now())

class OrganizationMember(Base):
    __tablename__ = "organization_member"
    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organization.id"), nullable=False)
    user_id = Column(String, ForeignKey("auth_user.id"), nullable=False)
    level = Column(String, nullable=False)  # "owner", "admin", "user" (legacy)
    role_id = Column(Integer, ForeignKey("role.id"), nullable=False)  # RBAC role
    created_at = Column(TIMESTAMP, server_default=func.now())

class Permission(Base):
    __tablename__ = "permission"
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)  # e.g., "project:read"
    description = Column(String, nullable=True)
    resource_type = Column(String, nullable=False)  # e.g., "project"
    action = Column(String, nullable=False)  # e.g., "read"

class Role(Base):
    __tablename__ = "role"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    organization_id = Column(Integer, ForeignKey("organization.id"), nullable=True)  # NULL = system role
    is_system_role = Column(Boolean, server_default="f", nullable=False)

class RolePermission(Base):  # Join table
    role_id = Column(Integer, ForeignKey("role.id"), nullable=False)
    permission_id = Column(Integer, ForeignKey("permission.id"), nullable=False)

class Team(Base):
    __tablename__ = "team"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    organization_id = Column(Integer, ForeignKey("organization.id"), nullable=False)

class TeamMember(Base):  # Join table
    team_id = Column(Integer, ForeignKey("team.id"), nullable=False)
    user_id = Column(String, ForeignKey("auth_user.id"), nullable=False)

class ResourceAccess(Base):
    __tablename__ = "resource_access"
    id = Column(Integer, primary_key=True)
    resource_type = Column(String, nullable=False)  # "project" or "org"
    resource_id = Column(Integer, nullable=False)
    role_id = Column(Integer, ForeignKey("role.id"), nullable=False)
    grantee_type = Column(String, nullable=False)  # "user" or "team"
    grantee_id = Column(String, nullable=False)
```

---

### 1.3 API Endpoints (Fully Implemented)

#### Organization Management (`/organizations`)
| Method | Endpoint | Function | Notes |
|--------|----------|----------|-------|
| POST | `/organizations` | `create_organization` | Creates org, adds owner as member with Owner role |
| GET | `/organizations` | `list_organizations` | Lists orgs user owns or is member of |
| GET | `/organizations/{id}` | `get_organization` | Get single org details |
| PATCH | `/organizations/{id}` | `update_organization` | Update name, billing_user_id (requires `org:write`) |
| DELETE | `/organizations/{id}` | `delete_organization` | Delete org (requires `org:delete`) |

#### Organization Members (`/organizations/{id}/members`)
| Method | Endpoint | Function | Notes |
|--------|----------|----------|-------|
| POST | `/organizations/{id}/members` | `add_organization_member` | Adds user with specified role |
| GET | `/organizations/{id}/members` | `list_organization_members` | Lists all members with roles |
| PATCH | `/organizations/{id}/members/{user_id}/role` | `update_member_role` | Changes member's RBAC role |
| DELETE | `/organizations/{id}/members/{user_id}` | `remove_organization_member` | Removes member from org |

#### Roles & Permissions
| Method | Endpoint | Function | Notes |
|--------|----------|----------|-------|
| GET | `/permissions` | `list_permissions` | List all atomic permissions |
| GET | `/organizations/{id}/roles` | `list_organization_roles` | List system + custom roles |
| POST | `/organizations/{id}/roles` | `create_custom_role` | Create custom role with permissions |
| GET | `/organizations/{id}/roles/{role_id}` | `get_role` | Get role details with permissions |
| PATCH | `/organizations/{id}/roles/{role_id}` | `update_role` | Update custom role name/description |
| DELETE | `/organizations/{id}/roles/{role_id}` | `delete_role` | Delete custom role |
| POST | `/organizations/{id}/roles/{role_id}/permissions` | `add_permissions_to_role` | Add permissions to custom role |
| DELETE | `/organizations/{id}/roles/{role_id}/permissions/{perm_id}` | `remove_permission_from_role` | Remove permission from role |

#### Teams (`/organizations/{id}/teams`)
| Method | Endpoint | Function | Notes |
|--------|----------|----------|-------|
| POST | `/organizations/{id}/teams` | `create_team` | Create team in org |
| GET | `/organizations/{id}/teams` | `list_teams` | List all teams |
| GET | `/organizations/{id}/teams/{team_id}` | `get_team` | Get team with members |
| PATCH | `/organizations/{id}/teams/{team_id}` | `update_team` | Update team name/description |
| DELETE | `/organizations/{id}/teams/{team_id}` | `delete_team` | Delete team |
| POST | `/organizations/{id}/teams/{team_id}/members` | `add_team_members` | Add users to team |
| DELETE | `/organizations/{id}/teams/{team_id}/members/{user_id}` | `remove_team_member` | Remove user from team |

#### Resource Access (`/resources/{type}/{id}/access`)
| Method | Endpoint | Function | Notes |
|--------|----------|----------|-------|
| POST | `/resources/{type}/{id}/access` | `grant_resource_access` | Grant role to user/team on resource |
| GET | `/resources/{type}/{id}/access` | `list_resource_access` | List all access entries |
| PATCH | `/resources/{type}/{id}/access/{access_id}` | `update_resource_access` | Change role on existing grant |
| DELETE | `/resources/{type}/{id}/access` | `revoke_resource_access` | Revoke access |

---

### 1.4 DAOs (Data Access Objects)

| DAO | Location | Key Methods |
|-----|----------|-------------|
| `OrganizationDAO` | `db/dao/organization_dao.py` | `create`, `get`, `update`, `delete`, `get_billing_user_id`, `get_user_organizations` |
| `OrganizationMemberDAO` | `db/dao/organization_member_dao.py` | `create`, `filter`, `update`, `delete`, `get_member`, `update_member_role`, `list_members` |
| `RoleDAO` | `db/dao/role_dao.py` | `create`, `get`, `get_by_name`, `get_organization_roles`, `add_permission`, `remove_permission`, `has_permission` |
| `PermissionDAO` | `db/dao/permission_dao.py` | `get_by_resource_type`, `list_all` |
| `TeamDAO` | `db/dao/team_dao.py` | `create`, `get`, `get_by_name`, `list_organization_teams`, `add_member`, `remove_member`, `get_team_members`, `is_team_member` |
| `ResourceAccessDAO` | `db/dao/resource_access_dao.py` | `grant_access`, `revoke_access`, `get_resource_access`, `get_user_access`, `check_user_permission`, `check_user_has_permission_in_org` |

---

### 1.5 Permission Checking Logic (`ResourceAccessDAO.check_user_permission`)

The permission checking follows this priority order:

```
1. Is user the Organization Owner? → Use Owner role permissions (implicit full access)
2. Does the resource have explicit ResourceAccess entries?
   → YES: Only check explicit grants for this user/teams
   → NO: Fall back to implicit organization membership role
3. Get user's role from OrganizationMember.role_id
4. Check if role has required permission via RolePermission table
```

**Key Feature**: Permission cache with automatic invalidation when roles/memberships change.

---

## Part 2: Console Frontend (Nassim's Work)

### 2.1 User Type with Organization Context

```typescript
// console/src/types/user.ts
export interface User {
    id: string;
    name: string;
    lastName: string;
    jobTitle: string;
    email: string;
    apiKey: string;
    stripe_customer_id: string;  // ← User's personal Stripe ID
    organization: {
        name: string;
        level: string;  // "owner", "admin", "user"
    }
    // ... other fields
}
```

### 2.2 Admin Page (Organization-Aware)

The Admin page (`console/src/app/(home)/admin/page.tsx`) checks organization level:

```typescript
const isAdmin = user.organization?.level === "admin" || user.organization?.level === "owner";
```

This guards access to:
- User Hiring Approvals
- One-Time Approval Links

### 2.3 Billing Components (Currently User-Only)

| Component | Location | Purpose |
|-----------|----------|---------|
| `Main.tsx` | `Pages/Billing/Main.tsx` | Main billing page |
| `Balance.tsx` | `Pages/Billing/Balance.tsx` | Shows credit balance |
| `Refill.tsx` | `Pages/Billing/Refill.tsx` | Add credits UI |

**Current State**: All billing is tied to the individual user (`user.stripe_customer_id`).

### 2.4 Billing API Routes (Currently User-Only)

| Route | Purpose |
|-------|---------|
| `/api/billing/balance` | Get user credit balance |
| `/api/billing/details` | Get full billing details |
| `/api/billing/auto-recharge/*` | Auto-recharge settings |
| `/api/billing/eligibility` | Monthly billing eligibility |
| `/api/billing/hasFreeCredits` | Check free credits |
| `/api/billing/hasCustomerId` | Check Stripe customer exists |
| `/api/billing/syncCards` | Sync card fingerprints |
| `/api/billing/userCards` | Get user's saved cards |
| `/api/stripe/*` | Stripe checkout, portal, sessions |

### 2.5 Billing Library Functions (`lib/user/billing/`)

| File | Key Functions |
|------|---------------|
| `billing.ts` | `getUserBillingDetails`, `enableAutoRecharge`, `setAutoRechargeThreshold`, `setAutoRechargeQty`, `createRecharge`, `getUserCards` |
| `stripe.ts` | `getCustomerByEmail`, `createCustomer`, `createCheckoutSession`, `createPortalSession` |
| `payments.ts` | Payment processing logic |
| `customer-sync.ts` | Stripe customer synchronization |

---

## Part 3: What's Missing (Your Task)

### 3.1 Database Changes Needed

You need to **add billing columns directly to the Organization table**:

```python
class Organization(Base):
    # ... existing fields ...
    
    # NEW BILLING FIELDS
    credits = Column(Numeric, nullable=False, default=0, server_default="0")
    stripe_customer_id = Column(String, nullable=True)  # Org's own Stripe Customer
    autorecharge = Column(Boolean, nullable=False, default=False, server_default="f")
    autorecharge_threshold = Column(Numeric, nullable=False, default=10, server_default="10")
    autorecharge_qty = Column(Numeric, nullable=False, default=100, server_default="100")
    
    # B2B Invoicing fields
    billing_email = Column(String, nullable=True)
    tax_id = Column(String, nullable=True)
    billing_address = Column(String, nullable=True)  # JSON or separate table
    
    # Account status
    billing_status = Column(String, nullable=False, default="ACTIVE", server_default="'ACTIVE'")
```

### 3.2 Stripe Integration Needed

1. **Organization Stripe Customer Creation**: When org sets up billing, create a Stripe Customer for the org (not the user)
2. **Webhook Handler Update**: `orchestra/web/api/webhooks/stripe.py` needs to handle `organization_id` in metadata
3. **Checkout Sessions**: Create checkout sessions that credit the organization wallet
4. **Auto-Recharge**: Implement org-level auto-recharge (can reuse user logic pattern)

### 3.3 Billing Logic Needed

Update `orchestra/lib/billing.py`:
- `get_billing_user_id()` → needs to become `get_billing_entity()` returning either user or org
- Cost deduction logic needs to check if operation is under an org context
- Credit the correct entity (user or org) based on context

### 3.4 API Endpoints Needed

New endpoints under `/organizations/{id}/billing/`:
```
GET  /organizations/{id}/billing          - Get org billing details
POST /organizations/{id}/billing/setup    - Setup Stripe customer for org
GET  /organizations/{id}/billing/portal   - Get Stripe billing portal URL
POST /organizations/{id}/billing/checkout - Create checkout session for org
PATCH /organizations/{id}/billing/auto-recharge - Update auto-recharge settings
```

### 3.5 Frontend Components Needed

1. **Organization Billing Page**: Similar to user billing but for orgs
2. **Organization Settings Page**: Manage org profile, billing email, tax info
3. **Context Switcher**: Allow users to switch between personal and org contexts in billing

---

## Part 4: How Current Billing Works (For Reference)

### 4.1 Cost Deduction Flow

```
1. API Request comes in with API Key
2. dependencies.py extracts organization_id from ApiKey
3. billing.py.get_billing_user_id(organization_id) returns billing_user_id
4. Cost is deducted from Users.credits WHERE id = billing_user_id
5. If credits < threshold, trigger auto-recharge for that user
```

### 4.2 Stripe Webhook Flow

```
1. Stripe sends checkout.session.completed
2. webhooks/stripe.py extracts user_id from session metadata
3. Creates Recharge record for that user_id
4. Updates Users.credits += amount
```

### 4.3 What Changes for Org Billing

```
1. API Request comes in with API Key (same)
2. Check if organization has its own billing (stripe_customer_id NOT NULL)
3. If YES: Deduct from Organization.credits
4. If NO: Fall back to billing_user_id (legacy support)
5. Stripe webhooks check for organization_id in metadata first
```

---

## Summary

**Julia's Work (Orchestra)**: ✅ Complete RBAC system with Organizations, Members, Roles, Permissions, Teams, and Resource Access. The `org:write` permission is already defined for billing operations.

**Nassim's Work (Console)**: ✅ Admin page with org-level access control. Billing components exist but are user-only.

**Your Task**: Add billing fields to Organization table, update billing logic to support org wallets, create new API endpoints for org billing, update Stripe integration, and create frontend components for org billing management.

