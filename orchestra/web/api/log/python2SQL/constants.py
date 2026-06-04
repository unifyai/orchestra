from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, Interval, String, Time
from sqlalchemy.dialects.postgresql import JSONB

__all__ = [
    "STR_TO_SQL_TYPES",
    "get_default_value_for_type",
]

# Type mapping used by the query builder.
# These types are used to cast extracted JSON text values
# (e.g., data->>'field') to the appropriate SQL type for comparisons and arithmetic.
# Example: STR_TO_SQL_TYPES['float'] -> Float, used for cast(data->>'score', Float)
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


def get_default_value_for_type(type_name: str) -> Any:
    """
    Get the default/initial value for a given type name.

    Args:
        type_name: The type name (e.g., "int", "str", "datetime")

    Returns:
        The appropriate default value for the type
    """
    default_values = {
        "int": 0,
        "float": 0.0,
        "bool": False,
        "str": "",
        "datetime": datetime.min,
        "time": time.min,
        "date": date.min,
        "timedelta": timedelta(),
        "dict": {},
        "list": [],
        "nonetype": None,
    }

    return default_values.get(
        type_name.lower(),
        "",
    )  # Default to empty string for unknown types
