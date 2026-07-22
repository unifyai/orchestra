"""Local Composio trigger adapter used when COMPOSIO_API_KEY is unset.

Provides deterministic provision, signed delivery verification, and lifecycle
behavior for local stacks and integration tests without calling Composio APIs.
"""

from __future__ import annotations

import json
import os
import secrets
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from orchestra.provider_triggers.backend_ids import COMPOSIO_BACKEND_ID
from orchestra.provider_triggers.composio_trigger_adapter import (
    COMPOSIO_WEBHOOK_SECRET_REF,
    ComposioTriggerAdapter,
)
from orchestra.provider_triggers.provider_identity import provider_account_subject_hmac
from orchestra.provider_triggers.trigger_adapter import (
    NormalizedProviderDelivery,
    ProviderAccountIdentity,
    TriggerDeleteRequest,
    TriggerHealthResult,
    TriggerProviderAdapter,
    TriggerProvisionRequest,
    TriggerProvisionResult,
)


@dataclass
class _ProvisionRecord:
    request: TriggerProvisionRequest
    result: TriggerProvisionResult


@dataclass
class LocalComposioTriggerScenario:
    """Process-local knobs for local stub scenarios in tests and seeds."""

    subject: str = "assistant:provider-trigger-probe"
    display_label: str = "github:assistant:provider-trigger-probe"
    provider_connection_id: str = "ca_local_stub"
    connection_status: str = "connected"
    health_status: str = "ok"
    health_error_code: str | None = None
    provision_error: Exception | None = None
    lose_next_provision_response: bool = False
    use_per_generation_signing_secrets: bool = False
    alternate_subject: str | None = None
    revoked_resources: set[str] = field(default_factory=set)
    deleted_external_trigger_ids: set[str] = field(default_factory=set)
    provisions_by_idempotency_key: dict[str, _ProvisionRecord] = field(
        default_factory=dict,
    )
    generation_signing_secrets: dict[str, str] = field(default_factory=dict)
    delete_calls: list[TriggerDeleteRequest] = field(default_factory=list)

    def reset(self) -> None:
        """Clear all scenario state between tests."""

        self.lose_next_provision_response = False
        self.use_per_generation_signing_secrets = False
        self.alternate_subject = None
        self.revoked_resources.clear()
        self.deleted_external_trigger_ids.clear()
        self.provisions_by_idempotency_key.clear()
        self.generation_signing_secrets.clear()
        self.delete_calls.clear()
        self.health_status = "ok"
        self.health_error_code = None
        self.provision_error = None
        self.connection_status = "connected"


_SCENARIO = LocalComposioTriggerScenario()


def get_local_composio_trigger_scenario() -> LocalComposioTriggerScenario:
    """Return the mutable process-local scenario knobs for the local stub."""

    return _SCENARIO


def reset_local_composio_trigger_state() -> None:
    """Reset local stub state between tests."""

    _SCENARIO.reset()


def _stable_local_trigger_id(request: TriggerProvisionRequest) -> str:
    """Return a deterministic external id for one passthrough trigger config."""

    stable_payload = json.dumps(
        {
            "provider_trigger_slug": request.provider_trigger_slug,
            "trigger_config": dict(request.trigger_config),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return f"ti_local_{uuid.uuid5(uuid.NAMESPACE_OID, stable_payload).hex[:12]}"


class LocalComposioTriggerAdapter(TriggerProviderAdapter):
    """Deterministic Composio trigger adapter for credential-free local stacks."""

    backend_id = COMPOSIO_BACKEND_ID

    def __init__(
        self,
        *,
        webhook_secret: str | None = None,
        account_subject_pepper: str | None = None,
        timeout_seconds: int = 30,
    ) -> None:
        self.webhook_secret = (
            webhook_secret
            if webhook_secret is not None
            else os.getenv("COMPOSIO_WEBHOOK_SECRET")
        )
        self.account_subject_pepper = (
            account_subject_pepper
            if account_subject_pepper is not None
            else os.getenv("TRIGGER_ACCOUNT_SUBJECT_PEPPER")
            or os.getenv("TRIGGER_EVENT_WRAPPING_MASTER_KEY")
            or ""
        )
        self.timeout_seconds = timeout_seconds
        self._scenario = _SCENARIO
        self._delivery_adapter = ComposioTriggerAdapter(
            api_key="",
            webhook_secret=self.webhook_secret,
            account_subject_pepper=self.account_subject_pepper,
            timeout_seconds=timeout_seconds,
        )

    def _active_subject(self) -> str:
        return self._scenario.alternate_subject or self._scenario.subject

    def resolve_account_identity(
        self,
        *,
        provider_connection_id: str,
    ) -> ProviderAccountIdentity:
        if not provider_connection_id:
            raise ValueError("provider_connection_id is required")
        subject = self._active_subject()
        subject_hmac = None
        if self.account_subject_pepper:
            subject_hmac = provider_account_subject_hmac(
                subject,
                pepper=self.account_subject_pepper,
            )
        return ProviderAccountIdentity(
            subject=subject,
            display_label=self._scenario.display_label,
            subject_hmac=subject_hmac,
            connected_account_id=provider_connection_id,
            provider_user_id=subject,
            raw={"status": self._scenario.connection_status},
        )

    def provision(self, request: TriggerProvisionRequest) -> TriggerProvisionResult:
        if self._scenario.provision_error is not None:
            raise self._scenario.provision_error

        existing = self._scenario.provisions_by_idempotency_key.get(
            request.idempotency_key,
        )
        if existing is not None:
            return existing.result
        external_trigger_id = _stable_local_trigger_id(request)
        signing_secret_ref = COMPOSIO_WEBHOOK_SECRET_REF
        signing_secret_version = "project"
        if request.generation_id and self._scenario.use_per_generation_signing_secrets:
            generation_secret = secrets.token_urlsafe(24)
            signing_secret_ref = f"local-gen-{request.generation_id}"
            signing_secret_version = request.generation_id
            self._scenario.generation_signing_secrets[signing_secret_ref] = (
                generation_secret
            )

        result = TriggerProvisionResult(
            external_trigger_id=external_trigger_id,
            signing_secret_ref=signing_secret_ref,
            signing_secret_version=signing_secret_version,
            raw={
                "id": external_trigger_id,
                "status": "active",
                "provider_trigger_slug": request.provider_trigger_slug,
                "trigger_config": dict(request.trigger_config),
            },
        )
        record = _ProvisionRecord(request=request, result=result)
        self._scenario.provisions_by_idempotency_key[request.idempotency_key] = record

        if self._scenario.lose_next_provision_response:
            self._scenario.lose_next_provision_response = False
            raise RuntimeError("simulated lost Composio provision response")

        return result

    def delete(self, request: TriggerDeleteRequest) -> None:
        self._scenario.delete_calls.append(request)
        if not request.external_trigger_id:
            return
        if request.external_trigger_id in self._scenario.deleted_external_trigger_ids:
            return
        self._scenario.deleted_external_trigger_ids.add(request.external_trigger_id)

    def verify_delivery(
        self,
        *,
        headers: Mapping[str, str],
        raw_body: bytes,
        signing_secrets: Sequence[str],
        tolerance_seconds: int | None = None,
    ) -> bool:
        secrets_list = list(signing_secrets)
        if not secrets_list:
            secrets_list = list(self._scenario.generation_signing_secrets.values())
            if self.webhook_secret:
                secrets_list.append(self.webhook_secret)
        return self._delivery_adapter.verify_delivery(
            headers=headers,
            raw_body=raw_body,
            signing_secrets=secrets_list,
            tolerance_seconds=tolerance_seconds,
        )

    def delivery_external_trigger_id(
        self,
        *,
        headers: Mapping[str, str],
        raw_body: bytes,
    ) -> str | None:
        return self._delivery_adapter.delivery_external_trigger_id(
            headers=headers,
            raw_body=raw_body,
        )

    def normalize_delivery(
        self,
        *,
        headers: Mapping[str, str],
        raw_body: bytes | Mapping[str, Any],
    ) -> NormalizedProviderDelivery:
        return self._delivery_adapter.normalize_delivery(
            headers=headers,
            raw_body=raw_body,
        )

    def stable_event_identity(
        self,
        delivery: Mapping[str, Any] | NormalizedProviderDelivery,
    ) -> str | None:
        return self._delivery_adapter.stable_event_identity(delivery)

    def health(
        self,
        *,
        external_trigger_id: str | None,
        provider_connection_id: str | None,
        connection_id: str | None = None,
    ) -> TriggerHealthResult:
        _ = external_trigger_id, connection_id
        if not provider_connection_id:
            return TriggerHealthResult(
                status="error",
                error_code="provider_connection_missing",
            )
        if self._scenario.health_status == "ok":
            return TriggerHealthResult(
                status="ok",
                detail={
                    "connected_account_id": provider_connection_id,
                    "provider_status": self._scenario.connection_status.upper(),
                },
            )
        return TriggerHealthResult(
            status="error",
            error_code=self._scenario.health_error_code
            or "provider_health_check_failed",
            detail={"provider_status": self._scenario.connection_status},
        )
