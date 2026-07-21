"""Build Google Workspace Events subscription create bodies from trigger config.

Pure helpers used by ``NativeGoogleTriggerAdapter.provision``. Family rules
follow the native Google capability matrix: Meet is user-level Cloud Identity,
Drive and Chat require an authored ``target_resource``, and Chat batch slugs
are delivery-only.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Mapping

CLOUD_IDENTITY_USER_RESOURCE_PREFIX = "//cloudidentity.googleapis.com/users/"
DRIVE_RESOURCE_PREFIX = "//drive.googleapis.com/"
CHAT_RESOURCE_PREFIX = "//chat.googleapis.com/"


class NativeGoogleTargetFamily(StrEnum):
    """Target-resource families from the native Google capability matrix."""

    meet_user = "google_meet_user"
    drive_resource = "google_drive_resource"
    chat_space = "google_chat_space"
    chat_batch_delivery = "google_chat_batch_delivery"


def infer_native_google_target_family(slug: str) -> NativeGoogleTargetFamily:
    """Infer the capability-matrix family for one Google Workspace Events slug."""

    normalized = slug.strip()
    if not normalized.startswith("google.workspace."):
        raise ValueError(f"unsupported native Google trigger slug: {slug}")
    parts = normalized.split(".")
    if "batch" in parts[-1]:
        return NativeGoogleTargetFamily.chat_batch_delivery
    if normalized.startswith("google.workspace.meet."):
        return NativeGoogleTargetFamily.meet_user
    if normalized.startswith("google.workspace.drive."):
        return NativeGoogleTargetFamily.drive_resource
    if normalized.startswith("google.workspace.chat."):
        return NativeGoogleTargetFamily.chat_space
    raise ValueError(f"unsupported native Google trigger slug: {slug}")


def normalize_drive_target_resource(raw: str) -> str:
    """Normalize authored Drive config into a Workspace Events targetResource."""

    value = raw.strip()
    if not value:
        raise ValueError("Drive target_resource is required")
    if value.startswith(DRIVE_RESOURCE_PREFIX):
        rest = value[len(DRIVE_RESOURCE_PREFIX) :]
        if rest.startswith("files/") or rest.startswith("drives/"):
            return value
        raise ValueError(
            "Drive target_resource must be a files/ or drives/ resource under "
            f"{DRIVE_RESOURCE_PREFIX}",
        )
    if value.startswith("files/") or value.startswith("drives/"):
        return f"{DRIVE_RESOURCE_PREFIX}{value}"
    # Bare Drive file/folder ids share the files/ resource name.
    return f"{DRIVE_RESOURCE_PREFIX}files/{value}"


def normalize_chat_target_resource(raw: str) -> str:
    """Normalize authored Chat config into a Workspace Events targetResource."""

    value = raw.strip()
    if not value:
        raise ValueError("Chat target_resource is required")
    if value.startswith(CHAT_RESOURCE_PREFIX):
        rest = value[len(CHAT_RESOURCE_PREFIX) :]
        if rest.startswith("spaces/"):
            return value
        raise ValueError(
            "Chat target_resource must be a spaces/ resource under "
            f"{CHAT_RESOURCE_PREFIX}",
        )
    if value.startswith("spaces/"):
        return f"{CHAT_RESOURCE_PREFIX}{value}"
    raise ValueError(
        "Chat target_resource must be spaces/<id> or spaces/- "
        "(or the full //chat.googleapis.com/... form)",
    )


def build_workspace_events_subscription_body(
    *,
    provider_trigger_slug: str,
    trigger_config: Mapping[str, Any] | None,
    cloud_identity_user_id: str | None,
    pubsub_topic: str,
) -> dict[str, Any]:
    """Return the JSON body for ``subscriptions.create``.

    Raises ``ValueError`` for delivery-only slugs or missing required config so
    the adapter can fail closed before calling Google.
    """

    slug = provider_trigger_slug.strip()
    if not slug:
        raise ValueError("provider_trigger_slug is required")
    topic = pubsub_topic.strip()
    if not topic:
        raise ValueError("native Google events pubsub topic is not configured")

    family = infer_native_google_target_family(slug)
    if family is NativeGoogleTargetFamily.chat_batch_delivery:
        raise ValueError(
            "native Google Chat batch event types are delivery-only and cannot "
            f"be provisioned as standalone subscriptions: {slug}",
        )

    config = dict(trigger_config or {})
    if family is NativeGoogleTargetFamily.meet_user:
        user_id = (cloud_identity_user_id or "").strip()
        if not user_id:
            raise ValueError("Cloud Identity user id is required for Meet targets")
        target_resource = f"{CLOUD_IDENTITY_USER_RESOURCE_PREFIX}{user_id}"
        drive_options = None
    elif family is NativeGoogleTargetFamily.drive_resource:
        raw_target = config.get("target_resource")
        if not isinstance(raw_target, str) or not raw_target.strip():
            raise ValueError("Drive trigger_config.target_resource is required")
        target_resource = normalize_drive_target_resource(raw_target)
        include_descendants = config.get("include_descendants")
        drive_options = (
            {"includeDescendants": bool(include_descendants)}
            if include_descendants is not None
            else None
        )
    else:
        raw_target = config.get("target_resource")
        if not isinstance(raw_target, str) or not raw_target.strip():
            raise ValueError("Chat trigger_config.target_resource is required")
        target_resource = normalize_chat_target_resource(raw_target)
        drive_options = None

    body: dict[str, Any] = {
        "targetResource": target_resource,
        "eventTypes": [slug],
        "notificationEndpoint": {"pubsubTopic": topic},
        "payloadOptions": {"includeResource": False},
    }
    if drive_options is not None:
        body["driveOptions"] = drive_options
    return body
