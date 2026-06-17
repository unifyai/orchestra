#!/usr/bin/env python3
"""Run Builtins artifacts seeding directly for self-host deployments.

This is the first-class non-GCP path. It writes the same integrations artifact
request payload used by hosted Cloud Run Jobs, then invokes
``orchestra.workers.builtins_artifacts_seed_job`` with ``--request-file`` so
reruns with the same manifest hash resume from Builtins ``Integrations/Meta``
checkpoints.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from orchestra.workers import builtins_artifacts_seed_job

SEED_OWNER = "public-builtins"
SYNC_PASSTHROUGH_FIELDS = {
    "tool_limit_per_app",
    "component_limit_per_app",
    "include_all_managed_apps",
    "include_all_apps",
    "create_auth_configs",
    "sync_tools",
    "prune_unlisted_apps",
}


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _load_manifest(path: Path) -> dict[str, Any]:
    with path.open("rb") as file:
        if path.suffix == ".json":
            manifest = json.loads(file.read().decode("utf-8"))
        else:
            manifest = tomllib.load(file)
    if not isinstance(manifest.get("providers"), dict):
        raise ValueError(
            "Self-host integration manifest must contain a providers table",
        )
    return manifest


def build_request_from_manifest(
    manifest: dict[str, Any],
    *,
    backend_id: str,
    workers: int,
    batch_size: int,
) -> dict[str, Any]:
    environment = str(manifest.get("environment") or "selfhost")
    if environment != "selfhost":
        raise ValueError("self-host runner only supports environment='selfhost'")
    config = manifest["providers"].get(backend_id)
    if not isinstance(config, dict):
        raise ValueError(f"{backend_id}: provider config not found")
    if config.get("status") != "enabled":
        raise ValueError(f"{backend_id}: provider must be enabled for self-host sync")
    sync = config.get("sync")
    if not isinstance(sync, dict):
        raise ValueError(f"{backend_id}: sync config is required")
    mode = sync.get("mode", "partial")
    if mode not in {"partial", "full"}:
        raise ValueError(f"{backend_id}: sync.mode must be partial or full")

    backend_payload = {
        "backend_id": backend_id,
        "kind": config.get("kind") or backend_id,
        "environment": environment,
        "display_name": config.get("display_name") or backend_id.title(),
        "status": config.get("status", "disabled"),
        "allowed_orgs_or_tenants": config.get("allowed_orgs_or_tenants") or [],
        "default_priority": int(config.get("default_priority", 100)),
        "config_json": config.get("config_json") or {},
    }
    sync_payload: dict[str, Any] = {
        "backend_id": backend_id,
        "app_slugs": [] if mode == "full" else list(sync.get("app_slugs") or []),
        "sync_mode": mode,
    }
    for field in SYNC_PASSTHROUGH_FIELDS:
        if field in sync:
            sync_payload[field] = sync[field]
    if mode == "full":
        if "include_all_managed_apps" in sync_payload:
            sync_payload["include_all_managed_apps"] = True
        if "include_all_apps" in sync_payload:
            sync_payload["include_all_apps"] = True

    desired_config = {
        "schema_version": manifest.get("schema_version", 1),
        "environment": environment,
        "seed_owner": SEED_OWNER,
        "artifact_kind": "integrations",
        "backend": backend_payload,
        "sync": {**sync_payload, "mode": mode},
    }
    desired_hash = hashlib.sha256(_json_dumps(desired_config).encode()).hexdigest()
    sync_payload["cache_version"] = (
        f"{SEED_OWNER}-{environment}-{backend_id}-{desired_hash[:12]}"
    )
    return {
        "artifact_kind": "integrations",
        "backend_id": backend_id,
        "environment": environment,
        "desired_hash": desired_hash,
        "desired_config": desired_config,
        "cache_version": sync_payload["cache_version"],
        "mode": "all",
        "app_slugs": list(sync_payload.get("app_slugs") or []),
        "prune_unlisted_apps": bool(sync_payload.get("prune_unlisted_apps", False)),
        "sync_payload": sync_payload,
        "batch_size": int(batch_size),
        "workers": int(workers),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        required=True,
        help="Self-host bootstrap TOML/JSON.",
    )
    parser.add_argument("--backend-id", default="composio")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument(
        "--write-request-file",
        default="",
        help="Write the request JSON here before running the worker.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the request JSON and do not run the worker.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    payload = build_request_from_manifest(
        _load_manifest(Path(args.manifest)),
        backend_id=args.backend_id,
        workers=args.workers,
        batch_size=args.batch_size,
    )
    if args.dry_run:
        print(_json_dumps(payload))
        return 0
    if args.write_request_file:
        request_path = Path(args.write_request_file)
        request_path.write_text(_json_dumps(payload), encoding="utf-8")
        return builtins_artifacts_seed_job.main(["--request-file", str(request_path)])
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json") as file:
        file.write(_json_dumps(payload))
        file.flush()
        return builtins_artifacts_seed_job.main(["--request-file", file.name])


if __name__ == "__main__":
    raise SystemExit(main())
