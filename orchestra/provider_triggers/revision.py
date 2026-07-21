"""Provider-event revision hashing."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from orchestra.provider_triggers.task_trigger import (
    ProviderEventTrigger,
    parse_task_trigger,
)


def normalize_trigger_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a deterministic trigger_config dict for hashing."""

    if not config:
        return {}
    return json.loads(json.dumps(dict(config), sort_keys=True))


def provider_event_revision_payload(
    *,
    trigger: ProviderEventTrigger | Mapping[str, Any],
    binding_id: str,
    execution_mode: str,
    entrypoint: int | None,
    provider_account_subject_hmac: str | None = None,
    requires_filesystem: bool = False,
    requires_computer: bool = False,
) -> dict[str, Any]:
    """Build the config-only payload hashed into provider-event revisions."""

    if isinstance(trigger, ProviderEventTrigger):
        trigger_payload = trigger
    else:
        parsed = parse_task_trigger(trigger)
        if not isinstance(parsed, ProviderEventTrigger):
            raise TypeError("provider_event trigger required")
        trigger_payload = parsed

    payload: dict[str, Any] = {
        "binding_id": binding_id,
        "connection_id": trigger_payload.connection_id,
        "backend_id": trigger_payload.backend_id,
        "canonical_app_slug": trigger_payload.canonical_app_slug,
        "provider_trigger_slug": trigger_payload.provider_trigger_slug,
        "trigger_config": normalize_trigger_config(trigger_payload.trigger_config),
        "execution_mode": execution_mode,
        "entrypoint": entrypoint,
        "requires_filesystem": bool(requires_filesystem),
        "requires_computer": bool(requires_computer),
    }
    if provider_account_subject_hmac:
        payload["provider_account_subject_hmac"] = provider_account_subject_hmac
    return payload


def compute_provider_event_revision(
    *,
    trigger: ProviderEventTrigger | Mapping[str, Any],
    binding_id: str,
    execution_mode: str,
    entrypoint: int | None,
    provider_account_subject_hmac: str | None = None,
    requires_filesystem: bool = False,
    requires_computer: bool = False,
) -> str:
    """Return the SHA-256 digest for one provider-event revision."""

    payload = provider_event_revision_payload(
        trigger=trigger,
        binding_id=binding_id,
        execution_mode=execution_mode,
        entrypoint=entrypoint,
        provider_account_subject_hmac=provider_account_subject_hmac,
        requires_filesystem=requires_filesystem,
        requires_computer=requires_computer,
    )
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
