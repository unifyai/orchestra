"""
Light-weight SessionLocal helper.

• In production we build the engine from `settings.db_url`.
• Inside pytest (where `settings` may be missing) we fall back to SQLite.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

try:
    # Adjust the import path if your Settings object lives elsewhere
    from orchestra.settings import settings  # type: ignore

    _ENGINE = create_engine(str(settings.db_url), pool_pre_ping=True, future=True)
except Exception:  # pragma: no cover
    # Local test-run without Postgres / settings → use in-memory SQLite
    _ENGINE = create_engine("sqlite:///:memory:", future=True)

SessionLocal = sessionmaker(bind=_ENGINE, expire_on_commit=False)
