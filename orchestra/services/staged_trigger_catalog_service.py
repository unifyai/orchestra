"""Connection-gated provider trigger catalog from staged imports."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from orchestra.db.dao.integration_provider_dao import IntegrationProviderDAO
from orchestra.db.dao.trigger_catalog_dao import TriggerCatalogDAO
from orchestra.provider_triggers.backend_ids import (
    NATIVE_GOOGLE_BACKEND_ID,
    NATIVE_MICROSOFT_BACKEND_ID,
)
from orchestra.provider_triggers.catalog_import.registry import (
    supported_trigger_catalog_backends,
)
from orchestra.provider_triggers.task_trigger import ProviderEventTrigger
from orchestra.provider_triggers.topology import evaluate_provider_trigger_topology
from orchestra.provider_triggers.workspace_connection_facade import (
    ensure_workspace_trigger_connections,
)
from orchestra.web.api.integrations.operations import OwnerContext

_ACTIVE_CONNECTION_STATUSES = frozenset({"connected", "active"})
_NATIVE_BACKENDS = frozenset({NATIVE_GOOGLE_BACKEND_ID, NATIVE_MICROSOFT_BACKEND_ID})


@dataclass(frozen=True)
class TriggerCapability:
    """Honest-bar capability matrix for one catalog slug.

    ``live_ready``/``delivery_only`` are ``None`` for backends that do not carry
    native capability metadata (Composio/Pipedream), so those backends are never
    gated by the fail-closed checks below.
    """

    config_schema: dict[str, Any]
    live_ready: bool | None
    delivery_only: bool | None
    target_resource_family: str | None

    @property
    def is_gated(self) -> bool:
        return self.live_ready is not None or self.delivery_only is not None

    @property
    def provisionable(self) -> bool:
        return bool(self.live_ready) and not bool(self.delivery_only)


def _catalog_environment() -> str:
    return os.getenv("PROVIDER_TRIGGER_CATALOG_ENVIRONMENT", "staging")


def _capability_from_candidate_json(
    candidate_json: dict[str, Any],
) -> TriggerCapability:
    config_schema = candidate_json.get("config_schema")
    return TriggerCapability(
        config_schema=config_schema if isinstance(config_schema, dict) else {},
        live_ready=candidate_json.get("live_ready"),
        delivery_only=candidate_json.get("delivery_only"),
        target_resource_family=candidate_json.get("target_resource_family"),
    )


def missing_required_config(
    config_schema: dict[str, Any] | None,
    trigger_config: dict[str, Any] | None,
) -> list[str]:
    """Return required config keys that are absent or empty in ``trigger_config``.

    Fail-closed presence check only — not full JSON-schema validation. A key is
    satisfied when present with a non-empty value.
    """

    if not isinstance(config_schema, dict):
        return []
    required = config_schema.get("required")
    if not isinstance(required, list):
        return []
    config = trigger_config if isinstance(trigger_config, dict) else {}
    missing: list[str] = []
    for key in required:
        value = config.get(key)
        if value is None or (isinstance(value, (str, list, dict)) and len(value) == 0):
            missing.append(str(key))
    return missing


def _trigger_config_schema(
    *,
    backend_id: str,
    candidate_json: dict[str, Any],
    raw_metadata: dict[str, Any],
) -> dict[str, Any]:
    if backend_id in _NATIVE_BACKENDS or backend_id == "composio":
        # Native honest-bar entries carry a top-level ``config_schema``; older
        # Composio candidates keep it under ``raw_metadata.config``.
        config = candidate_json.get("config_schema")
        if isinstance(config, dict) and config:
            return config
        config = raw_metadata.get("config")
        return config if isinstance(config, dict) else {}
    if backend_id == "pipedream":
        return {
            "configurable_props": raw_metadata.get("configurable_props") or [],
            "description": raw_metadata.get("description"),
            "name": raw_metadata.get("name"),
        }
    return {}


def resolve_trigger_capability(
    session: Session,
    *,
    backend_id: str,
    provider_trigger_slug: str,
    environment: str | None = None,
) -> TriggerCapability | None:
    """Resolve one slug's capability from the staged catalog (not gated by
    connections). Returns ``None`` when the slug is absent from the current
    snapshot so non-native backends stay ungated.
    """

    catalog_dao = TriggerCatalogDAO(session)
    resolved_env = environment or _catalog_environment()
    bootstrap = catalog_dao.get_bootstrap_state(
        environment=resolved_env,
        backend_id=backend_id,
    )
    if bootstrap is None or not bootstrap.desired_hash:
        return None
    snapshot = catalog_dao.get_snapshot_by_hash(
        environment=resolved_env,
        backend_id=backend_id,
        content_hash=bootstrap.desired_hash,
    )
    if snapshot is None:
        return None
    for candidate in catalog_dao.list_candidates_for_snapshot(snapshot.id):
        if candidate.provider_trigger_slug != provider_trigger_slug:
            continue
        return _capability_from_candidate_json(dict(candidate.candidate_json or {}))
    return None


def list_connected_app_slugs(
    session: Session,
    *,
    assistant_id: int,
    backend_id: str | None = None,
) -> set[tuple[str, str]]:
    """Return connected (backend_id, canonical_app_slug) pairs for one assistant."""

    dao = IntegrationProviderDAO(session)
    owner = OwnerContext(owner_scope="assistant", assistant_id=assistant_id)
    connections = dao.list_connections(owner)
    connected: set[tuple[str, str]] = set()
    for connection in connections:
        if connection.status not in {"connected", "active"}:
            continue
        if backend_id is not None and connection.backend_id != backend_id:
            continue
        if connection.canonical_app_slug:
            connected.add((connection.backend_id, connection.canonical_app_slug))
    return connected


def list_staged_triggers_for_assistant(
    session: Session,
    *,
    assistant_id: int,
    backend_id: str | None = None,
) -> dict[str, Any]:
    """Return staged provider triggers visible for one assistant's connections."""

    # heal facade rows on catalog read so OAuth callbacks that only
    # upsert assistant secrets still expose native triggers without a separate
    # workspace-connect completion hook in Orchestra.
    ensure_workspace_trigger_connections(session, assistant_id=assistant_id)

    topology = evaluate_provider_trigger_topology(session, require_worker=False)
    connected_apps = list_connected_app_slugs(
        session,
        assistant_id=assistant_id,
        backend_id=backend_id,
    )
    if not topology.available:
        return {
            "available": False,
            "unavailable_reason": topology.unavailable_reason,
            "triggers": [],
        }

    catalog_dao = TriggerCatalogDAO(session)
    environment = _catalog_environment()
    triggers: list[dict[str, Any]] = []
    backends = (
        (backend_id,)
        if backend_id is not None
        else supported_trigger_catalog_backends()
    )
    for resolved_backend in backends:
        bootstrap = catalog_dao.get_bootstrap_state(
            environment=environment,
            backend_id=resolved_backend,
        )
        if bootstrap is None or not bootstrap.desired_hash:
            continue
        snapshot = catalog_dao.get_snapshot_by_hash(
            environment=environment,
            backend_id=resolved_backend,
            content_hash=bootstrap.desired_hash,
        )
        if snapshot is None:
            continue
        for candidate in catalog_dao.list_candidates_for_snapshot(snapshot.id):
            app_hint = (candidate.canonical_app_hint or "").strip().casefold()
            if not app_hint:
                continue
            if (resolved_backend, app_hint) not in {
                (backend, slug.casefold()) for backend, slug in connected_apps
            }:
                continue
            candidate_json = dict(candidate.candidate_json or {})
            raw_metadata = dict(candidate_json.get("raw_metadata") or {})
            capability = _capability_from_candidate_json(candidate_json)
            triggers.append(
                {
                    "backend_id": resolved_backend,
                    "canonical_app_slug": app_hint,
                    "provider_trigger_slug": candidate.provider_trigger_slug,
                    "provider_version": candidate.provider_version,
                    "display_name": raw_metadata.get("name"),
                    "description": raw_metadata.get("description"),
                    "config_schema": _trigger_config_schema(
                        backend_id=resolved_backend,
                        candidate_json=candidate_json,
                        raw_metadata=raw_metadata,
                    ),
                    "live_ready": capability.live_ready,
                    "delivery_only": capability.delivery_only,
                    "provisionable": (
                        capability.provisionable if capability.is_gated else None
                    ),
                    "target_resource_family": capability.target_resource_family,
                },
            )

    triggers.sort(
        key=lambda item: (
            item["canonical_app_slug"],
            item["backend_id"],
            item["provider_trigger_slug"],
        ),
    )
    return {
        "available": True,
        "unavailable_reason": None,
        "triggers": triggers,
    }


def validate_provider_event_trigger_for_assistant(
    session: Session,
    *,
    assistant_id: int,
    trigger: ProviderEventTrigger,
) -> None:
    """Reject provider-event tasks that reference unknown connections or slugs."""

    owner = OwnerContext(owner_scope="assistant", assistant_id=assistant_id)
    connection = IntegrationProviderDAO(session).best_connection(
        owner=owner,
        canonical_app_slug=trigger.canonical_app_slug,
        backend_id=trigger.backend_id,
        connection_id=trigger.connection_id,
    )
    if connection is None:
        raise ValueError(
            f"provider_event_connection_not_found: {trigger.connection_id}",
        )
    if connection.status not in _ACTIVE_CONNECTION_STATUSES:
        raise ValueError(
            f"provider_event_connection_not_active: {trigger.connection_id}",
        )
    if connection.backend_id != trigger.backend_id:
        raise ValueError("provider_event_backend_mismatch")
    app_slug = (connection.canonical_app_slug or "").casefold()
    if app_slug != trigger.canonical_app_slug.casefold():
        raise ValueError("provider_event_app_mismatch")

    catalog = list_staged_triggers_for_assistant(
        session,
        assistant_id=assistant_id,
        backend_id=trigger.backend_id,
    )
    if not catalog.get("available"):
        raise ValueError("provider_event_catalog_unavailable")
    allowed_rows = {
        row["provider_trigger_slug"]: row
        for row in catalog.get("triggers") or []
        if str(row.get("canonical_app_slug", "")).casefold() == app_slug
    }
    matched_row = allowed_rows.get(trigger.provider_trigger_slug)
    if matched_row is None:
        raise ValueError(
            "provider_event_trigger_slug_not_in_catalog: "
            f"{trigger.provider_trigger_slug}",
        )

    _enforce_honest_capability_bar(trigger=trigger, catalog_row=matched_row)


def _enforce_honest_capability_bar(
    *,
    trigger: ProviderEventTrigger,
    catalog_row: dict[str, Any],
) -> None:
    """Fail closed at typed create/enable per the native honest capability bar.

    Discovery may surface every curated row, but a delivery-only or not-live-ready
    slug must never enable into a healthy binding, and an enable must carry the
    required target-resource config.
    """

    # Only gate an actual enable; drafts/paused rows may still be authored while
    # the twin gathers the resource config.
    if trigger.state != "enabled":
        return

    provisionable = catalog_row.get("provisionable")
    # ``None`` provisionable means the backend is not part of the native
    # capability gate (Composio/Pipedream) — leave those to their own conformance.
    if provisionable is False:
        if catalog_row.get("delivery_only"):
            raise ValueError(
                "provider_event_trigger_delivery_only: "
                f"{trigger.provider_trigger_slug}",
            )
        raise ValueError(
            "provider_event_trigger_not_live_ready: "
            f"{trigger.provider_trigger_slug}",
        )

    missing = missing_required_config(
        catalog_row.get("config_schema"),
        trigger.trigger_config,
    )
    if missing:
        raise ValueError(
            "provider_event_trigger_config_required: "
            f"{trigger.provider_trigger_slug}:{','.join(missing)}",
        )
