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
    )  # ACTIVE, SUSPENDED, CLOSED
    suspension_reason = Column(
        String,
        nullable=True,
        default=None,
    )  # dispute, admin_freeze — NULL when ACTIVE
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
    __table_args__ = (
        sa.CheckConstraint(
            "account_status IN ('ACTIVE', 'SUSPENDED', 'CLOSED')",
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
    whatsapp_number = Column(String, nullable=True)
    discord_id = Column(String, nullable=True)

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

    __table_args__ = (
        Index(
            "uq_user_whatsapp_number",
            "whatsapp_number",
            unique=True,
            postgresql_where=text("whatsapp_number IS NOT NULL"),
        ),
        Index(
            "uq_user_discord_id",
            "discord_id",
            unique=True,
            postgresql_where=text("discord_id IS NOT NULL"),
        ),
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


class EmailAccount(Base):
    """
    Email/password credentials for a user.

    Users who only use OAuth will have no row here. One row per user maximum.
    The email address itself is not duplicated — it is always read from User.email.
    """

    __tablename__ = "email_account"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    password_hash = Column(String, nullable=False)  # argon2id hash
    email_verified = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )  # Safety-net default; set to True at creation after verification
    password_changed_at = Column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )  # Set on every password change; used for session invalidation
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, onupdate=func.now())

    # ORM relationship
    user = relationship("User", backref=backref("email_account", uselist=False))


class EmailVerification(Base):
    """
    Short-lived verification codes for signup and password reset.

    During signup, this table also serves as temporary storage for the user's
    credentials until their email is verified — no User or EmailAccount row is
    created until verification succeeds.

    Row lifecycle: rows are always deleted on success (both signup and password
    reset). Expired rows are cleaned up by a periodic job.
    """

    __tablename__ = "email_verification"

    id = Column(Integer, primary_key=True)
    email = Column(
        String,
        nullable=False,
        index=True,
    )  # Not a FK — user may not exist yet (signup)
    code_hash = Column(String, nullable=False)  # SHA-256 hash of the 6-digit code
    purpose = Column(String, nullable=False)  # "signup" | "password_reset"
    password_hash = Column(
        String,
        nullable=True,
    )  # argon2id hash — only for purpose="signup"
    name = Column(String, nullable=True)  # User's first name — only for signup
    last_name = Column(String, nullable=True)  # User's last name — only for signup
    expires_at = Column(TIMESTAMP(timezone=True), nullable=False)
    attempts = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )  # Max 5 attempts before invalidation
    token_jti = Column(String, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())


class PhoneVerification(Base):
    """
    Short-lived verification codes for phone / WhatsApp number ownership.

    When a user wants to add or change their phone_number or whatsapp_number
    on the User table, they must first verify ownership via SMS.  The flow:

    1. ``POST /user/phone/send-verification`` creates a row with a hashed
       6-digit code and sends the SMS via the communication service.
    2. ``POST /user/phone/confirm-verification`` checks the code, and on
       success sets ``verified_at``.
    3. ``PUT /user`` (profile update) checks for a recent verified row
       matching the new number before accepting the change.

    Rows are deleted after successful profile update or by a periodic
    cleanup job for expired entries.
    """

    __tablename__ = "phone_verifications"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    phone_number = Column(String, nullable=False)
    phone_type = Column(String, nullable=False)  # "phone" | "whatsapp"
    code_hash = Column(String, nullable=False)
    expires_at = Column(TIMESTAMP(timezone=True), nullable=False)
    attempts = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    verified_at = Column(TIMESTAMP(timezone=True), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


class MFACredential(Base):
    """
    Polymorphic MFA credential.

    For TOTP, one row per user (the same secret can be scanned into multiple
    authenticator apps). For WebAuthn (future), one row per registered device.

    ``credential_data`` is an encrypted JSON blob whose structure depends on
    ``method_type`` (e.g. ``{"secret": "BASE32..."}`` for TOTP).
    """

    __tablename__ = "mfa_credential"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    method_type = Column(String, nullable=False)  # "totp", "webauthn", "sms"
    credential_data = Column(sa.LargeBinary, nullable=False)  # Encrypted JSON blob
    enabled = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    created_at = Column(TIMESTAMP, server_default=func.now())
    confirmed_at = Column(TIMESTAMP(timezone=True), nullable=True)
    last_used_at = Column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (Index("ix_mfa_credential_user_type", "user_id", "method_type"),)

    # ORM relationship
    user = relationship("User", backref=backref("mfa_credentials", lazy="dynamic"))


class MFARecovery(Base):
    """
    Recovery codes for MFA.

    Tied to the user (not to a specific MFA method). 10 codes generated
    per setup, each 8 alphanumeric characters. Stored as SHA-256 hashes.
    Each code is single-use.
    """

    __tablename__ = "mfa_recovery"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    code_hash = Column(String, nullable=False)  # SHA-256 hash
    used = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    used_at = Column(TIMESTAMP(timezone=True), nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())

    # ORM relationship
    user = relationship("User", backref=backref("mfa_recovery_codes", lazy="dynamic"))


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

    image = Column(String, nullable=True)

    # Timezone for org-level billing (IANA format, e.g., "America/New_York")
    # Initialized from owner's timezone on creation, defaults to UTC if not set
    timezone = Column(String, nullable=True)

    # Monthly spending limit for all users/assistants in the org (NULL = no limit)
    monthly_spending_cap = Column(Numeric, nullable=True)
    monthly_spending_cap_set_at = Column(TIMESTAMP(timezone=True), nullable=True)

    # === MFA ENFORCEMENT ===
    # When True, all email/password members must enable MFA to access this org
    require_mfa = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )

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

    # === FREE TRIAL ===
    free_trial = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
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

    __table_args__ = (Index("idx_log_event_context_context_id", "context_id"),)


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
    description = Column(String, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "field_name",
            "context_id",
            name="uq_project_field_name_context_id",
        ),
        Index("idx_field_type_context_id", "context_id"),
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

    Contact details (phone, email, WhatsApp) are stored in the
    ``assistant_contacts`` table (see :class:`AssistantContact`).
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
    desktop_mode = Column(String, nullable=True)
    desktop_filesync_sshkey = Column(String, nullable=True)
    user_desktop_id = Column(
        Integer,
        ForeignKey("user_desktops.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
    )
    user_desktop_filesys_sync = Column(Boolean, nullable=False, default=False)
    about = Column(String, nullable=True)
    timezone = Column(String, nullable=True)
    weekly_limit = Column(Numeric, nullable=True)
    # Monthly spending limit for this assistant (NULL = no limit)
    # Cannot exceed the user's monthly_spending_cap
    monthly_spending_cap = Column(Numeric, nullable=True)
    # When the spending cap was last changed (for notification deduplication)
    monthly_spending_cap_set_at = Column(TIMESTAMP(timezone=True), nullable=True)
    max_parallel = Column(Integer, nullable=True)
    deploy_env = Column(String, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())
    voice_id = sa.Column(
        sa.String,
        nullable=True,
        index=True,
    )
    voice_provider = Column(String, nullable=True)
    is_local = Column(Boolean, nullable=False, default=False, server_default="false")

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
        sa.CheckConstraint(
            "desktop_mode IN ('ubuntu', 'windows', 'macos')",
            name="ck_assistant_desktop_mode",
        ),
    )


class AssistantContact(Base):
    """Tracks provisioned contact details for assistants.

    Each row represents a single provisioned resource (phone, email, or
    WhatsApp sender) with metadata for billing and lifecycle management.

    Lifecycle statuses:
        active         – resource is provisioned and in use.
        grace_period   – billing account has insufficient credits; resource
                         stays active for up to 14 days while user tops up.
        deleted        – resource has been deprovisioned (soft-delete).
    """

    __tablename__ = "assistant_contacts"

    id = Column(Integer, primary_key=True)

    assistant_id = Column(
        Integer,
        ForeignKey("assistants.agent_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # "phone", "email", "whatsapp"
    contact_type = Column(String, nullable=False)

    # The actual provisioned value (E.164 phone, email address, WhatsApp number)
    contact_value = Column(String, nullable=False)

    # Provider used for provisioning: "twilio", "google_workspace", etc.
    provider = Column(String, nullable=True)

    # Who provisioned: "platform" (we manage it) vs "user" (BYOD – future)
    provisioned_by = Column(
        String,
        nullable=False,
        default="platform",
        server_default="platform",
    )

    # Country code for phone numbers (affects pricing lookups)
    country_code = Column(String, nullable=True)

    # Lifecycle status
    status = Column(
        String,
        nullable=False,
        default="active",
        server_default="active",
    )

    # Type-specific metadata (JSONB):
    #   phone:    {"sid": "PNxxx", "capabilities": {"voice": true, "sms": true}}
    #   email:    {"workspace_user_id": "...", "domain": "unify.ai"}
    #   whatsapp: {"messaging_service_sid": "MGxxx"}
    metadata_ = Column("metadata", JSONB, nullable=True, default=dict)

    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
    deleted_at = Column(TIMESTAMP(timezone=True), nullable=True)

    # When the grace period started (NULL if not in grace period)
    grace_period_started_at = Column(TIMESTAMP(timezone=True), nullable=True)

    # Last month billed (e.g. "2026-03") – prevents double-billing
    last_billed_month = Column(String, nullable=True)

    # Monthly cost in $ at time of last levy (audit trail)
    monthly_cost = Column(Numeric, nullable=True)

    # Relationship
    assistant = relationship(
        "Assistant",
        backref=backref("contacts", passive_deletes=True),
    )

    __table_args__ = (
        # One active contact of each type per assistant
        Index(
            "uq_assistant_contact_type_active",
            "assistant_id",
            "contact_type",
            unique=True,
            postgresql_where=text("status != 'deleted'"),
        ),
        # Prevent duplicate active contact values across all assistants
        # (excludes WhatsApp because pool numbers are shared)
        Index(
            "uq_active_contact_value",
            "contact_value",
            unique=True,
            postgresql_where=text(
                "status != 'deleted' AND contact_type NOT IN ('whatsapp', 'discord')",
            ),
        ),
        sa.CheckConstraint(
            "contact_type IN ('phone', 'email', 'whatsapp', 'discord')",
            name="ck_assistant_contact_type",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'grace_period', 'deleted')",
            name="ck_assistant_contact_status",
        ),
        sa.CheckConstraint(
            "provisioned_by IN ('platform', 'user')",
            name="ck_assistant_contact_provisioned_by",
        ),
    )


class AssistantContactCost(Base):
    """Monthly and one-time costs for each contact type + provider combination.

    Supports per-country pricing (phone numbers vary by country) and
    per-provider pricing (multiple providers per contact type in the future).
    """

    __tablename__ = "contact_type_costs"

    id = Column(Integer, primary_key=True)

    # "phone", "email", "whatsapp"
    contact_type = Column(String, nullable=False)

    # "twilio", "google_workspace", etc.  NULL = default for that type.
    provider = Column(String, nullable=True)

    # NULL = default pricing, "US", "GB", etc. for country-specific pricing.
    country_code = Column(String, nullable=True)

    # Monthly maintenance cost in $
    monthly_cost = Column(Numeric, nullable=False)

    # One-time setup fee in $
    one_time_cost = Column(
        Numeric,
        nullable=False,
        default=0,
        server_default="0",
    )

    effective_from = Column(TIMESTAMP(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "contact_type",
            "provider",
            "country_code",
            name="uq_contact_cost",
        ),
        sa.CheckConstraint(
            "contact_type IN ('phone', 'email', 'whatsapp', 'discord')",
            name="ck_contact_type_cost_type",
        ),
    )


class OneTimeCreditGrantLink(Base):
    """
    Credit grant links that award credits when claimed.

    A link can be single-use (max_claims=1, the default), multi-use
    (max_claims>1), or unlimited (max_claims=NULL) so that it can be
    shared on social media or with a group of prospective users.

    Credits are applied to the billing account that corresponds to the
    claimer's active workspace:
    - Personal API key → user's BillingAccount
    - Organization API key → organization's BillingAccount

    Guards:
    - Per-link budget: number of claims must stay below max_claims (if set).
    - Per-user lifetime: a user can only benefit from one link ever.
    - Per-org lifetime: an organization can only benefit from one link ever.
    """

    __tablename__ = "one_time_credit_grant_link"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    token = Column(String, unique=True, index=True, nullable=False)
    name = Column(
        String,
        nullable=True,
        comment="Optional admin-facing label (e.g. outreach channel or campaign)",
    )
    expires_at = Column(TIMESTAMP(timezone=True), nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    credit_amount = Column(
        Float,
        nullable=False,
        default=10.0,
        comment="Amount of credits to grant per claim",
    )
    max_claims = Column(
        Integer,
        nullable=True,
        comment="Maximum number of claims allowed (NULL = unlimited)",
    )

    claims = relationship(
        "CreditGrantLinkClaim",
        back_populates="link",
        cascade="all, delete-orphan",
    )


class CreditGrantLinkClaim(Base):
    """
    Records an individual claim against a credit grant link.

    Each row represents one user (or org) successfully redeeming a link.
    """

    __tablename__ = "credit_grant_link_claim"
    __table_args__ = (
        UniqueConstraint("link_id", "user_id", name="uq_claim_link_user"),
    )

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    link_id = Column(
        String,
        ForeignKey("one_time_credit_grant_link.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(String, ForeignKey("user.id"), nullable=False, index=True)
    organization_id = Column(
        Integer,
        ForeignKey("organization.id"),
        nullable=True,
        index=True,
        comment="Organization that received the credits (NULL = personal claim)",
    )
    claimed_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    link = relationship("OneTimeCreditGrantLink", back_populates="claims")


class OnboardingStatus(Base):
    """
    Tracks user onboarding progress.

    The current_step represents WHERE TO RESUME next time:
    - workspace_setup: Initial state – user needs to choose personal vs. organization workspace
    - completed: All onboarding steps done

    step_data accumulates information from completed steps:
    - selected_type: "personal" | "organization"
    - organization_id, organization_name (if organization)
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
    - cancelled: Deliberately stopped (e.g. parent project deleted)

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
            "status IN ('pending', 'generating', 'vector_ready', 'inserting', 'completed', 'failed', 'cancelled')",
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


class AuthRateLimitEntry(Base):
    """
    IP-based rate limiting for unauthenticated auth endpoints.

    Unlike RateLimitCounter (which keys on user_id), this table keys on
    a composite string of IP + identifier (email, user_id, or just IP)
    to throttle login attempts, MFA brute-force, registration spam, etc.
    """

    __tablename__ = "auth_rate_limit_entry"

    id = Column(Integer, primary_key=True, autoincrement=True)

    key = Column(
        String(500),
        nullable=False,
        index=True,
        comment="Composite key: 'ip:identifier' or just 'ip'",
    )
    endpoint_category = Column(
        String(50),
        nullable=False,
        comment="Auth rate limit category (auth_login, auth_mfa, auth_register, ...)",
    )
    time_bucket = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        comment="Start of the 5-minute time bucket",
    )
    attempt_count = Column(
        Integer,
        nullable=False,
        server_default="1",
    )

    __table_args__ = (
        UniqueConstraint(
            "key",
            "endpoint_category",
            "time_bucket",
            name="uq_auth_rate_limit_entry",
        ),
        Index(
            "ix_auth_rate_limit_key_category",
            "key",
            "endpoint_category",
            "time_bucket",
        ),
        Index(
            "ix_auth_rate_limit_time_bucket",
            "time_bucket",
        ),
    )


class ApiMessage(Base):
    """
    Tracks programmatic API messages sent to assistants.

    Each row represents a single request-response exchange: a developer sends a
    message via the REST API, and the assistant may (or may not) respond.
    The polling endpoint reads from this table.
    """

    __tablename__ = "api_messages"

    id = Column(String, primary_key=True)
    assistant_id = Column(
        Integer,
        ForeignKey("assistants.agent_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_id = Column(Integer, nullable=True)
    message = Column(String, nullable=False)
    status = Column(String, nullable=False, default="processing")
    response = Column(String, nullable=True)
    tags = Column(JSONB, nullable=True, server_default="[]")
    attachments = Column(JSONB, nullable=True, server_default="[]")
    response_tags = Column(JSONB, nullable=True)
    response_attachments = Column(JSONB, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    completed_at = Column(TIMESTAMP, nullable=True)


class SharedPoolNumber(Base):
    """Platform-owned contact identifiers shared across assistants.

    Each row represents a registered sender (e.g. a Twilio WhatsApp number,
    an Instagram bot account) that can be assigned to multiple assistants.
    The pool is small and managed at the platform level.
    """

    __tablename__ = "shared_pool_numbers"

    id = Column(Integer, primary_key=True)
    platform = Column(
        String,
        nullable=False,
        default="whatsapp",
        server_default="whatsapp",
    )
    number = Column(String, nullable=False, unique=True)
    status = Column(String, nullable=False, default="active", server_default="active")
    twilio_sender_sid = Column(String, nullable=True)
    auth_token = Column(String, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

    __table_args__ = (
        sa.CheckConstraint(
            "status IN ('active', 'inactive')",
            name="ck_shared_pool_number_status",
        ),
    )


class SharedPlatformRoute(Base):
    """Maps (pool_number, external_contact) → assistant for inbound routing.

    Only used for external contacts (non-platform-users).  Platform users
    are routed dynamically via user identity lookups (Tier 1).
    Routes are created when an assistant sends an outbound message
    to an external contact, establishing a permanent reply path.
    """

    __tablename__ = "shared_platform_routes"

    id = Column(Integer, primary_key=True)
    pool_number_id = Column(
        Integer,
        ForeignKey("shared_pool_numbers.id", ondelete="CASCADE"),
        nullable=False,
    )
    contact_number = Column(String, nullable=False)
    assistant_id = Column(
        Integer,
        ForeignKey("assistants.agent_id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    last_inbound_at = Column(TIMESTAMP(timezone=True), nullable=True)

    call_permission_status = Column(String, nullable=True)
    call_permission_granted_at = Column(TIMESTAMP(timezone=True), nullable=True)
    call_permission_expires_at = Column(TIMESTAMP(timezone=True), nullable=True)

    pool_number = relationship("SharedPoolNumber")
    assistant = relationship("Assistant")

    __table_args__ = (
        UniqueConstraint(
            "pool_number_id",
            "contact_number",
            name="uq_pool_contact",
        ),
        Index(
            "ix_shared_routes_assistant",
            "assistant_id",
            "contact_number",
        ),
        Index(
            "ix_shared_routes_contact",
            "contact_number",
        ),
    )


class DecommissionedRoute(Base):
    """Tracks old (pool_number, contact) pairs after conflict reassignment.

    When an assistant is reassigned to a new pool number, its old routes
    are recorded here so that inbound messages to the old number can be
    answered with an auto-reply instead of being silently dropped.
    """

    __tablename__ = "decommissioned_routes"

    id = Column(Integer, primary_key=True)
    platform = Column(String, nullable=False)
    pool_number_id = Column(
        Integer,
        ForeignKey("shared_pool_numbers.id", ondelete="CASCADE"),
        nullable=False,
    )
    contact_identifier = Column(String, nullable=False)
    old_assistant_id = Column(
        Integer,
        ForeignKey("assistants.agent_id", ondelete="CASCADE"),
        nullable=False,
    )
    new_pool_number_id = Column(
        Integer,
        ForeignKey("shared_pool_numbers.id", ondelete="CASCADE"),
        nullable=True,
    )
    decommissioned_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

    pool_number = relationship("SharedPoolNumber", foreign_keys=[pool_number_id])
    new_pool_number = relationship(
        "SharedPoolNumber",
        foreign_keys=[new_pool_number_id],
    )

    __table_args__ = (
        Index(
            "ix_decommissioned_routes_lookup",
            "pool_number_id",
            "contact_identifier",
        ),
    )


class ConflictEvent(Base):
    """Audit log for shared-pool conflict resolutions.

    Records every conflict detection + resolution, including the pool
    reassignments performed and the delivery status of WhatsApp
    notifications sent to affected users.
    """

    __tablename__ = "conflict_events"

    id = Column(Integer, primary_key=True)
    platform = Column(String, nullable=False)
    conflict_type = Column(String, nullable=False)
    trigger_assistant_id = Column(
        Integer,
        ForeignKey("assistants.agent_id", ondelete="SET NULL"),
        nullable=True,
    )
    affected_assistant_ids = Column(JSONB, nullable=False)
    old_pool_assignments = Column(JSONB, nullable=False)
    new_pool_assignments = Column(JSONB, nullable=False)
    notification_recipients = Column(JSONB, nullable=True)
    notification_status = Column(JSONB, nullable=True)
    status = Column(
        String,
        nullable=False,
        default="notifying",
        server_default="notifying",
    )
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    resolved_at = Column(TIMESTAMP(timezone=True), nullable=True)

    trigger_assistant = relationship("Assistant")

    __table_args__ = (
        sa.CheckConstraint(
            "conflict_type IN ('contact_overlap', 'user_to_user', 'org_membership')",
            name="ck_conflict_event_type",
        ),
        sa.CheckConstraint(
            "status IN ('notifying', 'resolved', 'notification_failed', 'failed')",
            name="ck_conflict_event_status",
        ),
        Index("ix_conflict_events_status", "status"),
        Index("ix_conflict_events_trigger_assistant", "trigger_assistant_id"),
    )


class CreditTransaction(Base):
    """Append-only ledger of every credit movement on a billing account.

    Positive ``amount`` = credits added (recharge, promo, refund, dispute).
    Negative ``amount`` = credits spent (llm, hire, resources, media).

    The public API constrains ``category`` to the canonical spending set
    (``llm | hire | resources | media``) for debits and
    (``recharge | promo | refund | dispute``) for credits.
    Internal reconciliation routines may use additional diagnostic
    categories (e.g. ``void``, ``stale_pending_recharge``).

    ``balance_after`` is a snapshot captured in the same DB transaction
    as the balance update so it can be used for reconciliation:
    the latest row's ``balance_after`` must always equal
    ``billing_account.credits``.  NULL for historical backfills
    where the running balance cannot be reliably reconstructed.
    """

    __tablename__ = "credit_transaction"

    id = Column(BigInteger, primary_key=True)
    billing_account_id = Column(
        Integer,
        ForeignKey("billing_account.id", ondelete="CASCADE"),
        nullable=False,
    )
    at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    # Financial
    amount = Column(Numeric, nullable=False)
    balance_after = Column(Numeric, nullable=True)

    # Dimensions (indexed for fast filtering)
    category = Column(String, nullable=False)
    assistant_id = Column(Integer, nullable=True)
    user_id = Column(String, nullable=True)
    organization_id = Column(Integer, nullable=True)

    description = Column(String, nullable=True)
    detail = Column(JSONB, nullable=True)

    __table_args__ = (
        Index("ix_credit_txn_ba_at", "billing_account_id", "at"),
        Index("ix_credit_txn_ba_category_at", "billing_account_id", "category", "at"),
        Index("ix_credit_txn_assistant_category_at", "assistant_id", "category", "at"),
        Index("ix_credit_txn_user_at", "user_id", "at"),
    )


class AssistantCleanupTask(Base):
    """Retryable cleanup work item for assistant teardown after owner deletion.

    A task is created before an owner row is irreversibly deleted. The payload
    stores the minimum retry state needed to finish runtime teardown, contact
    deprovisioning, and assistant-scoped GCS cleanup outside the original
    request lifecycle.
    """

    __tablename__ = "assistant_cleanup_tasks"

    id = Column(Integer, primary_key=True)
    assistant_id = Column(Integer, nullable=False)
    deploy_env = Column(String, nullable=True)
    desktop_mode = Column(String, nullable=True)
    source_flow = Column(String, nullable=False)
    cleanup_payload = Column(
        JSONB,
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    )
    status = Column(String, nullable=False, default="pending", server_default="pending")
    attempt_count = Column(Integer, nullable=False, server_default=sa.text("0"))
    last_error = Column(String, nullable=True)
    last_result = Column(JSONB, nullable=True)
    next_retry_at = Column(TIMESTAMP(timezone=True), nullable=True)
    processing_started_at = Column(TIMESTAMP(timezone=True), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
    completed_at = Column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_assistant_cleanup_task_status",
        ),
        Index("ix_assistant_cleanup_tasks_status", "status", "next_retry_at"),
        Index("ix_assistant_cleanup_tasks_assistant", "assistant_id"),
    )


class DashboardToken(Base):
    """Token-to-context mapping for dashboard tiles and layouts.

    Content lives in Unify contexts (Dashboards/Tiles, Dashboards/Layouts);
    this table provides the routing information the console needs to resolve
    a token-based URL to the correct Unify context path and creator identity.
    """

    __tablename__ = "dashboard_token"

    token = Column(String(12), primary_key=True)
    entity_type = Column(String(20), nullable=False)
    context_name = Column(String(500), nullable=False)
    project_id = Column(
        Integer,
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    organization_id = Column(
        Integer,
        ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=True,
    )
    created_at = Column(TIMESTAMP, server_default=func.now())

    project = relationship(
        "Project",
        backref=backref("dashboard_tokens", passive_deletes=True),
    )

    __table_args__ = (
        Index("idx_dashboard_token_project_id", "project_id"),
        Index("idx_dashboard_token_user_id", "user_id"),
    )
