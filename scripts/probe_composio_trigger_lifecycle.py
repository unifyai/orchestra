#!/usr/bin/env python3
"""Probe Composio trigger create/delete lifecycle behavior.

Requires:
  - COMPOSIO_API_KEY in the environment or `.env.provider-probes`
  - COMPOSIO_PROBE_CONNECTED_ACCOUNT_ID from a disposable GitHub connection
  - Optional COMPOSIO_PROBE_USER_ID (defaults to assistant:provider-trigger-probe)

Example:
  source .env.provider-probes
  export COMPOSIO_PROBE_CONNECTED_ACCOUNT_ID=ca_...
  ./.venv/bin/python scripts/probe_composio_trigger_lifecycle.py
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
PROBE_ENV = REPO_ROOT / ".env.provider-probes"


def _load_probe_env() -> None:
    if PROBE_ENV.is_file():
        for line in PROBE_ENV.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key, value)


def _api_key() -> str:
    value = os.getenv("COMPOSIO_API_KEY", "").strip()
    if not value:
        raise SystemExit("COMPOSIO_API_KEY is required")
    return value


def _base_url() -> str:
    return os.getenv(
        "COMPOSIO_BASE_URL",
        "https://backend.composio.dev/api/v3.1",
    ).rstrip("/")


def _headers() -> dict[str, str]:
    return {"x-api-key": _api_key(), "Content-Type": "application/json"}


def main() -> int:
    _load_probe_env()
    connected_account_id = os.getenv("COMPOSIO_PROBE_CONNECTED_ACCOUNT_ID", "").strip()
    if not connected_account_id:
        print(
            "Missing COMPOSIO_PROBE_CONNECTED_ACCOUNT_ID.\n"
            "Create a disposable Composio GitHub connection in Console, then rerun with:\n"
            "  export COMPOSIO_PROBE_CONNECTED_ACCOUNT_ID=<connected_account_id>",
            file=sys.stderr,
        )
        return 2

    user_id = os.getenv(
        "COMPOSIO_PROBE_USER_ID",
        "assistant:provider-trigger-probe",
    ).strip()
    owner = os.getenv("COMPOSIO_PROBE_REPO_OWNER", "octocat").strip()
    repo = os.getenv("COMPOSIO_PROBE_REPO_NAME", "Hello-World").strip()
    idempotency_key = f"provider-trigger-{uuid.uuid4().hex}"

    create_payload = {
        "slug": "GITHUB_ISSUE_CREATED_TRIGGER",
        "user_id": user_id,
        "connected_account_id": connected_account_id,
        "trigger_config": {"owner": owner, "repo": repo},
        "idempotency_key": idempotency_key,
    }
    create_response = requests.post(
        f"{_base_url()}/trigger_instances",
        headers=_headers(),
        json=create_payload,
        timeout=60,
    )
    print("create_status", create_response.status_code)
    if create_response.status_code >= 400:
        print(create_response.text[:1000], file=sys.stderr)
        return 1

    created = create_response.json()
    trigger_id = (
        created.get("id")
        or created.get("trigger_id")
        or (created.get("data") or {}).get("id")
    )
    print("trigger_id", trigger_id)
    print("idempotency_key", idempotency_key)

    replay_response = requests.post(
        f"{_base_url()}/trigger_instances",
        headers=_headers(),
        json=create_payload,
        timeout=60,
    )
    print("replay_status", replay_response.status_code)
    replay_body = replay_response.json()
    replay_id = (
        replay_body.get("id")
        or replay_body.get("trigger_id")
        or (replay_body.get("data") or {}).get("id")
    )
    print("replay_trigger_id", replay_id)
    print("idempotent_create", trigger_id == replay_id)

    if trigger_id:
        delete_response = requests.delete(
            f"{_base_url()}/trigger_instances/{trigger_id}",
            headers=_headers(),
            timeout=60,
        )
        print("delete_status", delete_response.status_code)

    evidence = {
        "provider_trigger_slug": "GITHUB_ISSUE_CREATED_TRIGGER",
        "connected_account_id": connected_account_id,
        "idempotency_key": idempotency_key,
        "trigger_id": trigger_id,
        "replay_trigger_id": replay_id,
        "idempotent_create": trigger_id == replay_id,
    }
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
