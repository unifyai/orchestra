"""Build Microsoft Graph subscription create bodies from trigger config.

Pure helpers used by ``NativeMicrosoftTriggerAdapter.provision``. Resource and
``changeType`` come from the curated native Microsoft manifest. Only the
delegated / user-login family is provisionable; app-only shapes fail closed.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

_MANIFEST_PATH = (
    Path(__file__).resolve().parent
    / "catalog_import"
    / "manifests"
    / "native_microsoft_graph_subscriptions.json"
)

# Graph ``clientState`` max length. Prefer the raw webhook secret when it fits;
# otherwise derive a stable 64-char digest both Orchestra and Adapters share.
_GRAPH_CLIENT_STATE_MAX_LEN = 128

# Conservative TTL under OneDrive/driveItem ceilings (~3 days) and Outlook's
# longer mail/calendar window. Health renews before expiry.
_DEFAULT_SUBSCRIPTION_TTL = timedelta(days=2)

_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z0-9_]+)\}")

# Manifest templates use Graph camelCase placeholders; authored trigger_config
# uses snake_case twin fields.
_PLACEHOLDER_TO_CONFIG_KEY = {
    "onlineMeetingId": "online_meeting_id",
    "todoTaskListId": "todo_task_list_id",
}


class NativeMicrosoftTargetFamily(StrEnum):
    """Target-resource families from the native Microsoft capability matrix."""

    delegated = "microsoft_graph_delegated"
    app_only = "microsoft_graph_app_only"


@dataclass(frozen=True)
class NativeMicrosoftManifestEntry:
    """One curated Microsoft Graph subscription catalog entry."""

    provider_trigger_slug: str
    graph_resource: str
    change_type: str
    target_resource_family: NativeMicrosoftTargetFamily
    live_ready: bool
    config_schema: Mapping[str, Any]


@lru_cache(maxsize=1)
def _load_manifest_entries() -> dict[str, NativeMicrosoftManifestEntry]:
    payload = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    entries: dict[str, NativeMicrosoftManifestEntry] = {}
    for raw in payload.get("entries") or []:
        if not isinstance(raw, Mapping):
            continue
        slug = str(raw.get("provider_trigger_slug") or "").strip()
        metadata = raw.get("raw_metadata")
        if not slug or not isinstance(metadata, Mapping):
            continue
        resource = str(metadata.get("graph_resource") or "").strip()
        change_type = str(metadata.get("change_type") or "").strip()
        family_raw = str(raw.get("target_resource_family") or "").strip()
        try:
            family = NativeMicrosoftTargetFamily(family_raw)
        except ValueError:
            continue
        if not resource or not change_type:
            continue
        config_schema = raw.get("config_schema")
        entries[slug] = NativeMicrosoftManifestEntry(
            provider_trigger_slug=slug,
            graph_resource=resource,
            change_type=change_type,
            target_resource_family=family,
            live_ready=bool(raw.get("live_ready")),
            config_schema=config_schema if isinstance(config_schema, Mapping) else {},
        )
    return entries


def lookup_microsoft_manifest_entry(
    provider_trigger_slug: str,
) -> NativeMicrosoftManifestEntry:
    """Return the curated manifest entry for one slug or raise ``ValueError``."""

    slug = provider_trigger_slug.strip()
    entry = _load_manifest_entries().get(slug)
    if entry is None:
        raise ValueError(f"unsupported native Microsoft trigger slug: {slug}")
    return entry


def graph_client_state(webhook_secret: str) -> str:
    """Return the Graph ``clientState`` value derived from the shared secret."""

    secret = webhook_secret.strip()
    if not secret:
        raise ValueError("native Microsoft webhook secret is required for clientState")
    if len(secret) <= _GRAPH_CLIENT_STATE_MAX_LEN:
        return secret
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _substitute_resource_template(
    template: str,
    trigger_config: Mapping[str, Any] | None,
) -> str:
    config = dict(trigger_config or {})
    placeholders = _PLACEHOLDER_RE.findall(template)
    if not placeholders:
        return template

    resolved = template
    for placeholder in placeholders:
        config_key = _PLACEHOLDER_TO_CONFIG_KEY.get(placeholder, placeholder)
        value = config.get(config_key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"native Microsoft trigger_config.{config_key} is required "
                f"for resource template {{{placeholder}}}",
            )
        resolved = resolved.replace("{" + placeholder + "}", value.strip())
    return resolved


def build_graph_subscription_body(
    *,
    provider_trigger_slug: str,
    trigger_config: Mapping[str, Any] | None,
    notification_url: str,
    client_state: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return the JSON body for Graph ``POST /subscriptions``.

    Raises ``ValueError`` for app-only / not-live-ready slugs or missing config
    so the adapter fails closed before calling Graph.
    """

    entry = lookup_microsoft_manifest_entry(provider_trigger_slug)
    if entry.target_resource_family is NativeMicrosoftTargetFamily.app_only:
        raise ValueError(
            "native Microsoft app-only Graph shapes are not provisionable with "
            f"delegated workspace OAuth: {entry.provider_trigger_slug}",
        )
    if not entry.live_ready:
        raise ValueError(
            "native Microsoft trigger is not live_ready: "
            f"{entry.provider_trigger_slug}",
        )

    notification = notification_url.strip()
    if not notification:
        raise ValueError("Microsoft Graph notificationUrl is required")
    state = client_state.strip()
    if not state:
        raise ValueError("Microsoft Graph clientState is required")
    if len(state) > _GRAPH_CLIENT_STATE_MAX_LEN:
        raise ValueError("Microsoft Graph clientState exceeds 128 characters")

    resource = _substitute_resource_template(entry.graph_resource, trigger_config)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    expiration = current.astimezone(timezone.utc) + _DEFAULT_SUBSCRIPTION_TTL

    return {
        "changeType": entry.change_type,
        "notificationUrl": notification,
        "lifecycleNotificationUrl": notification,
        "resource": resource,
        "expirationDateTime": expiration.strftime("%Y-%m-%dT%H:%M:%S.0000000Z"),
        "clientState": state,
        "latestSupportedTlsVersion": "v1_2",
    }


def renew_expiration_datetime(*, now: datetime | None = None) -> str:
    """Return a Graph ``expirationDateTime`` for subscription renewal."""

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    expiration = current.astimezone(timezone.utc) + _DEFAULT_SUBSCRIPTION_TTL
    return expiration.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
