# Load .env BEFORE importing orchestra - settings are evaluated at import time
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# Look for .env in repo root
_repo_root = Path(__file__).resolve().parent
load_dotenv(_repo_root / ".env", override=True)


# ---------------------------------------------------------------------------
# Log directory configuration
# ---------------------------------------------------------------------------


def _get_log_subdir() -> str:
    """Generate a datetime-prefixed subdirectory name for log isolation."""
    timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    # Use a simple identifier (PID) for this repo
    return f"{timestamp}_orchestrapid{os.getpid()}"


def pytest_sessionstart(session):
    """Configure Orchestra log directory for trace correlation."""
    root_path = Path(session.config.rootpath)
    subdir = _get_log_subdir()

    # Orchestra per-request trace logging
    orchestra_log_dir = root_path / "logs" / "orchestra" / subdir
    orchestra_log_dir.mkdir(parents=True, exist_ok=True)
    os.environ["ORCHESTRA_LOG_DIR"] = str(orchestra_log_dir)


# Re-export everything from orchestra/conftest.py
from orchestra.conftest import *  # noqa: E402, F401, F403
