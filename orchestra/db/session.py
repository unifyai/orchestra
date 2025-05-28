"""
Light-weight SessionLocal helper.

Uses PostgreSQL from settings.db_url for all environments.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from orchestra.settings import settings

_ENGINE = create_engine(str(settings.db_url), pool_pre_ping=True, future=True)

SessionLocal = sessionmaker(bind=_ENGINE, expire_on_commit=False)
