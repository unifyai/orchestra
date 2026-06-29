#!/usr/bin/env python3
"""Bootstrap self-host platform defaults."""

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
        "ok": result.ok,
        "created_user": False,
        "created_coordinator": False,
    }
    output_path = Path(
        os.environ.get("SELF_HOST_BOOTSTRAP_OUTPUT", "/tmp/self-host-bootstrap.json"),
    )
    output_path.write_text(json.dumps(payload), encoding="utf-8")
    print(
        "Self-host platform bootstrap ready. Register in Console to create the owner.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
