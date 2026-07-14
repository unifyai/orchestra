"""Resolve stored signing-secret references for provider-trigger ingress."""

from __future__ import annotations

import os
from datetime import datetime, timezone

from orchestra.db.models.provider_trigger_models import (
    EventTriggerSubscriptionGeneration,
)


def resolve_signing_secret_ref(secret_ref: str | None) -> str | None:
    """Resolve one stored signing-secret reference to raw secret material.

    Supported reference shapes:
    - ``env:VAR_NAME`` — read from the process environment
    - raw secret strings used by local tests
    """

    if not secret_ref:
        return None
    ref = secret_ref.strip()
    if not ref:
        return None
    if ref.startswith("env:"):
        env_name = ref[4:].strip()
        if not env_name:
            return None
        value = os.getenv(env_name)
        return value.strip() if isinstance(value, str) and value.strip() else None
    return ref


def accepted_signing_secrets_for_generation(
    generation: EventTriggerSubscriptionGeneration,
    *,
    now: datetime | None = None,
) -> list[str]:
    """Return current and still-overlapping previous signing secrets."""

    secrets: list[str] = []
    current = resolve_signing_secret_ref(generation.signing_secret_ref)
    if current:
        secrets.append(current)

    previous = resolve_signing_secret_ref(generation.previous_signing_secret_ref)
    if not previous:
        return secrets

    overlap_expires = generation.signing_overlap_expires_at
    if overlap_expires is None:
        secrets.append(previous)
        return secrets

    current_time = now or datetime.now(timezone.utc)
    if overlap_expires.tzinfo is None:
        overlap_expires = overlap_expires.replace(tzinfo=timezone.utc)
    if current_time <= overlap_expires:
        secrets.append(previous)
    return secrets
