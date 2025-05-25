import uuid

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    TIMESTAMP,
    Boolean,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSON, JSONB
from sqlalchemy.orm import relationship

from orchestra.db.base import Base


class Model(Base):
    """Model class for the model table."""

    __tablename__ = "model"

    id = Column(Integer(), primary_key=True)
    mdl_code = Column(String())
    uploaded_at = Column(TIMESTAMP, nullable=False)
    task = Column(String(), ForeignKey("task.name"), nullable=False)
    active = Column(Boolean(), server_default="f", nullable=False)  # type: ignore


class Task(Base):
    """Model class for the task table."""

    __tablename__ = "task"

    name = Column(String(), primary_key=True)
    modality = Column(String(), ForeignKey("modality.name"), nullable=False)


class Modality(Base):
    """Model class for the modality table."""

    __tablename__ = "modality"

    name = Column(String(), primary_key=True)


class Endpoint(Base):
    """Model class for the endpoint table."""

    __tablename__ = "endpoint"

    id = Column(Integer(), primary_key=True)
    mdl_id = Column(Integer(), ForeignKey("model.id"), nullable=False)
    provider_id = Column(Integer(), ForeignKey("provider.id"), nullable=False)
    created_at = Column(TIMESTAMP, nullable=False)
    active = Column(Boolean(), server_default="f", nullable=False)  # type: ignore


class Provider(Base):
    """Model class for the provider table."""

    __tablename__ = "provider"

    id = Column(Integer(), primary_key=True)
    name = Column(String(), nullable=False)
    display_name = Column(String(), nullable=False)
    image_url = Column(String(), nullable=False)


class Datapoint(Base):
    """Model class for the datapoint table."""

    __tablename__ = "datapoint"

    id = Column(Integer(), primary_key=True)
    benchmark_run_id = Column(Integer(), ForeignKey("benchmark_run.id"), nullable=False)
    metric_name = Column(String(), ForeignKey("metric.name"), nullable=False)
    value = Column(Numeric(), nullable=False)
    tooltip = Column(String())
    measured_at = Column(TIMESTAMP, nullable=False)


class BenchmarkRegime(Base):
    """Model class for the benchmark_regime table."""

    __tablename__ = "benchmark_regime"

    name = Column(String(), primary_key=True)


class BenchmarkRegion(Base):
    """Model class for the benchmark_region table."""

    __tablename__ = "benchmark_region"

    name = Column(String(), primary_key=True)


class BenchmarkSeqLen(Base):
    """Model class for the seq_len table."""

    __tablename__ = "benchmark_seq_len"

    name = Column(String(), primary_key=True)


class BenchmarkRun(Base):
    """Model class for the benchmark_run table."""

    __tablename__ = "benchmark_run"

    id = Column(Integer(), primary_key=True)
    endpoint_id = Column(Integer(), ForeignKey("endpoint.id"), nullable=False)
    regime = Column(String(), ForeignKey("benchmark_regime.name"), nullable=False)
    region = Column(String(), ForeignKey("benchmark_region.name"), nullable=False)
    seq_len = Column(String(), ForeignKey("benchmark_seq_len.name"), nullable=False)
    measured_at = Column(TIMESTAMP, nullable=False)


class Metric(Base):
    """Model class for the metric table."""

    __tablename__ = "metric"

    name = Column(String(), primary_key=True)
    units = Column(String(), nullable=False)
    display_name = Column(String(), nullable=False)
    tooltip = Column(String())
    priority = Column(Integer(), nullable=False)
    plottable = Column(Boolean(), nullable=False)  # type: ignore


class Users(Base):
    """Model class for the users table."""

    __tablename__ = "users"

    # IMPORTANT: If any change happens here the DB trigger must be updated as well!
    id = Column(String(), primary_key=True)
    credits = Column(Numeric(), nullable=False)
    stripe_customer_id = Column(String())
    autorecharge = Column(Boolean, nullable=False)
    autorecharge_threshold = Column(Numeric, nullable=False)
    autorecharge_qty = Column(Numeric, nullable=False)
    store_prompts = Column(Boolean)
    frozen = Column(Boolean(), nullable=False, server_default="f")


class Recharge(Base):
    """Model class for the recharge table."""

    __tablename__ = "recharge"

    id = Column(Integer(), primary_key=True)
    at = Column(TIMESTAMP, nullable=False)
    user_id = Column(String(), ForeignKey("users.id"), nullable=False)
    quantity = Column(Numeric(), nullable=False)
    type = Column(String())
    transaction_id = Column(String())
    status = Column(
        String(),
        nullable=False,
        server_default="pending",
    )  # one of: pending, completed, refunded, disputed


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


# CLEANUP: Delete this
class BetaList(Base):
    """Model class for the beta list table."""

    __tablename__ = "beta_list"

    id = Column(Integer(), primary_key=True)
    email = Column(String(), nullable=False)
    type = Column(String(), nullable=False)


class CustomApiKey(Base):
    """Model class for the custom api keys table."""

    __tablename__ = "custom_api_key"

    id = Column(Integer(), primary_key=True)
    user_id = Column(String(), ForeignKey("users.id"), nullable=False)
    key = Column(String(), nullable=False)
    value = Column(String(), nullable=False)


class CustomEndpoint(Base):
    """Model class for the custom endpoints table."""

    __tablename__ = "custom_endpoint"

    id = Column(Integer(), primary_key=True)
    user_id = Column(String(), ForeignKey("users.id"), nullable=False)
    name = Column(String(), nullable=False)
    model_arg = Column(String())
    url = Column(String(), nullable=False)
    key_id = Column(Integer(), ForeignKey("custom_api_key.id"), nullable=False)


class CustomRouter(Base):
    """Model class for the custom router table."""

    __tablename__ = "custom_router"

    id = Column(Integer(), primary_key=True)
    user_id = Column(String(), ForeignKey("users.id"))
    router_name = Column(String(), nullable=False)
    router_id = Column(String(), nullable=False)


class CreditCardFingerprint(Base):
    """Model class for the credit card fingerprint table."""

    __tablename__ = "credit_card_fingerprint"

    id = Column(Integer(), primary_key=True)
    user_id = Column(String(), ForeignKey("users.id"), nullable=False)
    fingerprint = Column(String(), nullable=False)


class LatestBenchmark(Base):
    """Model class for latest benchmark data table."""

    __tablename__ = "latest_benchmark"

    endpoint_id = Column(
        Integer(),
        ForeignKey("endpoint.id"),
        primary_key=True,
        nullable=False,
    )
    regime = Column(String(), primary_key=True)
    region = Column(String(), primary_key=True)
    seq_len = Column(String(), primary_key=True)
    input_cost = Column(Numeric())
    output_cost = Column(Numeric())
    ttft = Column(Numeric())
    itl = Column(Numeric())
    measured_at = Column(TIMESTAMP, nullable=False)


class CustomEndpointBenchmark(Base):
    """Model class for custom endpoint runtime benchmark table."""

    __tablename__ = "custom_endpoint_benchmark"

    id = Column(Integer(), primary_key=True)
    custom_endpoint_id = Column(
        Integer(),
        ForeignKey("custom_endpoint.id"),
        nullable=False,
    )
    metric_name = Column(String(), nullable=False)
    value = Column(Numeric(), nullable=False)
    measured_at = Column(TIMESTAMP, nullable=False)


class Tag(Base):
    """Model class for query tags table"""

    __tablename__ = "tags"

    id = Column(Integer(), primary_key=True)
    user_id = Column(String(), ForeignKey("users.id"), index=True, nullable=False)
    tag_name = Column(String(), nullable=False)
    queries = relationship("QueryTagAssociation", back_populates="tag")
    __table_args__ = (UniqueConstraint("user_id", "tag_name", name="uq_user_tag"),)


class QueryTagAssociation(Base):
    """Model class for map between tags and queries"""

    __tablename__ = "query_tag_association"
    user_id = Column(String(), ForeignKey("users.id"), primary_key=True, index=True)
    query_id = Column(Integer(), ForeignKey("query.id"), primary_key=True, index=True)
    tag_id = Column(Integer(), ForeignKey("tags.id"), primary_key=True, index=True)
    tag = relationship("Tag", back_populates="queries")
    query = relationship("Query", back_populates="tags")

    sa.ForeignKeyConstraint(
        ["user_id", "tag_id"],
        ["tags.user_id", "tags.id"],
        name="fk_user_tag_association",
    )


class LocalEndpoint(Base):
    """Model class for the local endpoints table."""

    __tablename__ = "local_endpoint"

    id = Column(Integer(), primary_key=True)
    user_id = Column(String(), ForeignKey("users.id"), nullable=False)
    name = Column(String(), nullable=False)
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_user_endpoint"),)


class Query(Base):
    """Model class for the query table."""

    __tablename__ = "query"

    id = Column(Integer(), primary_key=True)
    user_id = Column(
        String(),
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )
    at = Column(sa.TIMESTAMP(), nullable=False)
    model_provider_str = Column(String(), nullable=False)
    endpoint_id = Column(Integer(), ForeignKey("endpoint.id"), index=True)
    custom_endpoint_id = Column(
        Integer(),
        ForeignKey("custom_endpoint.id"),
        index=True,
    )
    local_endpoint_id = Column(
        Integer(),
        ForeignKey("local_endpoint.id"),
        index=True,
    )
    credits = Column(Numeric(), nullable=False)
    query_body = Column(String(), nullable=False)
    response_body = Column(String(), nullable=False)
    signature = Column(String())
    used_router = Column(Boolean())
    router = Column(String())
    status_code = Column(Integer(), nullable=False)
    tags = relationship("QueryTagAssociation", back_populates="query")
    __table_args__ = (Index("ix_user_endpoint", "user_id", "endpoint_id"),)


class Router(Base):
    """Model class for the router table."""

    __tablename__ = "router"

    id = sa.Column(sa.Integer(), primary_key=True)
    user_id = sa.Column(sa.String(), sa.ForeignKey("users.id"), nullable=True)
    name = sa.Column(sa.String(), nullable=False)
    endpoints = sa.Column(sa.String(), nullable=False)
    trained = sa.Column(sa.Boolean(), default=False, nullable=False)
    gcp_router_id = sa.Column(sa.String(), nullable=True)
    deployed = sa.Column(sa.Boolean(), default=False, nullable=False)

    __table_args__ = (sa.UniqueConstraint("user_id", "name", name="uq_router_name"),)


class AuthUser(Base):
    __tablename__ = "auth_user"

    id = Column(String, primary_key=True, default=uuid.uuid4)
    email = Column(String, unique=True, index=True, nullable=False)
    name = Column(String)
    last_name = Column(String)
    job_title = Column(String)
    image = Column(String)
    # Account tier, developer, professional, enterprise
    tier = Column(String, nullable=False, server_default="developer")
    # Toggles managed by usage quotas
    queries_enabled = Column(Boolean, nullable=False, server_default="true")
    evaluations_enabled = Column(Boolean, nullable=False, server_default="true")
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, onupdate=func.now())

    # Relationships
    interfaces = relationship(
        "Interface",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    temp_interfaces = relationship(
        "TempInterface",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


# Account table (for external providers like Google, GitHub)
# Each user can have multiple accounts
class Account(Base):
    __tablename__ = "account"

    id = Column(String, primary_key=True, default=uuid.uuid4)
    user_id = Column(String, ForeignKey("auth_user.id", ondelete="CASCADE"))
    provider = Column(String, nullable=False)  # OAuth provider name
    provider_type = Column(String, nullable=False)
    provider_account_id = Column(String, nullable=False)
    access_token = Column(String)  # OAuth access token (optional)
    # TODO: This can be removed? refreshtokens
    refresh_token = Column(String)  # OAuth refresh token (optional)
    # Expiration time for OAuth token (optional)
    expires_at = Column(TIMESTAMP)


class Organization(Base):
    __tablename__ = "organization"

    id = Column(Integer, primary_key=True)
    owner_id = Column(
        String,
        ForeignKey("auth_user.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    name = Column(String, unique=True, nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now())

    # Relationships
    interfaces = relationship(
        "Interface",
        back_populates="organization",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    temp_interfaces = relationship(
        "TempInterface",
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
        ForeignKey("auth_user.id", ondelete="CASCADE"),
        nullable=False,
    )
    level = Column(
        String,
        nullable=False,
    )  # owner, admin, user -> owner is duplicated info? :/
    created_at = Column(TIMESTAMP, server_default=func.now())


class ApiKey(Base):
    __tablename__ = "api_key"

    id = Column(Integer, primary_key=True)
    name = Column(String)
    user_id = Column(String, ForeignKey("auth_user.id", ondelete="CASCADE"))
    organization_id = Column(Integer, ForeignKey("organization.id", ondelete="CASCADE"))
    key = Column(String, unique=True, nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now())

    __table_args__ = (UniqueConstraint("user_id", "name"),)


class Project(Base):
    __tablename__ = "project"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        String,
        ForeignKey("auth_user.id", ondelete="CASCADE"),
        index=True,
    )
    organization_id = Column(
        Integer,
        ForeignKey("organization.id", ondelete="CASCADE"),
        index=True,
    )
    name = Column(String, nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, onupdate=func.now())
    contexts = relationship("Context", back_populates="project", passive_deletes=True)
    interfaces = relationship(
        "Interface",
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    temp_interfaces = relationship(
        "TempInterface",
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    # we want sql nulls to be distinct in the unique constraints
    # (postgresql_nulls_not_distinct=False)
    __table_args__ = (
        UniqueConstraint("user_id", "name"),
        UniqueConstraint("organization_id", "name"),
    )


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
    description = Column(String)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, onupdate=func.now())
    is_versioned = Column(Boolean, nullable=False, server_default="f")
    version = Column(Integer, nullable=False, server_default="1")
    allow_duplicates = Column(Boolean, nullable=False, server_default="t")

    project = relationship("Project", back_populates="contexts")
    artifacts = relationship("ContextArtifact", back_populates="context")
    log_events = relationship(
        "LogEvent",
        secondary="log_event_context",
        back_populates="contexts",
        passive_deletes=True,
    )

    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_project_context_name"),
    )


class ContextHistory(Base):
    """Model class for storing historical versions of contexts."""

    __tablename__ = "context_history"

    id = Column(Integer, primary_key=True)
    context_id = Column(
        Integer,
        ForeignKey("context.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version = Column(Integer, nullable=False)
    log_versions = Column(
        JSONB,
        nullable=False,
        server_default=text("'{}'"),
        comment="Stores {log_event_id: {field_key: version_int}}",
    )
    name = Column(String, nullable=True)
    description = Column(String, nullable=True)
    archived_at = Column(TIMESTAMP, server_default=func.now())


class ContextArtifact(Base):
    """Model class for storing artifacts within contexts."""

    __tablename__ = "context_artifact"

    id = Column(Integer, primary_key=True)
    context_id = Column(
        Integer,
        ForeignKey("context.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    key = Column(String, nullable=False)
    value = Column(String)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, onupdate=func.now())

    context = relationship("Context", back_populates="artifacts")

    __table_args__ = (
        UniqueConstraint(
            "context_id",
            "key",
            name="uq_context_artifact_key",
        ),
    )


class Artifact(Base):
    __tablename__ = "artifact"

    id = Column(Integer, primary_key=True)
    project_id = Column(
        Integer,
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    key = Column(String, nullable=False)
    value = Column(String)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, onupdate=func.now())


class LogEvent(Base):
    __tablename__ = "log_event"

    id = Column(Integer, primary_key=True)
    project_id = Column(
        Integer,
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, onupdate=func.now())
    # Relationships
    contexts = relationship(
        "Context",
        secondary="log_event_context",
        back_populates="log_events",
        passive_deletes=True,
    )
    derived_logs = relationship(
        "DerivedLog",
        cascade="all, delete-orphan",
        backref="log_event",
    )

    __table_args__ = (Index("idx_log_event_project_id_id", "project_id", "id"),)


class JSONLog(Base):
    __tablename__ = "json_log"

    id = Column(Integer, primary_key=True)
    log_event_id = Column(
        Integer,
        ForeignKey("log_event.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    key = Column(String, nullable=False)
    value = Column(JSON)
    __table_args__ = (UniqueConstraint("log_event_id", "key"),)


class Log(Base):
    __tablename__ = "log"

    id = Column(Integer, primary_key=True)
    log_event_id = Column(
        Integer,
        ForeignKey("log_event.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    key = Column(String, nullable=False, index=True)
    value = Column(JSONB)
    version = Column(Integer)
    inferred_type = Column(String)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, onupdate=func.now())
    __table_args__ = (UniqueConstraint("log_event_id", "key", "version"),)


class LogHistory(Base):
    __tablename__ = "log_history"

    id = Column(Integer, primary_key=True)
    log_event_id = Column(
        Integer,
        ForeignKey("log_event.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    key = Column(String, nullable=False)
    value = Column(JSONB)
    version = Column(Integer, nullable=False)
    inferred_type = Column(String)
    description = Column(String)
    archived_at = Column(TIMESTAMP, server_default=func.now())


class ParamVersion(Base):
    """Model class for tracking parameter versions."""

    __tablename__ = "param_version"

    project_id = Column(
        Integer,
        ForeignKey("project.id", ondelete="CASCADE"),
        primary_key=True,
    )
    context_id = Column(
        Integer,
        ForeignKey("context.id", ondelete="CASCADE"),
        primary_key=True,
    )
    param_key = Column(String, primary_key=True)
    last_version = Column(Integer, nullable=False)

    __table_args__ = (
        Index("idx_param_version_project_key", "project_id", "context_id", "param_key"),
    )


class JSONLogHistory(Base):
    __tablename__ = "json_log_history"

    id = Column(Integer, primary_key=True)
    log_event_id = Column(
        Integer,
        ForeignKey("log_event.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    key = Column(String, nullable=False)
    value = Column(JSON)
    version = Column(Integer, nullable=False)
    description = Column(String)
    archived_at = Column(TIMESTAMP, server_default=func.now())


class DerivedLog(Base):
    __tablename__ = "derived_log"

    id = Column(Integer, primary_key=True)
    log_event_id = Column(
        Integer,
        ForeignKey("log_event.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    key = Column(String, nullable=False, index=True)
    equation = Column(String)
    referenced_logs = Column(JSONB)
    value = Column(JSONB)
    inferred_type = Column(String)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, onupdate=func.now())

    __table_args__ = (UniqueConstraint("log_event_id", "key"),)


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
    is_active = Column(Boolean, nullable=False, server_default="t")
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, onupdate=func.now())

    __table_args__ = (UniqueConstraint("project_id", "context_id", "key"),)


class DashboardView(Base):
    __tablename__ = "dashboard_view"

    id = Column(Integer, primary_key=True)
    project_id = Column(
        Integer,
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String)
    view = Column(String)
    created_at = Column(TIMESTAMP, server_default=func.now())


class Interface(Base):
    __tablename__ = "interface"

    id = Column(String, primary_key=True, default=uuid.uuid4)
    # TODO: remove both <user_id> and <organization_id>
    user_id = Column(
        String,
        ForeignKey("auth_user.id", ondelete="CASCADE"),
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
    color = Column(String(), nullable=True)
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
    user = relationship("AuthUser", back_populates="interfaces")
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
    mutable = Column(Boolean(), nullable=False, server_default="f")  # type: ignore
    enum_values = Column(JSONB, nullable=False, server_default=text("'[]'"))
    enum_restrict = Column(Boolean(), nullable=False, server_default="false")
    created_at = Column(TIMESTAMP, server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "field_name",
            "context_id",
            name="uq_project_field_name_context_id",
        ),
    )


class TempInterface(Base):
    __tablename__ = "temp_interface"

    id = Column(String, primary_key=True, default=uuid.uuid4)
    user_id = Column(
        String,
        ForeignKey("auth_user.id", ondelete="CASCADE"),
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
    color = Column(String(), nullable=True)
    created_at = Column(TIMESTAMP, nullable=True, server_default=func.now())
    # Relationships
    project = relationship("Project", back_populates="temp_interfaces")
    user = relationship("AuthUser", back_populates="temp_interfaces")
    organization = relationship("Organization", back_populates="temp_interfaces")
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "project_id",
            "name",
            name="temp_it_uq_project_name",
        ),
    )


class AdminUser(Base):
    """Model class for admin users who have special privileges."""

    __tablename__ = "admin_user"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        String,
        ForeignKey("auth_user.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, onupdate=func.now())

    # Relationship to AuthUser
    auth_user = relationship("AuthUser", backref="admin_user")


class FavoriteProject(Base):
    """Model class for user's favorite projects."""

    __tablename__ = "favorite_project"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        String,
        ForeignKey("auth_user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id = Column(
        Integer,
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
    )
    icon = Column(String, nullable=False)
    position = Column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "project_id", name="uq_user_favorite_project"),
    )


class Assistant(Base):
    """Model class for the assistants table."""

    __tablename__ = "assistants"

    agent_id = Column(Integer, primary_key=True)
    user_id = Column(
        String,
        ForeignKey("auth_user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    first_name = Column(String, nullable=True)
    surname = Column(String, nullable=True)
    age = Column(Integer, nullable=True)
    region = Column(String, nullable=True)
    profile_photo = Column(String, nullable=True)
    about = Column(String, nullable=True)
    weekly_limit = Column(Numeric, nullable=True)
    max_parallel = Column(Integer, nullable=True)
    phone = Column(String, nullable=True)
    email = Column(String, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())
    recordings = relationship(
        "CallRecording",
        back_populates="assistant",
        cascade="all, delete-orphan",
    )
    voice_id = sa.Column(
        sa.String,
        sa.ForeignKey("voices.voice_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )


class Voice(Base):
    """Model class for the assistants voices table."""

    __tablename__ = "voices"

    voice_id = Column(String, primary_key=True)  # This will store Cartesia's voice ID
    user_id = Column(
        String,
        ForeignKey("auth_user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String, nullable=False)
    description = Column(String, nullable=False)
    gender = Column(String, nullable=False)
    language = Column(String, nullable=False)  # e.g., "en", "es"


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
    visible = Column(Boolean(), nullable=False, server_default="t")
    active = Column(Boolean(), nullable=False, server_default="f")
    order = Column(Integer, nullable=False, server_default="0")
    global_context = Column(String(), nullable=True)
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
    type = Column(String(), nullable=True)  # "Table", "Plot", "View", "Editor"

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
    file_path = Column(String(), nullable=True)
    file_type = Column(String(), nullable=True)
    content = Column(String(), nullable=True)

    # Relationships
    tile = relationship("Tile", back_populates="editor_tile")


class CallRecording(Base):
    """Model class for the assistant call recordings table."""

    __tablename__ = "assistant_call_recording"

    id = Column(Integer, primary_key=True)
    agent_id = Column(
        Integer,
        ForeignKey("assistants.agent_id"),
        nullable=False,
        index=True,
    )
    filename = Column(String, nullable=False)
    url = Column(String, nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now())
    assistant = relationship("Assistant", back_populates="recordings")


class Embedding(Base):
    """Model class for the embedding table that stores embeddings."""

    __tablename__ = "embedding"

    id = Column(Integer, primary_key=True)
    ref_id = Column(
        Integer,
        ForeignKey("log_event.id", ondelete="CASCADE"),
        nullable=False,
    )
    model = Column(String, nullable=False)
    key = Column(String, nullable=False)
    vector = Column(Vector(1536), nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("ref_id", "model", "key", name="uq_embedding"),
        Index(
            "idx_embedding_ref",
            "ref_id",
            "model",
            "key",
        ),
        Index(
            "embedding_hnsw_cosine_idx",
            "vector",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"vector": "vector_cosine_ops"},
        ),
        Index(
            "embedding_hnsw_l2_idx",
            "vector",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"vector": "vector_l2_ops"},
        ),
        Index(
            "embedding_hnsw_ip_idx",
            "vector",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"vector": "vector_ip_ops"},
        ),
    )
