import uuid
from datetime import datetime
from enum import Enum

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    Column,
    Date,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import backref, relationship

from orchestra.db.base import Base

# Python 3.11 ships enum.StrEnum. – Provide a fallback for older versions
try:
    from enum import StrEnum
except ImportError:  # pragma: no cover

    class StrEnum(str, Enum):  # type: ignore[override]
        """Minimal back-port of enum.StrEnum."""


# New enum mirrors the DB type ``recharge_status`` (see migration 20250520…)
class RechargeStatus(StrEnum):
    PENDING_INVOICE = "PENDING_INVOICE"
    INVOICE_CREATED = "INVOICE_CREATED"
    PAID = "PAID"
    FAILED = "FAILED"
    DISPUTED = "DISPUTED"


# Recharge type constants (moved from consts.py)
RECHARGE_TYPE_AUTO = "auto"
RECHARGE_TYPE_PAYMENT = "payment"
RECHARGE_TYPE_PROMO = "promo"


class BillingAccount(Base):
    """
    Shared billing entity for User and Organization.

    Consolidates all billing-related fields (credits, Stripe customer, autorecharge,
    account status) AND optional business profile fields (tax ID, address, business name)
    into a single table. Both User and Organization link here via FK.

    This eliminates field duplication and provides a single code path for all billing logic.
    """

    __tablename__ = "billing_account"

    id = Column(Integer, primary_key=True)

    # === CORE BILLING ===
    credits = Column(Numeric, nullable=False, default=0, server_default="0")
    stripe_customer_id = Column(String, nullable=True, unique=True, index=True)
    autorecharge = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    autorecharge_threshold = Column(
        Numeric,
        nullable=False,
        default=0,
        server_default="0",
    )
    autorecharge_qty = Column(
        Numeric,
        nullable=False,
        default=25,
        server_default="25",
    )
    account_status = Column(
        String,
        nullable=False,
        default="ACTIVE",
        server_default="ACTIVE",
    )  # ACTIVE, PAST_DUE, SUSPENDED, CLOSED
    billing_setup_complete = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    tier = Column(
        String,
        nullable=False,
        server_default="developer",
    )  # developer, pro, enterprise (future)

    # === BILLING PROFILE (optional — available to all billing entities) ===
    # A personal user can add their name / tax details without creating an org.
    # An org fills these in for proper business invoicing.
    # All fields sync to the Stripe Customer when set.
    # ``name`` is the display name — mapped to Stripe's individual_name (users)
    # or business_name (orgs) via build_stripe_customer_name().
    billing_email = Column(String, nullable=True)
    name = Column(String(255), nullable=True)
    tax_id = Column(String(100), nullable=True)
    tax_id_type = Column(String(50), nullable=True)
    tax_id_verification_status = Column(
        String(20),
        nullable=True,
    )  # pending, verified, unverified, unavailable (from Stripe)
    billing_address = Column(JSONB, nullable=True, default=dict)

    # === TIMESTAMPS ===
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, onupdate=func.now())

    # === RELATIONSHIPS ===
    recharges = relationship(
        "Recharge",
        back_populates="billing_account",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    credit_card_fingerprints = relationship(
        "CreditCardFingerprint",
        back_populates="billing_account",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        sa.CheckConstraint(
            "account_status IN ('ACTIVE', 'PAST_DUE', 'SUSPENDED', 'CLOSED')",
            name="ck_billing_account_status",
        ),
    )


class Recharge(Base):
    """Model class for the recharge table."""

    __tablename__ = "recharge"

    id = Column(Integer(), primary_key=True)
    at = Column(
        TIMESTAMP,
        nullable=False,
        server_default=func.now(),
        default=datetime.utcnow,
    )
    # Billing account that this recharge belongs to
    billing_account_id = Column(
        Integer,
        ForeignKey("billing_account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    quantity = Column(Numeric(), nullable=False)
    amount_usd = Column(Numeric(), nullable=False)
    type = Column(String())
    transaction_id = Column(String())
    status = Column(
        String(),
        nullable=False,
        server_default=RechargeStatus.PENDING_INVOICE.value,
    )
    stripe_invoice_id = Column(String)
    invoice_group = Column(Date)

    # ORM relationships
    billing_account = relationship("BillingAccount", back_populates="recharges")

    __table_args__ = (
        Index("idx_recharge_pending", "status", "invoice_group"),
        sa.CheckConstraint(
            "status IN ('PENDING_INVOICE','PAID','FAILED','INVOICE_CREATED','DISPUTED')",
            name="ck_recharge_status",
        ),
    )


class WebhookLog(Base):
    """
    Model for tracking processed Stripe webhook events to enforce idempotency.
    Each record represents a successfully processed webhook event.
    """

    __tablename__ = "webhook_log"

    id = Column(String, primary_key=True)
    event_id = Column(String, unique=True, nullable=False)
    event_type = Column(String, nullable=False)
    processed_at = Column(TIMESTAMP, nullable=False, server_default=func.now())


class RechargeType(Base):
    """Model class for the recharge_type table."""

    __tablename__ = "recharge_type"

    type = Column(String(), primary_key=True)


class CreditCardFingerprint(Base):
    """Model class for the credit card fingerprint table."""

    __tablename__ = "credit_card_fingerprint"

    id = Column(Integer(), primary_key=True)
    billing_account_id = Column(
        Integer,
        ForeignKey("billing_account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    fingerprint = Column(String(), nullable=False)

    # ORM relationship
    billing_account = relationship(
        "BillingAccount",
        back_populates="credit_card_fingerprints",
    )


class User(Base):
    """
    Consolidated user model.

    Previously split across `users` (billing) and `auth_user` (profile).
    Now a single table matching Organization, OrganizationMember, Team architecture.

    Billing fields live on BillingAccount (linked via billing_account_id FK).
    """

    __tablename__ = "user"

    # === IDENTITY FIELDS ===
    id = Column(String, primary_key=True, default=uuid.uuid4)
    email = Column(String, unique=True, index=True, nullable=False)
    name = Column(String)
    last_name = Column(String)
    job_title = Column(String)
    bio = Column(String, nullable=True)
    image = Column(String)
    timezone = Column(String, nullable=True)
    phone_number = Column(String, nullable=True)

    # === BILLING (via BillingAccount) ===
    billing_account_id = Column(
        Integer,
        ForeignKey("billing_account.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # === ACCOUNT SETTINGS ===
    # Toggles managed by usage quotas
    queries_enabled = Column(Boolean, nullable=False, server_default="true")
    evaluations_enabled = Column(Boolean, nullable=False, server_default="true")
    onboarded = Column(Boolean, nullable=False, server_default="false")
    store_prompts = Column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )

    # === SPENDING LIMITS ===
    # Monthly spending limit for this user's assistants (NULL = no limit)
    # Cannot exceed the org's monthly_spending_cap if user is in an org
    monthly_spending_cap = Column(Numeric, nullable=True)
    monthly_spending_cap_set_at = Column(TIMESTAMP(timezone=True), nullable=True)

    # === TIMESTAMPS ===
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, onupdate=func.now())

    # === RELATIONSHIPS ===
    billing_account = relationship("BillingAccount", foreign_keys=[billing_account_id])
    interfaces = relationship(
        "Interface",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


# Account table (for external providers like Google, GitHub)
# Each user can have multiple accounts
class Account(Base):
    __tablename__ = "account"

    id = Column(String, primary_key=True, default=uuid.uuid4)
    user_id = Column(String, ForeignKey("user.id", ondelete="CASCADE"))
    provider = Column(String, nullable=False)  # OAuth provider name
    provider_type = Column(String, nullable=False)
    provider_account_id = Column(String, nullable=False)
    access_token = Column(String)  # OAuth access token (optional)
    # TODO: This can be removed? refreshtokens
    refresh_token = Column(String)  # OAuth refresh token (optional)
    # Expiration time for OAuth token (optional)
    expires_at = Column(TIMESTAMP)


class Organization(Base):
    """
    Organization model.

    Billing fields live on BillingAccount (linked via billing_account_id FK).
    Business profile fields (tax_id, billing_address, etc.) also live on BillingAccount.
    """

    __tablename__ = "organization"

    id = Column(Integer, primary_key=True)
    owner_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    name = Column(String, unique=True, nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now())

    # === BILLING (via BillingAccount) ===
    billing_account_id = Column(
        Integer,
        ForeignKey("billing_account.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Timezone for org-level billing (IANA format, e.g., "America/New_York")
    # Initialized from owner's timezone on creation, defaults to UTC if not set
    timezone = Column(String, nullable=True)

    # Monthly spending limit for all users/assistants in the org (NULL = no limit)
    monthly_spending_cap = Column(Numeric, nullable=True)
    monthly_spending_cap_set_at = Column(TIMESTAMP(timezone=True), nullable=True)

    # === VERIFICATION FIELDS ===
    # Verified orgs get higher rate limits
    verified = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="Whether org has been manually verified by admin",
    )
    verified_at = Column(
        TIMESTAMP(timezone=True),
        nullable=True,
        comment="When the org was verified",
    )

    # Relationships
    billing_account = relationship(
        "BillingAccount",
        foreign_keys=[billing_account_id],
    )
    interfaces = relationship(
        "Interface",
        back_populates="organization",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class OrganizationMember(Base):
    __tablename__ = "organization_member"

    id = Column(Integer, primary_key=True)
    organization_id = Column(
        Integer,
        ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    role_id = Column(
        Integer,
        ForeignKey("role.id", ondelete="RESTRICT"),
        nullable=False,
    )  # RBAC role for this member (Owner, Admin, Member, Viewer, or custom roles)
    created_at = Column(TIMESTAMP, server_default=func.now())

    # Monthly spending limit for this member within this org (NULL = no limit)
    # Set by org admins; cannot exceed org's monthly_spending_cap
    monthly_spending_cap = Column(Numeric, nullable=True)
    # When the spending cap was last changed (for notification deduplication)
    monthly_spending_cap_set_at = Column(TIMESTAMP(timezone=True), nullable=True)


class OrganizationInvite(Base):
    """Model for pending organization invitations.

    Invites are deleted when accepted or declined.
    Expired invites are cleaned up via admin endpoint.
    """

    __tablename__ = "organization_invite"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    token = Column(String, unique=True, index=True, nullable=False)
    organization_id = Column(
        Integer,
        ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    invitee_email = Column(String, nullable=False, index=True)
    invitee_user_id = Column(
        String,
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )  # Set if user already exists in system
    invited_by_user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    role_id = Column(
        Integer,
        ForeignKey("role.id", ondelete="RESTRICT"),
        nullable=False,
    )  # Role to assign when invite is accepted
    expires_at = Column(TIMESTAMP(timezone=True), nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


class Permission(Base):
    """Model for permissions (atomic actions like 'project:read', 'interface:edit')."""

    __tablename__ = "permission"

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)  # e.g., "project:read"
    description = Column(String, nullable=True)
    resource_type = Column(String, nullable=False)  # e.g., "project", "interface"
    action = Column(String, nullable=False)  # e.g., "read", "write", "delete"
    created_at = Column(TIMESTAMP, server_default=func.now())


class Role(Base):
    """Model for roles within organizations."""

    __tablename__ = "role"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)  # e.g., "Owner", "Admin", "Member", "Viewer"
    description = Column(String, nullable=True)
    organization_id = Column(
        Integer,
        ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=True,  # NULL = system role, available to all orgs
    )
    is_system_role = Column(
        Boolean,
        server_default="f",
        nullable=False,
    )  # True for built-in roles
    created_at = Column(TIMESTAMP, server_default=func.now())

    # Relationships
    permissions = relationship(
        "Permission",
        secondary="role_permission",
        backref="roles",
    )

    __table_args__ = (
        UniqueConstraint("name", "organization_id", name="uq_role_name_org"),
    )


class RolePermission(Base):
    """Join table for Role-Permission many-to-many relationship."""

    __tablename__ = "role_permission"

    id = Column(Integer, primary_key=True)
    role_id = Column(
        Integer,
        ForeignKey("role.id", ondelete="CASCADE"),
        nullable=False,
    )
    permission_id = Column(
        Integer,
        ForeignKey("permission.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at = Column(TIMESTAMP, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("role_id", "permission_id", name="uq_role_permission"),
    )


class Team(Base):
    """Model for teams within organizations."""

    __tablename__ = "team"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    organization_id = Column(
        Integer,
        ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at = Column(TIMESTAMP, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("name", "organization_id", name="uq_team_name_org"),
    )


class TeamMember(Base):
    """Join table for Team-User many-to-many relationship."""

    __tablename__ = "team_member"

    id = Column(Integer, primary_key=True)
    team_id = Column(
        Integer,
        ForeignKey("team.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at = Column(TIMESTAMP, server_default=func.now())

    __table_args__ = (UniqueConstraint("team_id", "user_id", name="uq_team_member"),)


class ResourceAccess(Base):
    """Model for resource-level access control (RBAC)."""

    __tablename__ = "resource_access"

    id = Column(Integer, primary_key=True)
    resource_type = Column(
        String,
        nullable=False,
    )  # e.g., 'project', 'interface', 'tab', 'tile'
    resource_id = Column(Integer, nullable=False)
    role_id = Column(
        Integer,
        ForeignKey("role.id", ondelete="CASCADE"),
        nullable=False,
    )
    grantee_type = Column(
        String,
        nullable=False,
    )  # 'user' or 'team'
    grantee_id = Column(
        String,
        nullable=False,
    )  # user_id or team_id (as string)
    created_at = Column(TIMESTAMP, server_default=func.now())

    __table_args__ = (
        # Only one role per grantee per resource (single-role-per-resource)
        UniqueConstraint(
            "resource_type",
            "resource_id",
            "grantee_type",
            "grantee_id",
            name="uq_resource_access_grantee",
        ),
        Index("idx_resource_access_resource", "resource_type", "resource_id"),
        Index("idx_resource_access_grantee", "grantee_type", "grantee_id"),
    )


class ApiKey(Base):
    __tablename__ = "api_key"

    id = Column(Integer, primary_key=True)
    name = Column(String)
    user_id = Column(String, ForeignKey("user.id", ondelete="CASCADE"))
    organization_id = Column(Integer, ForeignKey("organization.id", ondelete="CASCADE"))
    key = Column(String, unique=True, nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now())

    __table_args__ = (UniqueConstraint("user_id", "name"),)


class Project(Base):
    __tablename__ = "project"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        index=True,
    )
    organization_id = Column(
        Integer,
        ForeignKey("organization.id", ondelete="CASCADE"),
        index=True,
    )
    name = Column(String, nullable=False)
    description = Column(String(256), nullable=True)
    icon = Column(String, nullable=False, server_default="folder")
    order = Column(Integer, nullable=False, server_default="0")
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, onupdate=func.now())
    is_versioned = Column(Boolean, nullable=False, server_default="f")
    current_commit_hash = Column(String, nullable=True)
    contexts = relationship("Context", back_populates="project", passive_deletes=True)
    interfaces = relationship(
        "Interface",
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    # we want sql nulls to be distinct in the unique constraints
    # (postgresql_nulls_not_distinct=False)
    __table_args__ = (
        UniqueConstraint("user_id", "name"),
        UniqueConstraint("organization_id", "name"),
        sa.CheckConstraint(
            "char_length(description) <= 256",
            name="ck_project_description_len",
        ),
    )


class ProjectVersion(Base):
    """Model class for storing historical versions of projects."""

    __tablename__ = "project_version"

    id = Column(Integer, primary_key=True)
    project_id = Column(
        Integer,
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    commit_hash = Column(String, nullable=False, unique=True)
    prev_commit_hash = Column(String, nullable=True)
    next_commit_hash = Column(JSONB, nullable=False, server_default="[]")
    commit_message = Column(String, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    # Relationship to its ContextVers ions
    context_versions = relationship("ContextVersion", back_populates="project_version")


class LogEventContext(Base):
    """Association table for the many-to-many relationship between LogEvent and Context."""

    __tablename__ = "log_event_context"

    log_event_id = Column(
        Integer,
        ForeignKey("log_event.id", ondelete="CASCADE"),
        primary_key=True,
    )
    context_id = Column(
        Integer,
        ForeignKey("context.id", ondelete="CASCADE"),
        primary_key=True,
    )


class Context(Base):
    """Model class for organizing logs and artifacts within projects."""

    __tablename__ = "context"

    id = Column(Integer, primary_key=True)
    project_id = Column(
        Integer,
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String, nullable=False)
    description = Column(String(256), nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, onupdate=func.now())
    is_versioned = Column(Boolean, nullable=False, server_default="f")
    allow_duplicates = Column(Boolean, nullable=False, server_default="t")
    unique_key_names = Column(JSONB, nullable=False, server_default="[]")
    unique_key_types = Column(JSONB, nullable=False, server_default="[]")
    auto_counting = Column(JSONB, nullable=False, server_default="{}")
    foreign_keys = Column(JSONB, nullable=False, server_default="[]")
    current_commit_hash = Column(String, nullable=True)

    project = relationship("Project", back_populates="contexts")
    log_events = relationship(
        "LogEvent",
        secondary="log_event_context",
        back_populates="contexts",
        passive_deletes=True,
    )

    @property
    def unique_keys(self):
        """Reconstruct unique_keys dict from the separate arrays."""
        if not self.unique_key_names or not self.unique_key_types:
            return {}
        return dict(zip(self.unique_key_names, self.unique_key_types))

    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_project_context_name"),
        sa.CheckConstraint(
            "char_length(description) <= 256",
            name="ck_context_description_len",
        ),
    )


class ContextVersion(Base):
    """Model class for storing historical versions of contexts."""

    __tablename__ = "context_version"

    id = Column(Integer, primary_key=True)
    context_id = Column(
        Integer,
        ForeignKey("context.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_version_id = Column(
        Integer,
        ForeignKey("project_version.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    name = Column(String, nullable=True)
    description = Column(String, nullable=True)
    archived_at = Column(TIMESTAMP, server_default=func.now())
    commit_hash = Column(String, nullable=False)
    prev_commit_hash = Column(String, nullable=True)
    next_commit_hash = Column(JSONB, nullable=False, server_default="[]")
    commit_message = Column(String, nullable=True)

    # Relationship to its ProjectVersion
    project_version = relationship("ProjectVersion", back_populates="context_versions")
    # Relationship to its LogEventVersion snapshots (JSONB mode)
    log_event_versions = relationship(
        "LogEventVersion",
        back_populates="context_version",
        cascade="all, delete-orphan",
    )


class LogEvent(Base):
    __tablename__ = "log_event"

    id = Column(Integer, primary_key=True)
    project_id = Column(
        Integer,
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    data = Column(JSONB, nullable=False, server_default=text("'{}'"))
    # Stores original insertion order of nested dictionary keys
    # Structure: {"_root": ["key1", "key2"], "key1.nested": ["a", "b"]}
    key_order = Column(JSONB, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, onupdate=func.now())
    # Relationships
    contexts = relationship(
        "Context",
        secondary="log_event_context",
        back_populates="log_events",
        passive_deletes=True,
    )

    __table_args__ = (
        Index("idx_log_event_project_id_id", "project_id", "id"),
        # GIN index for JSON field filtering
        Index("idx_log_event_data", "data", postgresql_using="gin"),
    )


class LogEventVersion(Base):
    """Model class for storing JSONB snapshots of log events for versioning.

    Stores complete JSONB document snapshots of log events for versioning.
    """

    __tablename__ = "log_event_version"

    id = Column(Integer, primary_key=True)
    context_version_id = Column(
        Integer,
        ForeignKey("context_version.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Original log_event_id for reference (not a FK since original may be deleted)
    log_event_id = Column(Integer, nullable=False, index=True)
    # Snapshot of the JSONB data column
    data = Column(JSONB, nullable=False)
    # Snapshot of the key_order column for preserving dict ordering
    key_order = Column(JSONB, nullable=True)
    # Timestamps from the original LogEvent
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)

    # Relationship back to ContextVersion
    context_version = relationship(
        "ContextVersion",
        back_populates="log_event_versions",
    )


class ActiveDerivedLog(Base):
    """Model class for storing filter-based derived logs that are applied to future base logs."""

    __tablename__ = "active_derived_log_template"

    id = Column(Integer, primary_key=True)
    project_id = Column(
        Integer,
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    context_id = Column(
        Integer,
        ForeignKey("context.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    key = Column(String, nullable=False, index=True)
    equation = Column(String, nullable=False)
    referenced_logs = Column(JSONB, nullable=False)
    filter_expression = Column(JSONB, nullable=False)
    inferred_type = Column(String)
    # Array of base field names this derived log depends on (e.g., ["score", "accuracy"])
    referenced_keys = Column(JSONB, nullable=True)
    is_active = Column(Boolean, nullable=False, server_default="t")
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, onupdate=func.now())

    __table_args__ = (UniqueConstraint("project_id", "context_id", "key"),)


class LogUniqueConstraint(Base):
    """
    Lookup table for efficient unique field validation.

    Replaces O(N×M) JSONB containment scans with O(M×log N) B-tree lookups
    for checking unique field constraints during log creation/update.

    Supports:
    - Single unique fields: field_name = 'row_id', value_hash = md5(value)
    - Composite keys: field_name = '__composite__', value_hash = md5(json(combo))
    """

    __tablename__ = "log_unique_constraint"

    context_id = Column(Integer, nullable=False)
    field_name = Column(String, nullable=False)
    value_hash = Column(String(32), nullable=False)  # MD5 hash of the value
    log_event_id = Column(
        BigInteger,
        ForeignKey("log_event.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at = Column(TIMESTAMP, server_default=func.now())

    __table_args__ = (
        sa.PrimaryKeyConstraint("context_id", "field_name", "value_hash"),
        Index("idx_log_unique_constraint_log_event", "log_event_id"),
    )


class Interface(Base):
    __tablename__ = "interface"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    # TODO: remove both <user_id> and <organization_id>
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        index=True,
    )
    organization_id = Column(
        Integer,
        ForeignKey("organization.id", ondelete="CASCADE"),
        index=True,
    )
    name = Column(String(), nullable=False)
    new_counter = Column(Integer, nullable=False)
    items = Column(String(), nullable=False)
    project_id = Column(
        Integer,
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    context = Column(String(), nullable=True)
    icon = Column(String(), nullable=False, server_default="folder")
    color = Column(String(), nullable=True)
    order = Column(Integer, nullable=False, server_default="0")
    # Flag to indicate if this is a checkpoint (manual save) or auto-save
    is_checkpoint = Column(Boolean(), nullable=False, server_default="f")
    # ID of the checkpoint counterpart (if this is the active version)
    # or the active counterpart (if this is a checkpoint)
    checkpoint_or_active_id = Column(String, nullable=True)
    created_at = Column(TIMESTAMP, nullable=True, server_default=func.now())
    updated_at = Column(TIMESTAMP, onupdate=func.now())
    active_tab_id = Column(String, nullable=True)
    # Relationships
    project = relationship("Project", back_populates="interfaces")
    user = relationship("User", back_populates="interfaces")
    organization = relationship("Organization", back_populates="interfaces")
    tabs = relationship(
        "Tab",
        back_populates="interface",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "project_id",
            "name",
            "is_checkpoint",
            name="it_uq_project_name_checkpoint",
        ),
    )


class FieldType(Base):
    """Model class for the field_type table."""

    __tablename__ = "field_type"

    id = Column(Integer, primary_key=True)
    project_id = Column(
        Integer,
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
    )
    context_id = Column(
        Integer,
        ForeignKey("context.id", ondelete="CASCADE"),
        nullable=True,
    )
    field_name = Column(String, nullable=False)
    field_type = Column(String, nullable=False)
    field_category = Column(
        String,
        nullable=False,
        server_default="entry",
    )  # entry, param, derived_entry
    mutable = Column(Boolean(), nullable=False, server_default="t")  # type: ignore
    unique = Column(Boolean(), nullable=False, server_default="f")  # type: ignore
    enum_values = Column(JSONB, nullable=False, server_default=text("'[]'"))
    enum_restrict = Column(Boolean(), nullable=False, server_default="false")
    description = Column(String(256), nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "field_name",
            "context_id",
            name="uq_project_field_name_context_id",
        ),
        sa.CheckConstraint(
            "char_length(description) <= 256",
            name="ck_field_type_description_len",
        ),
    )


class AdminUser(Base):
    """Model class for admin users who have special privileges."""

    __tablename__ = "admin_user"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, onupdate=func.now())

    # Relationship to User
    user = relationship("User", backref="admin_user")


class FavoriteProject(Base):
    """Model class for user's favorite projects."""

    __tablename__ = "favorite_project"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id = Column(
        Integer,
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
    )
    position = Column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "project_id", name="uq_user_favorite_project"),
    )


class DemoAssistantMeta(Base):
    """Model class for demo assistant metadata.

    Stores metadata about demo assistants created by Unify employees
    for demonstrating the product to prospects. Each demo assistant
    has a corresponding entry in this table linked via demo_id.
    """

    __tablename__ = "demo_assistant_meta"

    id = Column(Integer, primary_key=True)
    source_assistant_id = Column(
        Integer,
        ForeignKey("assistants.agent_id", ondelete="SET NULL"),
        nullable=True,
    )
    demoer_user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    label = Column(String, nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now())

    # Optional prospect details (for pre-populating boss contact in Unity)
    prospect_first_name = Column(String, nullable=True)
    prospect_surname = Column(String, nullable=True)
    prospect_email = Column(String, nullable=True)
    prospect_phone = Column(String, nullable=True)

    # Relationships
    demoer = relationship(
        "User",
        foreign_keys=[demoer_user_id],
        backref="created_demos",
    )


class UserDesktop(Base):
    """Registered user desktop machines.

    Each row represents a physical/virtual desktop that a user's desktop app
    has registered after obtaining a public hostname via the tunnel service.
    """

    __tablename__ = "user_desktops"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String, nullable=False)
    url = Column(String, nullable=False)
    os = Column(String, nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        sa.CheckConstraint(
            "os IN ('ubuntu', 'windows', 'macos')",
            name="ck_user_desktop_os",
        ),
    )


class Assistant(Base):
    """Model class for the assistants table.

    Assistants can be either personal (user_id set, organization_id NULL)
    or organizational (organization_id set, user_id is the creator).
    """

    __tablename__ = "assistants"

    agent_id = Column(Integer, primary_key=True)
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_id = Column(
        Integer,
        ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    first_name = Column(String, nullable=True)
    surname = Column(String, nullable=True)
    age = Column(Integer, nullable=True)
    nationality = Column(String, nullable=True)
    profile_photo = Column(String, nullable=True)
    profile_video = Column(String, nullable=True)
    desktop_url = Column(String, nullable=True)
    desktop_mode = Column(String, nullable=True)
    user_desktop_id = Column(
        Integer,
        ForeignKey("user_desktops.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
    )
    user_desktop_filesys_sync = Column(Boolean, nullable=False, default=False)
    about = Column(String, nullable=True)
    phone_country = Column(String, nullable=True)
    timezone = Column(String, nullable=True)
    weekly_limit = Column(Numeric, nullable=True)
    # Monthly spending limit for this assistant (NULL = no limit)
    # Cannot exceed the user's monthly_spending_cap
    monthly_spending_cap = Column(Numeric, nullable=True)
    # When the spending cap was last changed (for notification deduplication)
    monthly_spending_cap_set_at = Column(TIMESTAMP(timezone=True), nullable=True)
    max_parallel = Column(Integer, nullable=True)
    email = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    user_phone = Column(String, nullable=True)
    user_whatsapp_number = Column(String, nullable=True)
    assistant_whatsapp_number = Column(String, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())
    voice_id = sa.Column(
        sa.String,
        nullable=True,
        index=True,
    )
    voice_provider = Column(String, nullable=True)
    voice_mode = Column(String, nullable=True)

    # Demo assistant metadata FK (NULL for regular assistants)
    demo_id = Column(
        Integer,
        ForeignKey("demo_assistant_meta.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Relationship to demo metadata
    demo_meta = relationship(
        "DemoAssistantMeta",
        backref=backref("assistant", uselist=False),
        foreign_keys=[demo_id],
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "voice_id", "voice_provider"],
            ["voices.user_id", "voices.voice_id", "voices.provider"],
            name="fk_assistants_voices",
        ),
        # Personal assistants: unique per user
        UniqueConstraint(
            "user_id",
            "first_name",
            "surname",
            name="uq_user_assistant_name",
        ),
        # Org assistants: unique per organization
        UniqueConstraint(
            "organization_id",
            "first_name",
            "surname",
            name="uq_org_assistant_name",
        ),
        sa.CheckConstraint(
            "desktop_mode IN ('ubuntu', 'windows', 'macos')",
            name="ck_assistant_desktop_mode",
        ),
        sa.CheckConstraint(
            "voice_mode IN ('tts', 'sts')",
            name="ck_assistant_voice_mode",
        ),
    )


class AssistantSecret(Base):
    """Model class for storing secrets associated with assistants.

    Secrets are external service credentials (API keys, tokens, etc.) that
    an assistant needs to access external services on behalf of the user.
    """

    __tablename__ = "assistant_secrets"

    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
        index=True,
    )
    agent_id = Column(
        Integer,
        ForeignKey("assistants.agent_id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
        index=True,
    )
    secret_name = Column(
        String,
        primary_key=True,
        nullable=False,
    )
    secret_value = Column(String, nullable=False)
    description = Column(String, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    __table_args__ = (sa.PrimaryKeyConstraint("user_id", "agent_id", "secret_name"),)


class OneTimeCreditGrantLink(Base):
    """
    One-time links that grant credits when claimed.

    Each link can only be claimed once. When claimed, the user receives
    the specified credit_amount. Users can only benefit from one link
    ever (checked via query on this table's user_id column).
    """

    __tablename__ = "one_time_credit_grant_link"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    token = Column(String, unique=True, index=True, nullable=False)
    expires_at = Column(TIMESTAMP(timezone=True), nullable=False)
    user_id = Column(String, ForeignKey("user.id"), nullable=True, index=True)
    claimed_at = Column(TIMESTAMP(timezone=True), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    credit_amount = Column(
        Float,
        nullable=False,
        default=10.0,
        comment="Amount of credits to grant when link is claimed",
    )


class OnboardingStatus(Base):
    """
    Tracks user onboarding progress.

    The current_step represents WHERE TO RESUME next time:
    - account_setup: User needs to complete account setup (initial state)
    - billing_setup: Account done, user needs to add payment method
    - completed: All onboarding steps done

    step_data accumulates information from completed steps:
    - selected_type: "personal" | "business"
    - organization_id, organization_name (if business)
    - billing_skipped, payment_method_added (after billing step)
    - completed_at (when completed)
    """

    __tablename__ = "onboarding_status"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    current_step = Column(
        String(50),
        nullable=False,
        # No server_default - handled by DAO to avoid migrations when flow changes
        comment="Next step to resume at (freeform in DB, enforced by API)",
    )
    step_data = Column(
        JSONB,
        nullable=False,
        server_default="{}",
        comment="Accumulated data from completed steps (freeform JSON)",
    )
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    # Relationship
    user = relationship("User", backref=backref("onboarding_status", uselist=False))


class Voice(Base):
    """Model class for the assistants voices table."""

    __tablename__ = "voices"

    voice_id = Column(
        String,
        primary_key=True,
    )  # This will store the TTS provider's voice ID
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
        index=True,
    )
    provider = Column(String, primary_key=True, nullable=False)
    name = Column(String, nullable=False)
    description = Column(String, nullable=False)
    gender = Column(String, nullable=True)
    language = Column(String, nullable=False)  # e.g., "en", "es"
    is_preset = Column(
        Boolean,
        nullable=False,
        server_default="f",
    )  # True if this is a Cartesia preset voice

    __table_args__ = (
        sa.PrimaryKeyConstraint("user_id", "voice_id", "provider"),
        sa.CheckConstraint(
            "provider IN ('cartesia', 'elevenlabs', 'openai')",
            name="ck_voice_provider",
        ),
    )


class Tab(Base):
    """Model class for tabs within interfaces."""

    __tablename__ = "tab"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    interface_id = Column(
        String,
        ForeignKey("interface.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String(), nullable=False)
    icon = Column(String(), nullable=False, server_default="tab")
    visible = Column(Boolean(), nullable=False, server_default="t")
    active = Column(Boolean(), nullable=False, server_default="f")
    order = Column(Integer, nullable=False, server_default="0")
    context = Column(String(), nullable=True)
    color = Column(String(), nullable=True)
    # Flag to indicate if this is a checkpoint (manual save) or auto-save
    is_checkpoint = Column(Boolean(), nullable=False, server_default="f")
    # ID of the checkpoint counterpart (if this is the active version)
    # or the active counterpart (if this is a checkpoint)
    checkpoint_or_active_id = Column(String, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, onupdate=func.now())

    # Relationships
    interface = relationship("Interface", back_populates="tabs")
    tiles = relationship(
        "Tile",
        back_populates="tab",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "interface_id",
            "name",
            "is_checkpoint",
            name="tab_uq_interface_name_checkpoint",
        ),
    )


class Tile(Base):
    """Model class for tiles within tabs."""

    __tablename__ = "tile"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tab_id = Column(
        String,
        ForeignKey("tab.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String(), nullable=False)
    type = Column(
        String(),
        nullable=True,
    )  # "Table", "Plot", "View", "Editor", "Terminal"

    # Position properties
    x_position = Column(Float, nullable=False)
    y_position = Column(Float, nullable=False)
    width = Column(Float, nullable=False)
    height = Column(Float, nullable=False)
    minW = Column(Float, nullable=True)
    minH = Column(Float, nullable=True)

    # Common properties
    visible = Column(Boolean(), nullable=False, server_default="t")
    locked = Column(Boolean(), nullable=False, server_default="f")
    moved = Column(Boolean(), nullable=False, server_default="f")
    static = Column(Boolean(), nullable=False, server_default="f")
    color = Column(String(), nullable=True)

    # Common data properties
    context = Column(String(), nullable=True)
    table = Column(String(), nullable=True)
    auto_update = Column(String(), nullable=True)
    freeze = Column(String(), nullable=True)
    filters = Column(String(), nullable=True)
    common_filter = Column(String(), nullable=True)
    metric = Column(String(), nullable=True)
    column_context = Column(String(), nullable=True)
    grouping = Column(String(), nullable=True)

    # Flag to indicate if this is a checkpoint (manual save) or auto-save
    is_checkpoint = Column(Boolean(), nullable=False, server_default="f")
    # ID of the checkpoint counterpart (if this is the active version)
    # or the active counterpart (if this is a checkpoint)
    checkpoint_or_active_id = Column(String, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, onupdate=func.now())

    # Relationships
    tab = relationship("Tab", back_populates="tiles")
    table_tile = relationship(
        "TableTile",
        back_populates="tile",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
    plot_tile = relationship(
        "PlotTile",
        back_populates="tile",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
    view_tile = relationship(
        "ViewTile",
        back_populates="tile",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
    editor_tile = relationship(
        "EditorTile",
        back_populates="tile",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
    terminal_tile = relationship(
        "TerminalTile",
        back_populates="tile",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "tab_id",
            "name",
            "is_checkpoint",
            name="tile_uq_tab_name_checkpoint",
        ),
    )


class TableTile(Base):
    """Model class for Table-specific tile properties."""

    __tablename__ = "table_tile"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tile_id = Column(
        String,
        ForeignKey("tile.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    # Table-specific properties
    table_type = Column(String(), nullable=True)
    page_number = Column(String(), nullable=True)
    column_order = Column(String(), nullable=True)
    hidden_columns = Column(String(), nullable=True)
    default_hidden_columns = Column(Boolean(), nullable=False, server_default="t")
    sorting = Column(String(), nullable=True)
    group_sorting = Column(String(), nullable=True)
    columns_pin_left = Column(String(), nullable=True)
    columns_pin_right = Column(String(), nullable=True)
    selected = Column(String(), nullable=True)

    # Relationships
    tile = relationship("Tile", back_populates="table_tile")


class PlotTile(Base):
    """Model class for Plot-specific tile properties."""

    __tablename__ = "plot_tile"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tile_id = Column(
        String,
        ForeignKey("tile.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    # Plot-specific properties
    plot_type = Column(String(), nullable=True)
    plot_scale_x = Column(String(), nullable=True)
    plot_scale_y = Column(String(), nullable=True)
    plot_aggregate = Column(String(), nullable=True)
    x_axis = Column(String(), nullable=True)
    y_axis = Column(String(), nullable=True)
    plot_group_by = Column(String(), nullable=True)
    plot_group_by_colors = Column(String(), nullable=True)
    bin_count = Column(String(), nullable=True)
    regression_line = Column(String(), nullable=True)

    # Relationships
    tile = relationship("Tile", back_populates="plot_tile")


class ViewTile(Base):
    """Model class for View-specific tile properties."""

    __tablename__ = "view_tile"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tile_id = Column(
        String,
        ForeignKey("tile.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    # View-specific properties
    base_index = Column(String(), nullable=True)

    # Relationships
    tile = relationship("Tile", back_populates="view_tile")


class EditorTile(Base):
    """Model class for Editor-specific tile properties."""

    __tablename__ = "editor_tile"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tile_id = Column(
        String,
        ForeignKey("tile.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    # Editor-specific properties
    file_name = Column(String(), nullable=True)
    file_type = Column(String(), nullable=True)
    content = Column(String(), nullable=True)

    # Relationships
    tile = relationship("Tile", back_populates="editor_tile")


class TerminalTile(Base):
    """Model class for Terminal-specific tile properties."""

    __tablename__ = "terminal_tile"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tile_id = Column(
        String,
        ForeignKey("tile.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    # Terminal-specific properties
    shell_type = Column(String(), nullable=True)

    # Relationships
    tile = relationship("Tile", back_populates="terminal_tile")


class Embedding(Base):
    """Model class for the embedding table that stores embeddings.

    Supports soft-delete via the `is_deleted` column to avoid expensive HNSW index
    surgery during deletions. When embeddings are "deleted", they are marked with
    is_deleted=True rather than being physically removed.

    The HNSW indexes include `AND is_deleted = false` to exclude soft-deleted rows,
    ensuring they don't participate in vector similarity searches.
    """

    __tablename__ = "embedding"

    id = Column(Integer, primary_key=True)
    # ref_id uses SET NULL instead of CASCADE to preserve soft-deleted embeddings.
    # When a LogEvent is deleted, ref_id becomes NULL but the embedding row stays
    # until index maintenance cleans it up (avoiding HNSW index surgery on delete).
    ref_id = Column(
        Integer,
        ForeignKey("log_event.id", ondelete="SET NULL"),
        nullable=True,
    )
    model = Column(String, nullable=False)
    key = Column(String, nullable=False)
    vector = Column(Vector(), nullable=False)  # Support variable dimensions
    created_at = Column(TIMESTAMP, server_default=func.now())
    # Soft-delete flag for instant deletion without HNSW index surgery
    is_deleted = Column(Boolean, nullable=False, server_default=sa.text("false"))

    __table_args__ = (
        UniqueConstraint("ref_id", "model", "key", name="uq_embedding"),
        Index(
            "idx_embedding_ref",
            "ref_id",
            "model",
            "key",
        ),
        # B-tree index on is_deleted for efficient filtering
        Index("idx_embedding_is_deleted", "is_deleted"),
        # Composite index for deletion queries (filter by ref_id and is_deleted)
        Index("idx_embedding_ref_id_is_deleted", "ref_id", "is_deleted"),
        # CHECK constraints to ensure dimension integrity per model
        # Prevents dimension mismatches from corrupting the HNSW indexes
        sa.CheckConstraint(
            "model <> 'text-embedding-3-small' OR vector_dims(vector) = 1536",
            name="embedding_dims_text_openai_chk",
        ),
        sa.CheckConstraint(
            "model <> 'multimodalembedding@001' OR vector_dims(vector) = 1408",
            name="embedding_dims_vertexai_chk",
        ),
        # Model-specific HNSW expression indexes with dimension casts
        # The cast is critical - queries must also cast to use these indexes
        # Pattern: (vector::vector(N)) vector_cosine_ops for expression index + WHERE model = '...' for partial index
        # HNSW indexes exclude soft-deleted embeddings for performance
        # OpenAI text-embedding-3-small (1536 dimensions) - Cosine similarity
        Index(
            "embedding_hnsw_cosine_openai_1536_idx",
            sa.text(
                "(vector::vector(1536)) vector_cosine_ops",
            ),  # Include operator class in expression
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_where=sa.text(
                "model = 'text-embedding-3-small' AND is_deleted = false",
            ),
        ),
        # Vertex AI multimodalembedding@001 (1408 dimensions) - Cosine similarity
        Index(
            "embedding_hnsw_cosine_vertexai_1408_idx",
            sa.text(
                "(vector::vector(1408)) vector_cosine_ops",
            ),  # Include operator class in expression
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_where=sa.text(
                "model = 'multimodalembedding@001' AND is_deleted = false",
            ),
        ),
    )


class EmbeddingQueue(Base):
    """Queue for async embedding generation with two-stage processing pipeline.

    Embeddings are queued during log creation and processed by background workers
    in two stages to maximize throughput:

    Stage 1 (parallel-safe): Generate embedding vectors
    - Multiple workers can run concurrently using FOR UPDATE SKIP LOCKED
    - pending → generating → vector_ready

    Stage 2 (serial): Bulk insert into indexed Embedding table
    - Single worker for optimal HNSW index performance
    - vector_ready → inserting → (deleted from queue)

    Status values:
    - pending: Waiting for Stage 1 processing
    - generating: Being processed by Stage 1 worker (vector generation)
    - vector_ready: Vector generated, awaiting Stage 2 (index insertion)
    - inserting: Being processed by Stage 2 worker (bulk insert)
    - completed: Successfully processed (will be deleted from queue)
    - failed: Failed after max retries (kept for debugging)

    TODO: Migrate Cloud Scheduler jobs to Cloud Tasks for dynamic scaling
    based on queue depth rather than fixed scheduling intervals.
    """

    __tablename__ = "embedding_queue"

    id = Column(Integer, primary_key=True)
    ref_id = Column(
        Integer,
        ForeignKey("log_event.id", ondelete="CASCADE"),
        nullable=False,
    )
    key = Column(String, nullable=False)
    text = Column(String, nullable=False)  # Text to embed
    model = Column(String, nullable=False)
    dimensions = Column(Integer, nullable=True)
    status = Column(String, nullable=False, server_default="pending")
    retry_count = Column(Integer, nullable=False, server_default=sa.text("0"))
    error_message = Column(String, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    # Timestamp when item was claimed for processing (used for stale detection)
    processing_started_at = Column(TIMESTAMP, nullable=True)

    # Stage 1 output: Generated embedding vector (stored here until Stage 2 inserts it)
    generated_vector = Column(Vector(), nullable=True)
    # Timestamp when vector was generated (for monitoring/debugging)
    vector_generated_at = Column(TIMESTAMP, nullable=True)

    __table_args__ = (
        UniqueConstraint("ref_id", "key", "model", name="uq_embedding_queue"),
        sa.CheckConstraint(
            "status IN ('pending', 'generating', 'vector_ready', 'inserting', 'completed', 'failed')",
            name="chk_embedding_queue_status",
        ),
        Index("idx_embedding_queue_status_created", "status", "created_at"),
        Index("idx_embedding_queue_ref_id", "ref_id"),
        # Index for efficient stale processing detection
        Index(
            "idx_embedding_queue_processing_started",
            "status",
            "processing_started_at",
        ),
        # Index for Stage 2 worker to efficiently find vector_ready items
        Index(
            "idx_embedding_queue_vector_ready",
            "created_at",
            postgresql_where=sa.text("status = 'vector_ready'"),
        ),
    )


class Plot(Base):
    """Model class for shareable plot configurations.

    Plots are linked to projects and follow project-based access control.
    When a project is deleted, all associated plots are cascade deleted.
    """

    __tablename__ = "plot"

    id = Column(Integer, primary_key=True)
    token = Column(String(12), unique=True, nullable=False, index=True)
    project_id = Column(
        Integer,
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_id = Column(
        Integer,
        ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    title = Column(String, nullable=True)
    plot_config = Column(JSONB, nullable=False)
    project_config = Column(JSONB, nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    # Relationships - passive_deletes=True lets the DB handle CASCADE DELETE
    project = relationship("Project", backref=backref("plots", passive_deletes=True))

    __table_args__ = (
        Index("idx_plot_project_id", "project_id"),
        Index("idx_plot_user_id", "user_id"),
        Index("idx_plot_organization_id", "organization_id"),
    )


class TableView(Base):
    """Model class for shareable table view configurations.

    TableViews are linked to projects and follow project-based access control.
    When a project is deleted, all associated table views are cascade deleted.
    """

    __tablename__ = "table_view"

    id = Column(Integer, primary_key=True)
    token = Column(String(12), unique=True, nullable=False, index=True)
    project_id = Column(
        Integer,
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_id = Column(
        Integer,
        ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    title = Column(String, nullable=True)
    table_config = Column(JSONB, nullable=False)
    project_config = Column(JSONB, nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    # Relationships - passive_deletes=True lets the DB handle CASCADE DELETE
    project = relationship(
        "Project",
        backref=backref("table_views", passive_deletes=True),
    )

    __table_args__ = (
        Index("idx_table_view_project_id", "project_id"),
        Index("idx_table_view_user_id", "user_id"),
        Index("idx_table_view_organization_id", "organization_id"),
    )


class SpendingLimitNotification(Base):
    """
    Tracks spending limit notifications to prevent duplicate emails.

    When a spending limit is reached, we record the notification here.
    Subsequent limit breaches for the same (entity_type, entity_id, month, limit_value)
    are deduplicated unless the limit was re-configured (limit_set_at > notified_at).

    This table is intentionally NOT linked via foreign keys to entity tables
    so that notification records are preserved when entities are deleted (audit trail).
    """

    __tablename__ = "spending_limit_notifications"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Which entity hit the limit
    entity_type = Column(
        String(20),
        nullable=False,
        comment="'assistant', 'user', 'member', or 'organization'",
    )
    entity_id = Column(
        String,
        nullable=False,
        comment="ID of the entity (agent_id, user_id, or org_id)",
    )

    # When and at what limit
    month = Column(
        String(7),
        nullable=False,
        comment="Billing month in YYYY-MM format",
    )
    limit_value = Column(
        Numeric,
        nullable=False,
        comment="The limit value that was reached",
    )

    # When the limit was configured (for re-enable detection)
    limit_set_at = Column(
        TIMESTAMP(timezone=True),
        nullable=True,
        comment="When the limit was configured",
    )

    # Notification metadata
    notified_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    notified_user_ids = Column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
        comment="List of user IDs who received the notification email",
    )

    # Entity name for auditing (may become stale if entity renamed)
    entity_name = Column(
        String,
        nullable=True,
        comment="Name of the entity at time of notification (for auditing)",
    )

    # Current spend at time of notification (for auditing)
    current_spend = Column(
        Numeric,
        nullable=True,
        comment="Spend amount when notification was triggered",
    )

    __table_args__ = (
        # Index for deduplication lookups
        Index(
            "ix_spending_limit_notifications_dedupe",
            "entity_type",
            "entity_id",
            "month",
            "limit_value",
        ),
        # Index for entity lookups
        Index(
            "ix_spending_limit_notifications_entity",
            "entity_type",
            "entity_id",
        ),
        # Index for cleanup queries
        Index(
            "ix_spending_limit_notifications_month",
            "month",
        ),
    )


class RateLimitCounter(Base):
    """
    Tracks API request counts in 5-minute time buckets for rate limiting.

    This table replaces the previous approval-based gating with a flexible
    rate limiting system. It supports:
    - Category-based limits (assistant_hiring, assistant_media, assistant_crud, assistant_voice)
    - Optional per-endpoint overrides
    - User-level and organization-level (shared) limits
    - Rolling 24-hour window calculation
    """

    __tablename__ = "rate_limit_counter"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Who made the request
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_id = Column(
        Integer,
        ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # What endpoint/category
    endpoint_category = Column(
        String(50),
        nullable=False,
        comment="Rate limit category: 'assistant_hiring', 'assistant_media', 'assistant_crud', 'assistant_voice'",
    )
    endpoint_path = Column(
        String(200),
        nullable=True,
        comment="Specific endpoint path for per-endpoint overrides",
    )

    # When (5-minute buckets)
    time_bucket = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        comment="Start of the 5-minute time bucket",
    )

    # Request count
    request_count = Column(
        Integer,
        nullable=False,
        server_default="1",
        comment="Number of requests in this bucket",
    )

    __table_args__ = (
        # Unique constraint for upsert operations
        UniqueConstraint(
            "user_id",
            "endpoint_category",
            "endpoint_path",
            "time_bucket",
            name="uq_rate_limit_counter",
        ),
        # Index for user + category lookups
        Index(
            "ix_rate_limit_counter_user_category",
            "user_id",
            "endpoint_category",
            "time_bucket",
        ),
        # Index for organization-level lookups
        Index(
            "ix_rate_limit_counter_org_category",
            "organization_id",
            "endpoint_category",
            "time_bucket",
        ),
        # Index for endpoint-specific lookups
        Index(
            "ix_rate_limit_counter_endpoint",
            "user_id",
            "endpoint_path",
            "time_bucket",
        ),
        # Index for cleanup queries
        Index(
            "ix_rate_limit_counter_time_bucket",
            "time_bucket",
        ),
    )
