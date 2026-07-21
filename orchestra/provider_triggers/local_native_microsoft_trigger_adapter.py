"""Local native Microsoft trigger adapter for credential-free stacks and tests."""

from __future__ import annotations

import json
import os
import secrets
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from orchestra.provider_triggers.backend_ids import (
    NATIVE_MICROSOFT_BACKEND_ID,
    NATIVE_MICROSOFT_TEAMS_TRANSCRIPT_SLUG,
)
from orchestra.provider_triggers.native_webhook import verify_native_delivery
from orchestra.provider_triggers.provider_identity import (
    native_event_identity,
    provider_account_subject_hmac,
)
from orchestra.provider_triggers.trigger_adapter import (
    NormalizedProviderDelivery,
    ProviderAccountIdentity,
    TriggerDeleteRequest,
    TriggerHealthResult,
    TriggerProviderAdapter,
    TriggerProvisionRequest,
    TriggerProvisionResult,
)

NATIVE_MICROSOFT_WEBHOOK_SECRET_REF = "env:NATIVE_MICROSOFT_WEBHOOK_SECRET"


@dataclass
class _ProvisionRecord:
    request: TriggerProvisionRequest
    result: TriggerProvisionResult


@dataclass
class LocalNativeMicrosoftTriggerScenario:
    account_email: str = "teams.user@example.com"
    connection_status: str = "connected"
    health_status: str = "ok"
    provisions_by_idempotency_key: dict[str, _ProvisionRecord] = field(
        default_factory=dict,
    )
    deleted_external_trigger_ids: set[str] = field(default_factory=set)
    delete_calls: list[TriggerDeleteRequest] = field(default_factory=list)

    def reset(self) -> None:
        self.provisions_by_idempotency_key.clear()
        self.deleted_external_trigger_ids.clear()
        self.delete_calls.clear()
        self.health_status = "ok"
        self.connection_status = "connected"


_SCENARIO = LocalNativeMicrosoftTriggerScenario()


def get_local_native_microsoft_trigger_scenario() -> (
    LocalNativeMicrosoftTriggerScenario
):
    return _SCENARIO


def reset_local_native_microsoft_trigger_state() -> None:
    _SCENARIO.reset()


def _stable_external_trigger_id(request: TriggerProvisionRequest) -> str:
    stable_payload = json.dumps(
        {
            "backend_id": NATIVE_MICROSOFT_BACKEND_ID,
            "provider_trigger_slug": request.provider_trigger_slug,
            "trigger_config": dict(request.trigger_config),
            "provider_connection_id": request.provider_connection_id,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return f"nm_{uuid.uuid5(uuid.NAMESPACE_OID, stable_payload).hex[:12]}"


def _parse_provider_connection_id(provider_connection_id: str) -> str:
    prefix = "microsoft:"
    if provider_connection_id.startswith(prefix):
        return provider_connection_id[len(prefix) :]
    return provider_connection_id


class LocalNativeMicrosoftTriggerAdapter(TriggerProviderAdapter):
    backend_id = NATIVE_MICROSOFT_BACKEND_ID

    def __init__(
        self,
        *,
        webhook_secret: str | None = None,
        account_subject_pepper: str | None = None,
    ) -> None:
        self.webhook_secret = (
            webhook_secret
            if webhook_secret is not None
            else os.getenv("NATIVE_MICROSOFT_WEBHOOK_SECRET")
        )
        self.account_subject_pepper = (
            account_subject_pepper
            if account_subject_pepper is not None
            else os.getenv("TRIGGER_ACCOUNT_SUBJECT_PEPPER")
            or os.getenv("TRIGGER_EVENT_WRAPPING_MASTER_KEY")
            or ""
        )
        self._scenario = _SCENARIO

    def resolve_account_identity(
        self,
        *,
        provider_connection_id: str,
    ) -> ProviderAccountIdentity:
        if not provider_connection_id:
            raise ValueError("provider_connection_id is required")
        email = _parse_provider_connection_id(provider_connection_id)
        subject_hmac = None
        if self.account_subject_pepper:
            subject_hmac = provider_account_subject_hmac(
                email,
                pepper=self.account_subject_pepper,
            )
        return ProviderAccountIdentity(
            subject=email,
            display_label=f"microsoft_teams:{email}",
            subject_hmac=subject_hmac,
            connected_account_id=provider_connection_id,
            provider_user_id=email,
            raw={"status": self._scenario.connection_status},
        )

    def provision(self, request: TriggerProvisionRequest) -> TriggerProvisionResult:
        existing = self._scenario.provisions_by_idempotency_key.get(
            request.idempotency_key,
        )
        if existing is not None:
            return existing.result

        external_trigger_id = _stable_external_trigger_id(request)
        result = TriggerProvisionResult(
            external_trigger_id=external_trigger_id,
            signing_secret_ref=NATIVE_MICROSOFT_WEBHOOK_SECRET_REF,
            signing_secret_version="project",
            raw={
                "id": external_trigger_id,
                "status": "active",
                "provider_trigger_slug": request.provider_trigger_slug,
            },
        )
        self._scenario.provisions_by_idempotency_key[request.idempotency_key] = (
            _ProvisionRecord(request=request, result=result)
        )
        return result

    def delete(self, request: TriggerDeleteRequest) -> None:
        self._scenario.delete_calls.append(request)
        if request.external_trigger_id:
            self._scenario.deleted_external_trigger_ids.add(request.external_trigger_id)

    def verify_delivery(
        self,
        *,
        headers: Mapping[str, str],
        raw_body: bytes,
        signing_secrets: Sequence[str],
        tolerance_seconds: int | None = None,
    ) -> bool:
        secrets_list = [secret for secret in signing_secrets if secret]
        if not secrets_list and self.webhook_secret:
            secrets_list = [self.webhook_secret]
        return verify_native_delivery(
            headers=headers,
            raw_body=raw_body,
            signing_secrets=secrets_list,
            tolerance_seconds=tolerance_seconds,
        )

    def normalize_delivery(
        self,
        *,
        headers: Mapping[str, str],
        raw_body: bytes | Mapping[str, Any],
    ) -> NormalizedProviderDelivery:
        payload = (
            dict(raw_body)
            if isinstance(raw_body, Mapping)
            else json.loads(raw_body.decode("utf-8"))
        )
        if not isinstance(payload, dict):
            raise ValueError("Native Microsoft delivery payload must be a JSON object")

        event_id = native_event_identity(payload) or secrets.token_hex(8)
        provider_trigger_slug = str(
            payload.get("provider_trigger_slug")
            or NATIVE_MICROSOFT_TEAMS_TRANSCRIPT_SLUG,
        )
        external_trigger_id = payload.get("external_trigger_id")
        if isinstance(external_trigger_id, str):
            external_trigger_id = external_trigger_id.strip() or None
        else:
            external_trigger_id = None

        connected_account_id = payload.get("connected_account_id")
        provider_user_id = payload.get("provider_user_id")
        return NormalizedProviderDelivery(
            provider_event_identity=event_id,
            provider_trigger_slug=provider_trigger_slug,
            external_trigger_id=external_trigger_id,
            connected_account_id=(
                connected_account_id if isinstance(connected_account_id, str) else None
            ),
            provider_user_id=(
                provider_user_id if isinstance(provider_user_id, str) else None
            ),
            envelope={"headers": dict(headers), "provider": self.backend_id},
            source_body=payload,
            occurred_at=(
                str(payload.get("occurred_at"))
                if payload.get("occurred_at") is not None
                else None
            ),
        )

    def stable_event_identity(
        self,
        delivery: Mapping[str, Any] | NormalizedProviderDelivery,
    ) -> str | None:
        if isinstance(delivery, NormalizedProviderDelivery):
            return delivery.provider_event_identity
        return native_event_identity(delivery)

    def health(
        self,
        *,
        external_trigger_id: str | None,
        provider_connection_id: str | None,
    ) -> TriggerHealthResult:
        _ = external_trigger_id, provider_connection_id
        return TriggerHealthResult(status=self._scenario.health_status)
