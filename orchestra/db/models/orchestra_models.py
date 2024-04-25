import sqlalchemy as sa

from orchestra.db.base import Base


class Model(Base):
    """Model class for the model table."""

    __tablename__ = "model"

    id = sa.Column(sa.Integer(), primary_key=True)
    mdl_code = sa.Column(sa.String())
    user_id = sa.Column(sa.String(), sa.ForeignKey("users.id"))
    uploaded_at = sa.Column(sa.TIMESTAMP(), nullable=False)
    task = sa.Column(sa.String(), sa.ForeignKey("task.name"), nullable=False)
    description = sa.Column(sa.Text(), nullable=False)
    license = sa.Column(sa.String(), sa.ForeignKey("license.name"))
    input_args_format = sa.Column(sa.Text(), nullable=False)
    output_format = sa.Column(sa.Text(), nullable=False)
    custom_fields = sa.Column(sa.Text())
    active = sa.Column(
        sa.Boolean(),
        server_default="f",
        nullable=False,
    )  # type: ignore
    is_private = sa.Column(
        sa.Boolean(),
        server_default="f",
        nullable=False,
    )  # type: ignore


class License(Base):
    """Model class for the license table."""

    __tablename__ = "license"

    name = sa.Column(sa.String(), primary_key=True)
    image_url = sa.Column(sa.String())
    description = sa.Column(sa.Text(), nullable=False)


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
    description = sa.Column(sa.Text())


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
    sa.UniqueConstraint("user_id", "name", name="user_id_dataset_name")


class DatasetEvaluation(Base):
    """Model class for the dataset evaluation table."""

    __tablename__ = "dataset_evaluation"

    mdl_name = sa.Column(sa.String(), nullable=False, primary_key=True)
    dataset_name = sa.Column(sa.String(), nullable=False, primary_key=True)
    prompt = sa.Column(sa.String(), nullable=False, primary_key=True)
    gt_score = sa.Column(sa.Numeric(), nullable=False)
    score = sa.Column(sa.Numeric(), nullable=False)
    metric = sa.Column(sa.String(), nullable=True)
