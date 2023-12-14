import sqlalchemy as sa

from orchestra.db.base import Base


class Model(Base):
    """Model class for the model table."""

    __tablename__ = "model"

    id = sa.Column(sa.Integer(), primary_key=True)
    model_code = sa.Column(sa.String())
    user_id = sa.Column(sa.String(), sa.ForeignKey("user.id"))
    uploaded_at = sa.Column(sa.TIMESTAMP(), nullable=False)
    task = sa.Column(sa.String(), sa.ForeignKey("task.name"), nullable=False)
    description = sa.Column(sa.Text(), nullable=False)
    license = sa.Column(sa.String(), sa.ForeignKey("license.name"))
    input_args_format = sa.Column(sa.Text(), nullable=False)
    output_format = sa.Column(sa.Text(), nullable=False)
    custom_fields = sa.Column(sa.Text())


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
    model_id = sa.Column(sa.Integer(), sa.ForeignKey("model.id"), nullable=False)
    provider_id = sa.Column(sa.Integer(), sa.ForeignKey("provider.id"), nullable=False)
    created_at = sa.Column(sa.TIMESTAMP(), nullable=False)


class Provider(Base):
    """Model class for the provider table."""

    __tablename__ = "provider"

    id = sa.Column(sa.Integer(), primary_key=True)
    name = sa.Column(sa.String(), nullable=False)
    image_url = sa.Column(sa.String(), nullable=False)
    description = sa.Column(sa.Text())


class Datapoint(Base):
    """Model class for the datapoint table."""

    __tablename__ = "datapoint"

    id = sa.Column(sa.Integer(), primary_key=True)
    endpoint_id = sa.Column(sa.Integer(), sa.ForeignKey("endpoint.id"), nullable=False)
    measured_at = sa.Column(sa.TIMESTAMP(), nullable=False)
    metric_name = sa.Column(sa.String(), sa.ForeignKey("metric.name"), nullable=False)
    value = sa.Column(sa.Numeric(), nullable=False)


class Metric(Base):
    """Model class for the metric table."""

    __tablename__ = "metric"

    name = sa.Column(sa.String(), primary_key=True)


class Query(Base):
    """Model class for the query table."""

    __tablename__ = "query"

    id = sa.Column(sa.Integer(), primary_key=True)
    user_id = sa.Column(sa.String(), sa.ForeignKey("user.id"), nullable=False)
    at = sa.Column(sa.TIMESTAMP(), nullable=False)
    endpoint_id = sa.Column(sa.Integer(), sa.ForeignKey("endpoint.id"), nullable=False)
    credits = sa.Column(sa.Numeric(), nullable=False)


class User(Base):
    """Model class for the user table."""

    __tablename__ = "user"

    id = sa.Column(sa.String(), primary_key=True)
    credits = sa.Column(sa.Numeric(), nullable=False)


class Recharge(Base):
    """Model class for the recharge table."""

    __tablename__ = "recharge"

    id = sa.Column(sa.Integer(), primary_key=True)
    at = sa.Column(sa.TIMESTAMP(), nullable=False)
    user_id = sa.Column(sa.String(), sa.ForeignKey("user.id"), nullable=False)
    quantity = sa.Column(sa.Numeric(), nullable=False)
    type = sa.Column(sa.String(), sa.ForeignKey("recharge_type.type"), nullable=False)


class RechargeType(Base):
    """Model class for the recharge_type table."""

    __tablename__ = "recharge_type"

    type = sa.Column(sa.String(), primary_key=True)
