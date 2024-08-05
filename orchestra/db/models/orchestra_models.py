import sqlalchemy as sa

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


class Query(Base):
    """Model class for the query table."""

    __tablename__ = "query"

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


class DatasetEvaluationTask(Base):
    """Model class for the dataset evaluation task table."""

    __tablename__ = "dataset_evaluation_task"

    id = sa.Column(sa.Integer(), primary_key=True)
    user_id = sa.Column(sa.String(), sa.ForeignKey("users.id"), nullable=True)
    name = sa.Column(sa.String(), nullable=False)
    status = sa.Column(sa.String(), nullable=False)
    sa.UniqueConstraint("user_id", "name", name="uq_dataset_eval_user_id")


class DatasetEvaluation(Base):
    """Model class for the dataset evaluation table."""

    __tablename__ = "dataset_evaluation"

    mdl_name = sa.Column(sa.String(), nullable=False, primary_key=True)
    dataset_name = sa.Column(sa.String(), nullable=False, primary_key=True)
    prompt = sa.Column(sa.String(), nullable=False, primary_key=True)
    gt_score = sa.Column(sa.Numeric(), nullable=False)
    score = sa.Column(sa.Numeric(), nullable=False)
    input_tokens = sa.Column(sa.Numeric(), nullable=True)
    output_tokens = sa.Column(sa.Numeric(), nullable=True)


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
