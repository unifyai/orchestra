import sqlalchemy as sa
from sqlalchemy.orm import relationship

from orchestra.db.base import Base


class Model(Base):
    """Model class for the model table."""

    __tablename__ = "model"

    id = sa.Column(sa.Integer(), primary_key=True)
    mdl_code = sa.Column(sa.String())
    uploaded_at = sa.Column(sa.TIMESTAMP(), nullable=False)
    task = sa.Column(sa.String(), sa.ForeignKey("task.name"), nullable=False)

    active = sa.Column(
        sa.Boolean(),
        server_default="f",
        nullable=False,
    )  # type: ignore


class Task(Base):
    """Model class for the task table."""

    __tablename__ = "task"

    name = sa.Column(sa.String(), primary_key=True)
    modality = sa.Column(sa.String(), sa.ForeignKey("modality.name"), nullable=False)


class Modality(Base):
    """Model class for the modality table."""

    __tablename__ = "modality"

    name = sa.Column(sa.String(), primary_key=True)


class Endpoint(Base):
    """Model class for the endpoint table."""

    __tablename__ = "endpoint"

    id = sa.Column(sa.Integer(), primary_key=True)
    mdl_id = sa.Column(sa.Integer(), sa.ForeignKey("model.id"), nullable=False)
    provider_id = sa.Column(sa.Integer(), sa.ForeignKey("provider.id"), nullable=False)
    created_at = sa.Column(sa.TIMESTAMP(), nullable=False)
    active = sa.Column(
        sa.Boolean(),
        server_default="f",
        nullable=False,
    )  # type: ignore


class Provider(Base):
    """Model class for the provider table."""

    __tablename__ = "provider"

    id = sa.Column(sa.Integer(), primary_key=True)
    name = sa.Column(sa.String(), nullable=False)
    display_name = sa.Column(sa.String(), nullable=False)
    image_url = sa.Column(sa.String(), nullable=False)


class Datapoint(Base):
    """Model class for the datapoint table."""

    __tablename__ = "datapoint"

    id = sa.Column(sa.Integer(), primary_key=True)
    benchmark_run_id = sa.Column(
        sa.Integer(),
        sa.ForeignKey("benchmark_run.id"),
        nullable=False,
    )
    metric_name = sa.Column(sa.String(), sa.ForeignKey("metric.name"), nullable=False)
    value = sa.Column(sa.Numeric(), nullable=False)
    tooltip = sa.Column(sa.String())
    measured_at = sa.Column(sa.TIMESTAMP(), nullable=False)


class BenchmarkRegime(Base):
    """Model class for the benchmark_regime table."""

    __tablename__ = "benchmark_regime"

    name = sa.Column(sa.String(), primary_key=True)


class BenchmarkRegion(Base):
    """Model class for the benchmark_region table."""

    __tablename__ = "benchmark_region"

    name = sa.Column(sa.String(), primary_key=True)


class BenchmarkSeqLen(Base):
    """Model class for the seq_len table."""

    __tablename__ = "benchmark_seq_len"

    name = sa.Column(sa.String(), primary_key=True)


class BenchmarkRun(Base):
    """Model class for the benchmark_run table."""

    __tablename__ = "benchmark_run"

    id = sa.Column(sa.Integer(), primary_key=True)
    endpoint_id = sa.Column(
        sa.Integer(),
        sa.ForeignKey("endpoint.id"),
        nullable=False,
    )
    regime = sa.Column(
        sa.String(),
        sa.ForeignKey("benchmark_regime.name"),
        nullable=False,
    )
    region = sa.Column(
        sa.String(),
        sa.ForeignKey("benchmark_region.name"),
        nullable=False,
    )
    seq_len = sa.Column(
        sa.String(),
        sa.ForeignKey("benchmark_seq_len.name"),
        nullable=False,
    )
    measured_at = sa.Column(sa.TIMESTAMP(), nullable=False)


class Metric(Base):
    """Model class for the metric table."""

    __tablename__ = "metric"

    name = sa.Column(sa.String(), primary_key=True)
    units = sa.Column(sa.String(), nullable=False)
    display_name = sa.Column(sa.String(), nullable=False)
    tooltip = sa.Column(sa.String())
    priority = sa.Column(sa.Integer(), nullable=False)
    plottable = sa.Column(sa.Boolean(), nullable=False)  # type: ignore


class QueryOld(Base):
    """Model class for the old query table."""

    __tablename__ = "query_old"

    id = sa.Column(sa.Integer(), primary_key=True)
    user_id = sa.Column(sa.String(), sa.ForeignKey("users.id"), nullable=False)
    at = sa.Column(sa.TIMESTAMP(), nullable=False)
    endpoint_id = sa.Column(sa.Integer(), sa.ForeignKey("endpoint.id"), nullable=False)
    credits = sa.Column(sa.Numeric(), nullable=False)
    prompt = sa.Column(sa.String(), nullable=True)
    signature = sa.Column(sa.String(), nullable=True)
    used_router = sa.Column(sa.Boolean(), nullable=True)
    router = sa.Column(sa.String, nullable=True)


class Users(Base):
    """Model class for the users table."""

    __tablename__ = "users"

    # IMPORTANT: If any change happens here the DB trigger must be updated as well!
    id = sa.Column(sa.String(), primary_key=True)
    credits = sa.Column(sa.Numeric(), nullable=False)
    stripe_customer_id = sa.Column(sa.String(), nullable=True)
    autorecharge = sa.Column(sa.Boolean, nullable=False)
    autorecharge_threshold = sa.Column(sa.Numeric, nullable=False)
    autorecharge_qty = sa.Column(sa.Numeric, nullable=False)
    store_prompts = sa.Column(sa.Boolean, nullable=True)


class Recharge(Base):
    """Model class for the recharge table."""

    __tablename__ = "recharge"

    id = sa.Column(sa.Integer(), primary_key=True)
    at = sa.Column(sa.TIMESTAMP(), nullable=False)
    user_id = sa.Column(sa.String(), sa.ForeignKey("users.id"), nullable=False)
    quantity = sa.Column(sa.Numeric(), nullable=False)
    type = sa.Column(sa.String(), sa.ForeignKey("recharge_type.type"), nullable=False)
    transaction_id = sa.Column(sa.String(), nullable=True)


class RechargeType(Base):
    """Model class for the recharge_type table."""

    __tablename__ = "recharge_type"

    type = sa.Column(sa.String(), primary_key=True)


class BetaList(Base):
    """Model class for the beta list table."""

    __tablename__ = "beta_list"

    id = sa.Column(sa.Integer(), primary_key=True)
    email = sa.Column(sa.String(), nullable=False)
    type = sa.Column(sa.String(), nullable=False)


class CustomApiKey(Base):
    """Model class for the custom api keys table."""

    __tablename__ = "custom_api_key"

    id = sa.Column(sa.Integer(), primary_key=True)
    user_id = sa.Column(sa.String(), sa.ForeignKey("users.id"), nullable=False)
    key = sa.Column(sa.String(), nullable=False)
    value = sa.Column(sa.String(), nullable=False)


class CustomEndpoint(Base):
    """Model class for the custom endpoints table."""

    __tablename__ = "custom_endpoint"

    id = sa.Column(sa.Integer(), primary_key=True)
    user_id = sa.Column(sa.String(), sa.ForeignKey("users.id"), nullable=False)
    name = sa.Column(sa.String(), nullable=False)
    mdl_name = sa.Column(sa.String(), nullable=True)
    url = sa.Column(sa.String(), nullable=False)
    key_id = sa.Column(sa.Integer(), sa.ForeignKey("custom_api_key.id"), nullable=False)


class CustomRouter(Base):
    """Model class for the custom router table."""

    __tablename__ = "custom_router"

    id = sa.Column(sa.Integer(), primary_key=True)
    user_id = sa.Column(sa.String(), sa.ForeignKey("users.id"), nullable=True)
    router_name = sa.Column(sa.String(), nullable=False)
    router_id = sa.Column(sa.String(), nullable=False)


class CreditCardFingerprint(Base):
    """Model class for the credit card fingerprint table."""

    __tablename__ = "credit_card_fingerprint"

    id = sa.Column(sa.Integer(), primary_key=True)
    user_id = sa.Column(sa.String(), sa.ForeignKey("users.id"), nullable=False)
    fingerprint = sa.Column(sa.String(), nullable=False)


class LatestBenchmark(Base):
    """Model class for latest benchmark data table."""

    __tablename__ = "latest_benchmark"

    endpoint_id = sa.Column(
        sa.Integer(),
        sa.ForeignKey("endpoint.id"),
        nullable=False,
        primary_key=True,
    )
    regime = sa.Column(sa.String(), primary_key=True)
    region = sa.Column(sa.String(), primary_key=True)
    seq_len = sa.Column(sa.String(), primary_key=True)
    input_cost = sa.Column(sa.Numeric())
    output_cost = sa.Column(sa.Numeric())
    ttft = sa.Column(sa.Numeric())
    itl = sa.Column(sa.Numeric())
    measured_at = sa.Column(sa.TIMESTAMP(), nullable=False)


class CustomEndpointBenchmark(Base):
    """Model class for custom endpoint runtime benchmark table."""

    __tablename__ = "custom_endpoint_benchmark"

    id = sa.Column(sa.Integer(), primary_key=True)
    custom_endpoint_id = sa.Column(
        sa.Integer(),
        sa.ForeignKey("custom_endpoint.id"),
        nullable=False,
    )
    metric_name = sa.Column(sa.String(), nullable=False)
    value = sa.Column(sa.Numeric(), nullable=False)
    measured_at = sa.Column(sa.TIMESTAMP(), nullable=False)


class Tag(Base):
    """Model class for query tags table"""

    __tablename__ = "tags"

    id = sa.Column(sa.Integer(), primary_key=True)
    user_id = sa.Column(
        sa.String(),
        sa.ForeignKey("users.id"),
        nullable=False,
        index=True,
    )
    tag_name = sa.Column(sa.String(), nullable=False)
    queries = relationship("QueryTagAssociation", back_populates="tag")
    sa.UniqueConstraint("user_id", "tag_name", name="uq_user_tag")


class QueryTagAssociation(Base):
    """Model class for map between tags and queries"""

    __tablename__ = "query_tag_association"
    user_id = sa.Column(
        sa.String(),
        sa.ForeignKey("users.id"),
        primary_key=True,
        index=True,
    )
    query_id = sa.Column(
        sa.Integer(),
        sa.ForeignKey("query.id"),
        primary_key=True,
        index=True,
    )
    tag_id = sa.Column(
        sa.Integer(),
        sa.ForeignKey("tags.id"),
        primary_key=True,
        index=True,
    )

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

    id = sa.Column(sa.Integer(), primary_key=True)
    user_id = sa.Column(sa.String(), sa.ForeignKey("users.id"), nullable=False)
    name = sa.Column(sa.String(), nullable=False)
    sa.UniqueConstraint("user_id", "name", name="uq_user_endpoint")


class Query(Base):
    """Model class for the query table."""

    __tablename__ = "query"

    id = sa.Column(sa.Integer(), primary_key=True)
    user_id = sa.Column(
        sa.String(),
        sa.ForeignKey("users.id"),
        nullable=False,
        index=True,
    )
    at = sa.Column(sa.TIMESTAMP(), nullable=False)
    model_provider_str = sa.Column(sa.String(), nullable=False)
    endpoint_id = sa.Column(sa.Integer(), sa.ForeignKey("endpoint.id"), index=True)
    custom_endpoint_id = sa.Column(
        sa.Integer(),
        sa.ForeignKey("custom_endpoint.id"),
        index=True,
    )
    local_endpoint_id = sa.Column(
        sa.Integer(),
        sa.ForeignKey("local_endpoint.id"),
        index=True,
    )
    credits = sa.Column(sa.Numeric(), nullable=False)
    query_body = sa.Column(sa.String(), nullable=False)
    response_body = sa.Column(sa.String(), nullable=False)
    signature = sa.Column(sa.String(), nullable=True)
    used_router = sa.Column(sa.Boolean(), nullable=True)
    router = sa.Column(sa.String, nullable=True)
    tags = relationship("QueryTagAssociation", back_populates="query")
    __table_args__ = (sa.Index("ix_user_endpoint", "user_id", "endpoint_id"),)


# TODO: CASCADE DELETE FOR PROMPTS -> EVALUATIONS


class Dataset(Base):
    """Model class for the dataset table."""

    __tablename__ = "dataset"

    id = sa.Column(sa.Integer(), primary_key=True)
    user_id = sa.Column(
        sa.String(),
        sa.ForeignKey("users.id"),
        index=True,
        nullable=True,
    )
    name = sa.Column(sa.String(), nullable=False)
    __table_args__ = (sa.UniqueConstraint("user_id", "name", name="uq_userid_name"),)


class StoredPrompt(Base):
    """Model class for the stored prompt table."""

    __tablename__ = "stored_prompt"

    id = sa.Column(sa.Integer(), primary_key=True)
    user_id = sa.Column(
        sa.String(),
        sa.ForeignKey("users.id"),
        index=True,
        nullable=True,
    )
    prompt = sa.Column(sa.String(), nullable=False)
    ref_answer = sa.Column(sa.String(), nullable=True)
    num_tokens = sa.Column(sa.Integer(), nullable=False)
    timestamp = sa.Column(sa.TIMESTAMP(), nullable=False)


# TODO: Add StoredPromptExtraField
# id, prompt_id, field, value
class StoredPromptExtraField(Base):
    """Model class for the prompt extra field table."""

    __tablename__ = "stored_prompt_extra_field"

    id = sa.Column(sa.Integer(), primary_key=True)
    prompt_id = sa.Column(
        sa.Integer(),
        sa.ForeignKey("stored_prompt.id"),
        index=True,
        nullable=True,
    )
    field = sa.Column(sa.String(), nullable=False)
    value = sa.Column(sa.String(), nullable=False)


class StoredPromptResponse(Base):
    """Model class for the stored prompt response table."""

    __tablename__ = "stored_prompt_response"

    id = sa.Column(sa.Integer(), primary_key=True)
    prompt_id = sa.Column(
        sa.Integer(),
        sa.ForeignKey("stored_prompt.id"),
        index=True,
        nullable=True,
    )
    endpoint_str = sa.Column(sa.String(), nullable=False)
    response = sa.Column(sa.String(), nullable=False)
    num_tokens = sa.Column(sa.Integer(), nullable=False)


class Judgement(Base):
    """Model class for the judgement table."""

    __tablename__ = "judgement"

    id = sa.Column(sa.Integer(), primary_key=True)
    response_id = sa.Column(
        sa.Integer(),
        sa.ForeignKey("stored_prompt_response.id"),
        index=True,
        nullable=True,
    )
    judge_endpoint_id = sa.Column(
        sa.Integer(),
        sa.ForeignKey("endpoint.id"),
        nullable=False,
    )
    judgement = sa.Column(sa.String(), nullable=False)


class DatasetPrompt(Base):
    """Model class for the dataset prompt table."""

    __tablename__ = "dataset_prompt"

    id = sa.Column(sa.Integer(), primary_key=True)
    dataset_id = sa.Column(
        sa.Integer(),
        sa.ForeignKey("dataset.id"),
        index=True,
        nullable=True,
    )
    prompt_id = sa.Column(
        sa.Integer(),
        sa.ForeignKey("stored_prompt.id"),
        index=True,
        nullable=True,
    )


class Evaluator(Base):
    """Model class for the evaluator table."""

    __tablename__ = "evaluator"

    id = sa.Column(sa.Integer(), primary_key=True)
    user_id = sa.Column(
        sa.String(),
        sa.ForeignKey("users.id"),
        index=True,
        nullable=True,
    )
    name = sa.Column(sa.String(), nullable=False)
    system_prompt = sa.Column(sa.String(), nullable=False)
    class_config = sa.Column(sa.String(), nullable=False)
    judge_models = sa.Column(sa.String(), nullable=False)
    client_side = sa.Column(sa.Boolean(), nullable=False)


class Evaluation(Base):
    """Model class for the evaluation table."""

    __tablename__ = "evaluation"

    id = sa.Column(sa.Integer(), primary_key=True)
    prompt_id = sa.Column(
        sa.Integer(),
        sa.ForeignKey("stored_prompt.id"),
        index=True,
        nullable=True,
    )
    evaluator_id = sa.Column(
        sa.Integer(),
        sa.ForeignKey("evaluator.id"),
        index=True,
        nullable=True,
    )
    score = sa.Column(sa.Numeric(), nullable=False)
