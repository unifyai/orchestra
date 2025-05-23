from sqlalchemy.orm import DeclarativeBase

# --------------------------------------------------------------------------- #
# SQLite test-suite compatibility: make JSONB render as plain JSON            #
# --------------------------------------------------------------------------- #
from orchestra.db import sqlite_compat  # noqa: F401  (side-effect import)
from orchestra.db.meta import meta


class Base(DeclarativeBase):
    """Base for all models."""

    metadata = meta
