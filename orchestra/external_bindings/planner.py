"""Hydrate planner for external_entry columns."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from orchestra.external_bindings.auth import resolve_auth
from orchestra.external_bindings.registry import get_connector
from orchestra.external_bindings.types import BindingItem

# Re-export for callers that historically imported resolve_auth from planner.
__all__ = [
    "HydrateMode",
    "compute_input_hash",
    "hydrate_logs",
    "public_binding_summary",
    "resolve_auth",
    "sidecar_key",
]

EXT_SIDECAR_PREFIX = "__ext__"
DEFAULT_MAX_ROWS = 500
DEFAULT_TTL_SECONDS = 300


class HydrateMode(str, Enum):
    NONE = "none"
    STALE_OK = "stale_ok"
    FORCE = "force"


def sidecar_key(field_name: str) -> str:
    return f"{EXT_SIDECAR_PREFIX}{field_name}"


def public_binding_summary(binding_row: dict[str, Any]) -> dict[str, Any]:
    """Return binding metadata safe for get_fields (no secrets)."""
    binding = dict(binding_row.get("binding") or {})
    binding.pop("auth_secret_ref", None)
    http = binding.get("http")
    if isinstance(http, dict):
        http = dict(http)
        headers = http.get("headers")
        if isinstance(headers, dict):
            http["headers"] = {
                k: (
                    "[redacted]"
                    if "secret" in k.lower() or "authorization" in k.lower()
                    else v
                )
                for k, v in headers.items()
                if isinstance(v, str) and "${SECRET:" not in v
            }
        binding["http"] = http
    return {
        "connector_id": binding_row.get("connector_id"),
        "binding_version": binding_row.get("binding_version"),
        "is_active": binding_row.get("is_active", True),
        "binding": binding,
    }


def compute_input_hash(
    *,
    binding_version: int,
    connector_id: str,
    inputs: dict[str, Any],
    external_token: Optional[str] = None,
) -> str:
    payload = {
        "binding_version": binding_version,
        "connector_id": connector_id,
        "inputs": inputs,
        "external_token": external_token,
    }
    blob = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def _utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _needs_hydrate(
    *,
    mode: HydrateMode,
    sidecar: Optional[dict[str, Any]],
    expected_hash: str,
    ttl_seconds: int,
    now: datetime,
) -> bool:
    if mode == HydrateMode.NONE:
        return False
    if mode == HydrateMode.FORCE:
        return True
    if not sidecar or not isinstance(sidecar, dict):
        return True
    if sidecar.get("hash") != expected_hash:
        return True
    fetched_at = _parse_iso(sidecar.get("fetched_at"))
    if fetched_at is None:
        return True
    age = (now - fetched_at.astimezone(timezone.utc)).total_seconds()
    return age > ttl_seconds


def hydrate_logs(
    logs: list[dict[str, Any]],
    *,
    bindings: list[dict[str, Any]],
    mode: HydrateMode | str = HydrateMode.STALE_OK,
    hydrate_fields: Optional[list[str]] = None,
    materialize: bool = True,
    max_rows: int = DEFAULT_MAX_ROWS,
    raw_data_by_id: Optional[dict[int, dict[str, Any]]] = None,
    session=None,
    project_id: Optional[int] = None,
    context_id: Optional[int] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Hydrate external_entry fields on formatted log dicts.

    Parameters
    ----------
    logs:
        Output of ``_format_logs`` (id, entries, derived_entries, …).
    bindings:
        Active binding rows: field_name, connector_id, binding, binding_version.
    raw_data_by_id:
        Optional ``log_event_id -> full LogEvent.data`` for reading sidecars and
        input columns (preferred). Falls back to merging entries/external_entries.
    session / project_id / context_id:
        When set, ``auth_secret_ref`` resolves from the tenant Secrets vault
        owned by ``context_id`` (see ``external_bindings.auth``).

    Returns
    -------
    (logs, materialize_ops)
        ``materialize_ops`` is a list of ``{log_event_id, key, value}`` suitable
        for ``LogEventDAO.bulk_merge_data`` when ``materialize`` is True.
    """
    if isinstance(mode, str):
        mode = HydrateMode(mode)
    if mode == HydrateMode.NONE or not bindings or not logs:
        return logs, []

    field_filter = set(hydrate_fields) if hydrate_fields else None
    active = [
        b
        for b in bindings
        if b.get("is_active", True)
        and (field_filter is None or b["field_name"] in field_filter)
    ]
    if not active:
        return logs, []

    if len(logs) > max_rows:
        raise ValueError(
            f"Hydrate refused: {len(logs)} rows exceeds max_rows={max_rows}. "
            "Narrow the query or pass hydrate=none.",
        )

    now = datetime.now(timezone.utc)
    materialize_ops: list[dict[str, Any]] = []
    auth_cache: dict[tuple[Any, ...], Optional[str]] = {}

    # Ensure external_entries bucket exists
    for log in logs:
        log.setdefault("external_entries", {})
        log.setdefault("entries", {})
        log.setdefault("derived_entries", {})

    for binding_row in active:
        field_name = binding_row["field_name"]
        connector_id = binding_row["connector_id"]
        binding = dict(binding_row.get("binding") or {})
        binding_version = int(binding_row.get("binding_version") or 1)
        ttl = int(
            (binding.get("cache") or {}).get("ttl_seconds") or DEFAULT_TTL_SECONDS,
        )
        on_error = str(binding.get("on_error") or "fail")
        input_specs = binding.get("inputs") or []
        group_by = list((binding.get("batch") or {}).get("group_by") or [])

        misses: list[BindingItem] = []
        log_by_id = {int(log["id"]): log for log in logs}

        for log in logs:
            lid = int(log["id"])
            raw = (raw_data_by_id or {}).get(lid) or {}
            # Prefer raw JSONB for inputs/sidecars; fall back to formatted buckets.
            source = {
                **log.get("entries", {}),
                **log.get("external_entries", {}),
                **raw,
            }
            inputs = _extract_inputs(input_specs, source)
            sidecar = raw.get(sidecar_key(field_name))
            if not isinstance(sidecar, dict):
                sidecar = None
            expected = compute_input_hash(
                binding_version=binding_version,
                connector_id=connector_id,
                inputs=inputs,
                external_token=(sidecar or {}).get("token"),
            )
            if not _needs_hydrate(
                mode=mode,
                sidecar=sidecar,
                expected_hash=expected,
                ttl_seconds=ttl,
                now=now,
            ):
                # Serve cached value into external_entries if missing from format
                if field_name not in log["external_entries"] and field_name in source:
                    log["external_entries"][field_name] = source[field_name]
                # Drop from entries if it leaked
                log["entries"].pop(field_name, None)
                continue

            group_key = tuple(inputs.get(col) for col in group_by)
            misses.append(
                BindingItem(log_event_id=lid, inputs=inputs, group_key=group_key),
            )

        if not misses:
            continue

        connector = get_connector(connector_id)
        auth = resolve_auth(
            binding,
            session=session,
            project_id=project_id,
            context_id=context_id,
            cache=auth_cache,
            require=bool(binding.get("auth_secret_ref")),
        )

        # Group by batch group_key, respect max batch size
        max_batch = int((binding.get("batch") or {}).get("max") or 100)
        groups: dict[tuple[Any, ...], list[BindingItem]] = {}
        for item in misses:
            groups.setdefault(item.group_key, []).append(item)

        for _gk, group_items in groups.items():
            for start in range(0, len(group_items), max_batch):
                chunk = group_items[start : start + max_batch]
                results = connector.batch_fetch(binding=binding, items=chunk, auth=auth)
                by_id = {r.log_event_id: r for r in results}
                for item in chunk:
                    result = by_id.get(item.log_event_id)
                    log = log_by_id[item.log_event_id]
                    if result is None or not result.ok:
                        err = (result.error if result else "missing result") or "error"
                        if on_error == "fail":
                            raise RuntimeError(
                                f"External hydrate failed for field '{field_name}' "
                                f"log_event_id={item.log_event_id}: {err}",
                            )
                        if on_error == "null":
                            log["external_entries"][field_name] = None
                            if materialize:
                                materialize_ops.extend(
                                    _materialize_pair(
                                        item.log_event_id,
                                        field_name,
                                        None,
                                        compute_input_hash(
                                            binding_version=binding_version,
                                            connector_id=connector_id,
                                            inputs=item.inputs,
                                        ),
                                        error=err,
                                    ),
                                )
                        # stale: leave existing value alone
                        continue

                    log["external_entries"][field_name] = result.value
                    log["entries"].pop(field_name, None)
                    new_hash = compute_input_hash(
                        binding_version=binding_version,
                        connector_id=connector_id,
                        inputs=item.inputs,
                        external_token=result.external_token,
                    )
                    if materialize:
                        materialize_ops.extend(
                            _materialize_pair(
                                item.log_event_id,
                                field_name,
                                result.value,
                                new_hash,
                                token=result.external_token,
                            ),
                        )

    return logs, materialize_ops


def _extract_inputs(input_specs: list[Any], source: dict[str, Any]) -> dict[str, Any]:
    inputs: dict[str, Any] = {}
    for spec in input_specs:
        if isinstance(spec, str):
            inputs[spec] = source.get(spec)
        elif isinstance(spec, dict):
            name = spec.get("name") or spec.get("column")
            column = spec.get("column") or name
            if name:
                inputs[str(name)] = source.get(str(column))
    return inputs


def _materialize_pair(
    log_event_id: int,
    field_name: str,
    value: Any,
    digest: str,
    *,
    token: Optional[str] = None,
    error: Optional[str] = None,
) -> list[dict[str, Any]]:
    sidecar = {
        "hash": digest,
        "fetched_at": _utc_now_iso(),
        "token": token,
        "error": error,
    }
    return [
        {"log_event_id": log_event_id, "key": field_name, "value": value},
        {
            "log_event_id": log_event_id,
            "key": sidecar_key(field_name),
            "value": sidecar,
        },
    ]
