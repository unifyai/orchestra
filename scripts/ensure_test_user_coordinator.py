#!/usr/bin/env python3
"""Ensure the local Orchestra test user has a personal Coordinator."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from orchestra.db.models.orchestra_models import User
from orchestra.services.coordinator_service import (
    ensure_personal_coordinator_provisioned,
)
from orchestra.settings import settings


async def ensure_test_user_coordinator(
    session: Session,
    user_id: str,
) -> tuple[int, bool]:
    """Provision or repair the local test user's personal Coordinator."""
    user = session.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise ValueError(f"Local test user does not exist: {user_id}")

    previous_self_host = os.environ.get("SELF_HOST")
    os.environ["SELF_HOST"] = "1"
    try:
        coordinator, created = await ensure_personal_coordinator_provisioned(
            session,
            user_id=user_id,
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        if previous_self_host is None:
            os.environ.pop("SELF_HOST", None)
        else:
            os.environ["SELF_HOST"] = previous_self_host

    return coordinator.agent_id, created


def run(user_id: str) -> tuple[int, bool]:
    """Open a DB session and ensure the requested user's Coordinator."""
    engine = create_engine(str(settings.db_url), pool_pre_ping=True)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    try:
        return asyncio.run(ensure_test_user_coordinator(session, user_id))
    finally:
        session.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", required=True)
    args = parser.parse_args()

    coordinator_agent_id, created = run(args.user_id)
    action = "created" if created else "ready"
    print(
        f"Local test user Coordinator {action}: agent_id={coordinator_agent_id}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
