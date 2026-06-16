"""Cloud deploy bootstrap for provider-backed integrations.

The source of truth is a non-secret manifest committed under ``deploy/``.
Provider credentials stay on the deployed Orchestra Cloud Run service as
Secret Manager-backed environment variables; this script never reads or prints
those credential values.
"""

from __future__ import annotations

import argparse
import builtins
import hashlib
import json
import os
import sys
import threading
import time
import tomllib
from dataclasses import dataclass
from functools import partial
from typing import Any
from urllib import error, parse, request

print = partial(builtins.print, flush=True)

BOOTSTRAP_STATUS_SUCCESS = "success"
DEFAULT_SYNC_BATCH_SIZE = 25
DEFAULT_REQUEST_TIMEOUT_SECONDS = 120
REQUEST_HEARTBEAT_SECONDS = 30
SYNC_PASSTHROUGH_FIELDS = {
    "tool_limit_per_app",
    "component_limit_per_app",
    "include_all_managed_apps",
    "include_all_apps",
    "create_auth_configs",
    "sync_tools",
    "prune_unlisted_apps",
}


class SyncFailed(RuntimeError):
    """Raised after a structured failed sync result has already been persisted."""


@dataclass(frozen=True)
class ProviderPlan:
    backend_id: str
    desired_hash: str
    desired_config: dict[str, Any]
    backend_payload: dict[str, Any]
    sync_payload: dict[str, Any] | None


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _load_manifest(path: str) -> dict[str, Any]:
    with open(path, "rb") as file:
        if path.endswith(".json"):
            manifest = json.loads(file.read().decode("utf-8"))
        else:
            manifest = tomllib.load(file)
    if not isinstance(manifest, dict):
        raise ValueError("Manifest must be a JSON object")
    if manifest.get("environment") not in {"staging", "production", "selfhost"}:
        raise ValueError(
            "Manifest environment must be staging, production, or selfhost",
        )
    if not isinstance(manifest.get("providers"), dict):
        raise ValueError("Manifest providers must be an object")
    return manifest


def _csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _backend_payload(
    *,
    backend_id: str,
    environment: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    status = config.get("status", "disabled")
    if status not in {"enabled", "disabled"}:
        raise ValueError(f"{backend_id}: status must be enabled or disabled")
    return {
        "backend_id": backend_id,
        "kind": config.get("kind") or backend_id,
        "environment": environment,
        "display_name": config.get("display_name") or backend_id.title(),
        "status": status,
        "allowed_orgs_or_tenants": config.get("allowed_orgs_or_tenants") or [],
        "default_priority": int(config.get("default_priority", 100)),
        "config_json": config.get("config_json") or {},
    }


def _sync_payload(
    *,
    backend_id: str,
    config: dict[str, Any],
) -> dict[str, Any] | None:
    if config.get("status") != "enabled":
        return None
    sync = config.get("sync")
    if not sync:
        return None
    mode = sync.get("mode", "partial")
    if mode not in {"partial", "full"}:
        raise ValueError(f"{backend_id}: sync.mode must be partial or full")
    payload: dict[str, Any] = {
        "backend_id": backend_id,
        "app_slugs": [] if mode == "full" else list(sync.get("app_slugs") or []),
        "sync_mode": mode,
    }
    for field in SYNC_PASSTHROUGH_FIELDS:
        if field in sync:
            payload[field] = sync[field]
    if mode == "full":
        if "include_all_managed_apps" in payload:
            payload["include_all_managed_apps"] = True
        if "include_all_apps" in payload:
            payload["include_all_apps"] = True
    return payload


def _provider_plan(
    *,
    manifest: dict[str, Any],
    backend_id: str,
    config: dict[str, Any],
) -> ProviderPlan:
    environment = manifest["environment"]
    backend_payload = _backend_payload(
        backend_id=backend_id,
        environment=environment,
        config=config,
    )
    sync_payload = _sync_payload(backend_id=backend_id, config=config)
    desired_sync_config = None
    if sync_payload:
        desired_sync_config = {
            **sync_payload,
            "mode": sync_payload.get("sync_mode", "partial"),
        }
    desired_config = {
        "schema_version": manifest.get("schema_version", 1),
        "environment": environment,
        "backend": backend_payload,
        "sync": desired_sync_config,
    }
    desired_hash = hashlib.sha256(_json_dumps(desired_config).encode()).hexdigest()
    if sync_payload:
        sync_payload = {
            **sync_payload,
            "cache_version": f"cloud-bootstrap-{environment}-{backend_id}-{desired_hash[:12]}",
        }
    return ProviderPlan(
        backend_id=backend_id,
        desired_hash=desired_hash,
        desired_config=desired_config,
        backend_payload=backend_payload,
        sync_payload=sync_payload,
    )


def provider_plans(
    manifest: dict[str, Any],
    *,
    providers: list[str] | None = None,
) -> list[ProviderPlan]:
    selected = set(providers or manifest["providers"].keys())
    plans: list[ProviderPlan] = []
    for backend_id, config in sorted(manifest["providers"].items()):
        if backend_id not in selected:
            continue
        if not isinstance(config, dict):
            raise ValueError(f"{backend_id}: provider config must be an object")
        plans.append(
            _provider_plan(
                manifest=manifest,
                backend_id=backend_id,
                config=config,
            ),
        )
    missing = selected - {plan.backend_id for plan in plans}
    if missing:
        raise ValueError(
            f"Selected providers not in manifest: {', '.join(sorted(missing))}",
        )
    return plans


class AdminClient:
    def __init__(
        self,
        *,
        base_url: str,
        admin_key: str,
        timeout_seconds: int = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ):
        self.base_url = base_url.rstrip("/")
        self.admin_key = admin_key
        self.timeout_seconds = timeout_seconds

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        started_at = time.perf_counter()
        context = self._request_context(payload)
        print(
            f"admin request start method={method} path={path} "
            f"timeout={self.timeout_seconds}s{context}",
        )
        req = request.Request(
            f"{self.base_url}/{path.lstrip('/')}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.admin_key}",
                "Content-Type": "application/json",
                "accept": "application/json",
            },
        )
        done = threading.Event()
        heartbeat = threading.Thread(
            target=self._request_heartbeat,
            args=(done, method, path, started_at),
            daemon=True,
        )
        heartbeat.start()
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
                elapsed = time.perf_counter() - started_at
                print(
                    f"admin request complete method={method} path={path} "
                    f"status={response.status} elapsed={elapsed:.1f}s",
                )
                return json.loads(body) if body else {}
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            elapsed = time.perf_counter() - started_at
            print(
                f"admin request failed method={method} path={path} "
                f"status={exc.code} elapsed={elapsed:.1f}s",
            )
            raise RuntimeError(
                f"{method} {path} failed with HTTP {exc.code}: {detail}",
            ) from exc
        finally:
            done.set()

    @staticmethod
    def _request_context(payload: dict[str, Any] | None) -> str:
        if not payload:
            return ""
        parts: list[str] = []
        for field in (
            "backend_id",
            "sync_mode",
            "sync_tools",
            "include_all_managed_apps",
            "include_all_apps",
            "prune_unlisted_apps",
        ):
            if field in payload:
                parts.append(f"{field}={payload[field]}")
        if "app_slugs" in payload:
            parts.append(f"app_slugs={len(payload.get('app_slugs') or [])}")
        if "apps" in payload:
            parts.append(f"apps={len(payload.get('apps') or [])}")
        if "tools" in payload:
            parts.append(f"tools={len(payload.get('tools') or [])}")
        return f" {' '.join(parts)}" if parts else ""

    @staticmethod
    def _request_heartbeat(
        done: threading.Event,
        method: str,
        path: str,
        started_at: float,
    ) -> None:
        while not done.wait(REQUEST_HEARTBEAT_SECONDS):
            elapsed = time.perf_counter() - started_at
            print(
                f"admin request waiting method={method} path={path} "
                f"elapsed={elapsed:.1f}s",
            )

    def bootstrap_state(
        self,
        *,
        environment: str,
        backend_id: str,
    ) -> dict[str, Any] | None:
        query = parse.urlencode(
            {
                "environment": environment,
                "backend_id": backend_id,
            },
        )
        try:
            return self.request("GET", f"/admin/integrations/bootstrap-state?{query}")
        except RuntimeError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise

    def put_bootstrap_state(
        self,
        *,
        environment: str,
        plan: ProviderPlan,
        status: str,
        result: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> None:
        diagnostics = _sync_diagnostics(plan=plan, result=result)
        if error_message:
            diagnostics["error"] = error_message[:1000]
        payload = {
            "environment": environment,
            "backend_id": plan.backend_id,
            "desired_hash": plan.desired_hash,
            "desired_config": plan.desired_config,
            "last_status": status,
            "last_error": error_message[:1000] if error_message else None,
            "apps_upserted": int((result or {}).get("apps_upserted", 0)),
            "tools_upserted": int((result or {}).get("tools_upserted", 0)),
            "last_sync_diagnostics": diagnostics,
        }
        self.request("PUT", "/admin/integrations/bootstrap-state", payload)


def _sync_diagnostics(
    *,
    plan: ProviderPlan,
    result: dict[str, Any] | None,
) -> dict[str, Any]:
    sync_config = plan.desired_config.get("sync") or {}
    sync_mode = sync_config.get("mode") or sync_config.get("sync_mode")
    diagnostics: dict[str, Any] = {
        "sync_mode": sync_mode,
        "requested_app_slugs": list(sync_config.get("app_slugs") or []),
        "prune_unlisted_apps": bool(sync_config.get("prune_unlisted_apps", False)),
    }
    if result:
        for field in (
            "status",
            "error",
            "warning",
            "auth_configs_created",
            "auth_configs_reused",
            "cache_version",
            "prune_unlisted_apps",
        ):
            if field in result:
                diagnostics[field] = result[field]
        if sync_mode != "full":
            for field in ("skipped_apps", "matched_app_slugs"):
                if field in result:
                    diagnostics[field] = result[field]
    return diagnostics


def _print_sync_result(backend_id: str, result: dict[str, Any]) -> None:
    skipped_apps = result.get("skipped_apps") or []
    print(
        f"{backend_id}: sync result "
        f"status={result.get('status', 'success')} "
        f"apps={result.get('apps_upserted', 0)} "
        f"tools={result.get('tools_upserted', 0)} "
        f"matched={len(result.get('matched_app_slugs') or [])} "
        f"skipped={len(skipped_apps)} "
        f"cache_version={result.get('cache_version')}",
    )
    if skipped_apps:
        preview = ", ".join(
            f"{item.get('slug')}:{item.get('reason')}"
            for item in skipped_apps[:5]
            if isinstance(item, dict)
        )
        print(f"{backend_id}: skipped_apps={preview}")
    if result.get("error") or result.get("warning"):
        print(
            f"{backend_id}: sync diagnostic "
            f"error={result.get('error')} warning={result.get('warning')}",
        )


def _merge_sync_results(
    *,
    base: dict[str, Any] | None,
    batch: dict[str, Any],
    app_count: int | None = None,
) -> dict[str, Any]:
    merged = dict(base or {})
    merged["status"] = batch.get("status") or merged.get("status") or "success"
    if app_count is not None:
        merged["apps_upserted"] = app_count
    else:
        merged["apps_upserted"] = int(merged.get("apps_upserted", 0) or 0) + int(
            batch.get("apps_upserted", 0) or 0,
        )
    merged["tools_upserted"] = int(merged.get("tools_upserted", 0) or 0) + int(
        batch.get("tools_upserted", 0) or 0,
    )
    merged["skipped_apps"] = [
        *(merged.get("skipped_apps") or []),
        *(batch.get("skipped_apps") or []),
    ]
    merged["apps"] = [
        *(merged.get("apps") or []),
        *(batch.get("apps") or []),
    ]
    merged["tools"] = [
        *(merged.get("tools") or []),
        *(batch.get("tools") or []),
    ]
    matched = {
        str(slug)
        for slug in [
            *(merged.get("matched_app_slugs") or []),
            *(batch.get("matched_app_slugs") or []),
        ]
        if slug
    }
    merged["matched_app_slugs"] = sorted(matched)
    for field in (
        "cache_version",
        "warning",
        "error",
    ):
        if batch.get(field) is not None:
            merged[field] = batch[field]
    merged["auth_configs_created"] = int(
        merged.get("auth_configs_created", 0) or 0,
    ) + int(batch.get("auth_configs_created", 0) or 0)
    merged["auth_configs_reused"] = int(
        merged.get("auth_configs_reused", 0) or 0,
    ) + int(batch.get("auth_configs_reused", 0) or 0)
    return merged


def _chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _should_batch_composio_full_sync(plan: ProviderPlan) -> bool:
    payload = plan.sync_payload or {}
    return (
        plan.backend_id == "composio"
        and payload.get("sync_mode") == "full"
        and bool(payload.get("include_all_managed_apps"))
        and bool(payload.get("sync_tools", True))
    )


def _batched_composio_sync(
    *,
    client: AdminClient,
    environment: str,
    plan: ProviderPlan,
    batch_size: int,
) -> dict[str, Any]:
    assert plan.sync_payload is not None
    print(
        f"{plan.backend_id}: syncing app catalog before tool batches "
        f"batch_size={batch_size}",
    )
    app_payload = {
        **plan.sync_payload,
        "sync_tools": False,
        "app_slugs": [],
        "include_all_managed_apps": True,
    }
    app_result = client.request("POST", "/admin/integrations/sync", app_payload)
    app_count = int(app_result.get("apps_upserted", 0) or 0)
    aggregate = _merge_sync_results(base=None, batch=app_result, app_count=app_count)
    aggregate["sync_mode"] = plan.sync_payload.get("sync_mode") or "full"
    aggregate["requested_app_slugs"] = list(plan.sync_payload.get("app_slugs") or [])
    client.put_bootstrap_state(
        environment=environment,
        plan=plan,
        status=aggregate.get("status") or BOOTSTRAP_STATUS_SUCCESS,
        result=aggregate,
    )
    _print_sync_result(plan.backend_id, app_result)
    if app_result.get("status") == "failed":
        raise SyncFailed(
            app_result.get("error") or f"{plan.backend_id}: app catalog sync failed",
        )

    matched_slugs = [str(slug) for slug in app_result.get("matched_app_slugs") or []]
    if not matched_slugs:
        raise SyncFailed(f"{plan.backend_id}: app catalog sync returned no app slugs")

    batches = _chunks(matched_slugs, max(1, batch_size))
    for index, batch_slugs in enumerate(batches, start=1):
        print(
            f"{plan.backend_id}: syncing tool batch {index}/{len(batches)} "
            f"apps={len(batch_slugs)} first={batch_slugs[0]}",
        )
        batch_payload = {
            **plan.sync_payload,
            "sync_mode": "partial",
            "app_slugs": [slug.upper() for slug in batch_slugs],
            "include_all_managed_apps": False,
            "sync_tools": True,
            "prune_unlisted_apps": False,
        }
        batch_result = client.request(
            "POST",
            "/admin/integrations/sync",
            batch_payload,
        )
        aggregate = _merge_sync_results(
            base=aggregate,
            batch=batch_result,
            app_count=app_count,
        )
        client.put_bootstrap_state(
            environment=environment,
            plan=plan,
            status=aggregate.get("status") or BOOTSTRAP_STATUS_SUCCESS,
            result=aggregate,
        )
        print(
            f"{plan.backend_id}: tool batch {index}/{len(batches)} complete "
            f"batch_apps={batch_result.get('apps_upserted', 0)} "
            f"batch_tools={batch_result.get('tools_upserted', 0)} "
            f"cumulative_tools={aggregate.get('tools_upserted', 0)}",
        )
        if batch_result.get("status") == "failed":
            _print_sync_result(plan.backend_id, batch_result)
            raise SyncFailed(
                batch_result.get("error")
                or f"{plan.backend_id}: tool batch {index} failed",
            )
    return aggregate


def apply_plan(
    *,
    client: AdminClient,
    environment: str,
    plan: ProviderPlan,
    dry_run: bool = False,
    force: bool = False,
) -> str:
    print("=" * 80)
    print(f"{plan.backend_id}: bootstrap start")
    print(f"{plan.backend_id}: environment={environment}")
    print(f"{plan.backend_id}: desired_hash={plan.desired_hash[:12]}")
    print(
        f"{plan.backend_id}: backend status={plan.backend_payload.get('status')} "
        f"priority={plan.backend_payload.get('default_priority')}",
    )
    print(f"{plan.backend_id}: applying backend status/config")
    if dry_run:
        print(f"{plan.backend_id}: dry-run would upsert backend")
    else:
        client.request("POST", "/admin/integrations/backends", plan.backend_payload)

    if plan.sync_payload is None:
        print(f"{plan.backend_id}: no enabled sync configured")
        if not dry_run:
            client.put_bootstrap_state(
                environment=environment,
                plan=plan,
                status=BOOTSTRAP_STATUS_SUCCESS,
            )
        return "applied-no-sync"

    state = (
        None
        if dry_run
        else client.bootstrap_state(
            environment=environment,
            backend_id=plan.backend_id,
        )
    )
    if state:
        print(
            f"{plan.backend_id}: previous state "
            f"status={state.get('last_status')} "
            f"hash={str(state.get('desired_hash') or '')[:12]} "
            f"apps={state.get('apps_upserted', 0)} "
            f"tools={state.get('tools_upserted', 0)}",
        )
    else:
        print(f"{plan.backend_id}: no previous bootstrap state")

    if (
        state
        and state.get("desired_hash") == plan.desired_hash
        and state.get("last_status") in {BOOTSTRAP_STATUS_SUCCESS, "skipped"}
        and not force
    ):
        print(f"{plan.backend_id}: sync skipped, manifest hash already applied")
        if not dry_run:
            previous_diagnostics = state.get("last_sync_diagnostics") or {}
            skip_result = {
                "status": "skipped",
                "apps_upserted": state.get("apps_upserted", 0),
                "tools_upserted": state.get("tools_upserted", 0),
                "cache_version": previous_diagnostics.get("cache_version"),
                "warning": "Manifest hash already applied; catalog sync skipped.",
            }
            sync_config = plan.desired_config.get("sync") or {}
            sync_mode = sync_config.get("mode") or sync_config.get("sync_mode")
            if sync_mode != "full":
                skip_result["skipped_apps"] = previous_diagnostics.get(
                    "skipped_apps",
                    [],
                )
                skip_result["matched_app_slugs"] = previous_diagnostics.get(
                    "matched_app_slugs",
                    [],
                )
            client.put_bootstrap_state(
                environment=environment,
                plan=plan,
                status="skipped",
                result=skip_result,
            )
        return "skipped"

    if dry_run:
        print(f"{plan.backend_id}: dry-run would sync catalog")
        print(f"{plan.backend_id}: sync payload={_json_dumps(plan.sync_payload)}")
        return "dry-run"

    try:
        print(f"{plan.backend_id}: syncing catalog")
        print(
            f"{plan.backend_id}: app_count="
            f"{len(plan.sync_payload.get('app_slugs') or [])} "
            f"sync_mode={plan.sync_payload.get('sync_mode')} "
            f"cache_version={plan.sync_payload.get('cache_version')}",
        )
        if _should_batch_composio_full_sync(plan):
            batch_size = int(
                os.environ.get(
                    "ORCHESTRA_INTEGRATION_BOOTSTRAP_BATCH_SIZE",
                    DEFAULT_SYNC_BATCH_SIZE,
                ),
            )
            result = _batched_composio_sync(
                client=client,
                environment=environment,
                plan=plan,
                batch_size=batch_size,
            )
        else:
            result = client.request(
                "POST",
                "/admin/integrations/sync",
                plan.sync_payload,
            )
        client.put_bootstrap_state(
            environment=environment,
            plan=plan,
            status=result.get("status") or BOOTSTRAP_STATUS_SUCCESS,
            result=result,
        )
        _print_sync_result(plan.backend_id, result)
        if result.get("status") == "failed":
            raise SyncFailed(
                result.get("error") or f"{plan.backend_id}: integration sync failed",
            )
        return "synced"
    except Exception as exc:
        if isinstance(exc, SyncFailed):
            raise
        client.put_bootstrap_state(
            environment=environment,
            plan=plan,
            status="failed",
            error_message=str(exc),
        )
        raise


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply cloud provider integration bootstrap manifest.",
    )
    parser.add_argument("--manifest", required=True)
    parser.add_argument(
        "--admin-key",
        default=os.environ.get("ORCHESTRA_ADMIN_KEY", ""),
        help="Orchestra admin key. Defaults to ORCHESTRA_ADMIN_KEY.",
    )
    parser.add_argument(
        "--providers",
        default="",
        help="Optional comma-separated provider filter.",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    base_url = os.environ.get("ORCHESTRA_URL", "").rstrip("/")
    if not base_url:
        raise SystemExit("ORCHESTRA_URL is required")
    if not args.admin_key and not args.dry_run:
        raise SystemExit("ORCHESTRA_ADMIN_KEY or --admin-key is required")

    manifest = _load_manifest(args.manifest)
    plans = provider_plans(manifest, providers=_csv(args.providers) or None)
    client = AdminClient(
        base_url=base_url,
        admin_key=args.admin_key,
        timeout_seconds=int(
            os.environ.get(
                "ORCHESTRA_INTEGRATION_BOOTSTRAP_REQUEST_TIMEOUT_SECONDS",
                DEFAULT_REQUEST_TIMEOUT_SECONDS,
            ),
        ),
    )
    for plan in plans:
        apply_plan(
            client=client,
            environment=manifest["environment"],
            plan=plan,
            dry_run=args.dry_run,
            force=args.force,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
