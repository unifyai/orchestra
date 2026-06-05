#!/usr/bin/env python3
"""Bootstrap the self-host owner account and personal Coordinator."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from orchestra.services.self_host_bootstrap import run_self_host_bootstrap
from orchestra.settings import settings


def main() -> int:
    engine = create_engine(str(settings.db_url), pool_pre_ping=True)
    session_factory = sessionmaker(bind=engine)
    result = run_self_host_bootstrap(session_factory)
    payload = {
        "user_id": result.user_id,
        "email": result.email,
        "password": result.password,
        "api_key": result.api_key,
        "coordinator_agent_id": result.coordinator_agent_id,
        "created_user": result.created_user,
        "created_coordinator": result.created_coordinator,
    }
    output_path = Path(
        os.environ.get("SELF_HOST_BOOTSTRAP_OUTPUT", "/tmp/self-host-bootstrap.json"),
    )
    output_path.write_text(json.dumps(payload), encoding="utf-8")
    print(
        f"Self-host bootstrap ready for {payload['email']} "
        f"(Coordinator agent_id={payload['coordinator_agent_id']})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
