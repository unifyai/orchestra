"""Idempotent billing defaults for a fresh self-host Postgres volume."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from orchestra.services.self_host_bootstrap import ensure_platform_billing_defaults
from orchestra.settings import settings


def main() -> int:
    engine = create_engine(str(settings.db_url))
    session_factory = sessionmaker(bind=engine)
    with session_factory() as session:
        ensure_platform_billing_defaults(session)
        session.commit()
    print("Self-host billing defaults ensured.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
