# Load .env BEFORE importing orchestra - settings are evaluated at import time
from pathlib import Path

from dotenv import load_dotenv

# Look for .env in repo root
_repo_root = Path(__file__).resolve().parent
load_dotenv(_repo_root / ".env", override=True)

# Re-export everything from orchestra/conftest.py
from orchestra.conftest import *  # noqa: E402, F401, F403
