import sqlalchemy as sa

from orchestra.db.base import Base


class Model(Base):

    __tablename__ = "model"

    id = sa.Column(sa.String(), primary_key=True)
    uploaded_by = sa.Column(sa.Integer(), sa.ForeignKey("user.id"), nullable=False)
    uploaded_at = sa.Column(sa.TIMESTAMP(), nullable=False)
    task = sa.Column(sa.String(), sa.ForeignKey("task.name"), nullable=False)
    description = sa.Column(sa.Text(), nullable=False)
    license = sa.Column(sa.String(), sa.ForeignKey("license.name"))
    input_args_format = sa.Column(sa.Text(), nullable=False)
    output_format = sa.Column(sa.Text(), nullable=False)
    custom_fields = sa.Column(sa.Text())


class License(Base):

    __tablename__ = "license"

    name = sa.Column(sa.String(), primary_key=True)
    image_url = sa.Column(sa.String())
    description = sa.Column(sa.Text(), nullable=False)


class Task(Base):

    __tablename__ = "task"

    name = sa.Column(sa.String(), primary_key=True)
    modality = sa.Column(sa.String(), sa.ForeignKey("modality.name"), nullable=False)


class Modality(Base):

    __tablename__ = "modality"

    name = sa.Column(sa.String(), primary_key=True)


class Endpoint(Base):

    __tablename__ = "endpoint"

    id = sa.Column(sa.Integer(), primary_key=True)
    model_id = sa.Column(sa.String(), sa.ForeignKey("model.id"), nullable=False)
    # model_uploaded_by = sa.Column(sa.Integer(), sa.ForeignKey('model.uploaded_by'), nullable=False)
    provider_id = sa.Column(sa.String(), sa.ForeignKey("provider.id"), nullable=False)
    created_at = sa.Column(sa.TIMESTAMP(), nullable=False)


class Provider(Base):

    __tablename__ = "provider"

    id = sa.Column(sa.String(), primary_key=True)
    name = sa.Column(sa.String(), nullable=False)
    image_url = sa.Column(sa.String(), nullable=False)


class Datapoint(Base):

    __tablename__ = "datapoint"

    id = sa.Column(sa.Integer(), primary_key=True)
    endpoint_id = sa.Column(sa.Integer(), sa.ForeignKey("endpoint.id"), nullable=False)
    measured_at = sa.Column(sa.TIMESTAMP(), nullable=False)
    metric_name = sa.Column(sa.String(), sa.ForeignKey("metric.name"), nullable=False)
    value = sa.Column(sa.Numeric(), nullable=False)


class Metric(Base):

    __tablename__ = "metric"

    name = sa.Column(sa.String(), primary_key=True)


class Query(Base):

    __tablename__ = "query"

    id = sa.Column(sa.Integer(), primary_key=True)
    user_id = sa.Column(sa.Integer(), sa.ForeignKey("user.id"), nullable=False)
    at = sa.Column(sa.TIMESTAMP(), nullable=False)
    endpoint_id = sa.Column(sa.Integer(), sa.ForeignKey("endpoint.id"), nullable=False)
    credits = sa.Column(sa.Numeric(), nullable=False)


class User(Base):

    __tablename__ = "user"

    id = sa.Column(sa.Integer(), primary_key=True)
    credits = sa.Column(sa.Numeric(), nullable=False)


class Recharge(Base):

    __tablename__ = "recharge"

    id = sa.Column(sa.Integer(), primary_key=True)
    at = sa.Column(sa.TIMESTAMP(), nullable=False)
    user_id = sa.Column(sa.Integer(), sa.ForeignKey("user.id"), nullable=False)
    quantity = sa.Column(sa.Numeric(), nullable=False)
    type = sa.Column(sa.String(), sa.ForeignKey("recharge_type.type"), nullable=False)


class RechargeType(Base):

    __tablename__ = "recharge_type"

    type = sa.Column(sa.String(), primary_key=True)
