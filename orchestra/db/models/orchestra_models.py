import uuid

import sqlalchemy as sa
from sqlalchemy import (
    TIMESTAMP,
    Boolean,
    Column,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
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


class Recharge(Base):
    """Model class for the recharge table."""

    __tablename__ = "recharge"

    id = Column(Integer(), primary_key=True)
    at = Column(TIMESTAMP, nullable=False)
    user_id = Column(String(), ForeignKey("users.id"), nullable=False)
    quantity = Column(Numeric(), nullable=False)
    type = Column(String(), ForeignKey("recharge_type.type"), nullable=False)
    transaction_id = Column(String())


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
    time_to_first_token = Column(Numeric())
    inter_token_latency = Column(Numeric())
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


class Dataset(Base):
    """Model class for the dataset table."""

    __tablename__ = "dataset"

    id = Column(Integer(), primary_key=True)
    user_id = Column(String(), ForeignKey("users.id"), index=True)
    name = Column(String(), nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now())
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_userid_name"),)


class DatasetEntry(Base):
    """Model class for the dataset entries table."""

    __tablename__ = "dataset_entry"

    id = Column(String(10), primary_key=True)
    dataset_id = Column(
        Integer(),
        ForeignKey("dataset.id", ondelete="CASCADE"),
        index=True,
    )
    entry = Column(String(), nullable=False)  # JSON serialised
    entry_hash = Column(String(64), nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now())
    __table_args__ = (
        UniqueConstraint(
            "dataset_id",
            "entry_hash",
            name="uq_dataset_entry_hash",
        ),
    )


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
    # we want sql nulls to be distinct in the unique constraints
    # (postgresql_nulls_not_distinct=False)
    __table_args__ = (
        UniqueConstraint("user_id", "name"),
        UniqueConstraint("organization_id", "name"),
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


class DatasetArtifact(Base):
    __tablename__ = "dataset_artifact"

    id = Column(Integer, primary_key=True)
    dataset_id = Column(
        Integer,
        ForeignKey("dataset.id", ondelete="CASCADE"),
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


class Log(Base):
    __tablename__ = "log"

    id = Column(Integer, primary_key=True)
    log_event_id = Column(
        Integer,
        ForeignKey("log_event.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    key = Column(String, nullable=False)
    value = Column(String)
    version = Column(Integer)
    inferred_type = Column(String)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, onupdate=func.now())
    __table_args__ = (UniqueConstraint("log_event_id", "key"),)


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
    new_counter = Column(Integer)
    items = Column(String(), nullable=False)
