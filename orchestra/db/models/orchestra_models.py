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
from sqlalchemy.dialects.postgresql import JSONB
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


class QueryOld(Base):
    """Model class for the old query table."""

    __tablename__ = "query_old"

    id = Column(Integer(), primary_key=True)
    user_id = Column(String(), ForeignKey("users.id"), nullable=False)
    at = Column(TIMESTAMP, nullable=False)
    endpoint_id = Column(Integer(), ForeignKey("endpoint.id"), nullable=False)
    credits = Column(Numeric(), nullable=False)
    prompt = Column(String())
    signature = Column(String())
    used_router = Column(Boolean())
    router = Column(String)


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
    mdl_name = Column(String())
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


# TODO: CASCADE DELETE FOR PROMPTS -> EVALUATIONS


class Dataset(Base):
    """Model class for the dataset table."""

    __tablename__ = "dataset"

    id = Column(Integer(), primary_key=True)
    user_id = Column(String(), ForeignKey("users.id"), index=True)
    name = Column(String(), nullable=False)
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_userid_name"),)


class StoredPrompt(Base):
    """Model class for the stored prompt table."""

    __tablename__ = "stored_prompt"

    id = Column(Integer(), primary_key=True)
    user_id = Column(String(), ForeignKey("users.id"), index=True)
    system_msg = Column(String(), index=True)
    messages = Column(String(), nullable=False)
    prompt_kwargs = Column(String(), nullable=False)
    extra_fields = Column(JSONB, default={}, nullable=False)
    num_tokens = Column(Integer(), nullable=False)
    timestamp = Column(TIMESTAMP, nullable=False)
    __table_args__ = (
        Index(
            "uq_userid_prompt",
            func.hash_record_extended(
                func.row(user_id, system_msg, messages, prompt_kwargs, extra_fields),
                0,
            ),
            unique=True,
        ),
    )


class DefaultPrompt(Base):
    """Model class for the default prompt table."""

    __tablename__ = "default_prompt"

    id = Column(Integer(), primary_key=True)
    user_id = Column(String(), ForeignKey("users.id"), index=True)
    name = Column(String(), nullable=False)
    prompt = Column(String())


class StoredPromptVariation(Base):
    """Model class for variations of stored prompts table."""

    __tablename__ = "stored_prompt_variation"

    id = Column(Integer(), primary_key=True)
    datum_id = Column(
        Integer(),
        ForeignKey("stored_prompt.id"),
        index=True,
        nullable=False,
    )
    default_prompt_id = Column(
        Integer(),
        ForeignKey("default_prompt.id"),
        index=True,
        nullable=False,
    )


class StoredPromptResponse(Base):
    """Model class for the stored prompt response table."""

    __tablename__ = "stored_prompt_response"

    id = Column(Integer(), primary_key=True)
    datum_id = Column(Integer(), ForeignKey("stored_prompt.id"), index=True)
    prompt_variation_id = Column(
        Integer(),
        ForeignKey("stored_prompt_variation.id"),
        index=True,
    )
    endpoint_str = Column(String(), nullable=False)
    response = Column(String(), nullable=False)
    num_tokens = Column(Integer(), nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "datum_id",
            "prompt_variation_id",
            "endpoint_str",
            name="uq_prompt_response",
            postgresql_nulls_not_distinct=True,
        ),
    )


class Judgement(Base):
    """Model class for the judgement table."""

    __tablename__ = "judgement"

    id = Column(Integer(), primary_key=True)
    response_id = Column(Integer(), ForeignKey("stored_prompt_response.id"), index=True)
    judge_endpoint_str = Column(String(), nullable=False)
    evaluator_id = Column(Integer(), ForeignKey("evaluator.id"), nullable=False)
    judgement = Column(String(), nullable=False)
    judgement_score = Column(Numeric())

    __table_args__ = (
        UniqueConstraint(
            "response_id",
            "judge_endpoint_str",
            "evaluator_id",
            name="uq_judgement",
        ),
    )


class DatasetPrompt(Base):
    """Model class for the dataset prompt table."""

    __tablename__ = "dataset_prompt"

    id = Column(Integer(), primary_key=True)
    dataset_id = Column(Integer(), ForeignKey("dataset.id"), index=True)
    datum_id = Column(Integer(), ForeignKey("stored_prompt.id"), index=True)
    __table_args__ = (
        UniqueConstraint(
            "dataset_id",
            "datum_id",
            name="uq_dataset_prompt",
        ),
    )


class Evaluator(Base):
    """Model class for the evaluator table."""

    __tablename__ = "evaluator"

    id = Column(Integer(), primary_key=True)
    user_id = Column(String(), ForeignKey("users.id"), index=True)
    name = Column(String(), nullable=False)
    description = Column(String())
    judge_prompt = Column(String(), nullable=False)
    prompt_parser = Column(
        String(),
        nullable=False,
        default="{\"user_message\": \"['messages'][-1]['content']\"}",
    )
    response_parser = Column(
        String(),
        nullable=False,
        default="{\"assistant_message\": \"['message']['content']\"}",
    )
    extra_parser = Column(String())
    class_config = Column(String(), nullable=False)
    judge_models = Column(String(), nullable=False)
    client_side = Column(Boolean(), nullable=False)
    __table_args__ = (
        sa.UniqueConstraint("user_id", "name", name="uq_userid_evaluator"),
    )


class Evaluation(Base):
    """Model class for the evaluation table."""

    __tablename__ = "evaluation"

    id = Column(Integer(), primary_key=True)
    datum_id = Column(Integer(), ForeignKey("stored_prompt.id"), index=True)
    prompt_variation_id = Column(
        Integer(),
        ForeignKey("stored_prompt_variation.id"),
        index=True,
    )
    evaluator_id = Column(Integer(), ForeignKey("evaluator.id"), index=True)
    endpoint_str = Column(String(), nullable=False)
    score = Column(Numeric(), nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "datum_id",
            "prompt_variation_id",
            "evaluator_id",
            "endpoint_str",
            name="uq_evaluation",
            postgresql_nulls_not_distinct=True,
        ),
    )


class Router(Base):
    """Model class for the router table."""

    __tablename__ = "router"

    id = sa.Column(sa.Integer(), primary_key=True)
    user_id = sa.Column(sa.String(), sa.ForeignKey("users.id"), nullable=True)
    name = sa.Column(sa.String(), nullable=False)
    endpoints = sa.Column(sa.String(), nullable=False)
    evaluator_id = sa.Column(
        sa.Integer(),
        sa.ForeignKey("evaluator.id"),
        nullable=False,
    )
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
