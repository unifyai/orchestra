from sqlalchemy import Boolean, Date, DateTime, Float, Integer, Interval, String, Time
from sqlalchemy.dialects.postgresql import JSONB

__all__ = ["STR_TO_SQL_TYPES"]

STR_TO_SQL_TYPES = {
    "bool": Boolean,
    "int": Integer,
    "float": Float,
    "str": String,
    "datetime": DateTime,
    "time": Time,
    "date": Date,
    "timedelta": Interval,
    "dict": JSONB,
    "list": JSONB,
}
