"""Composio inbound trigger adapter for provider-native subscriptions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import quote

import requests

from orchestra.provider_triggers.backend_ids import (
    COMPOSIO_BACKEND_ID,
    DEFAULT_SIGNATURE_TOLERANCE_SECONDS,
)
from orchestra.provider_triggers.provider_identity import (
    composio_v3_event_identity,
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

logger = logging.getLogger(__name__)

COMPOSIO_WEBHOOK_SECRET_REF = "env:COMPOSIO_WEBHOOK_SECRET"
DEFAULT_COMPOSIO_BASE_URL = "https://backend.composio.dev/api/v3.1"


class _HttpResponse(Protocol):
    status_code: int
    text: str

    def json(self) -> Any: ...

    def raise_for_status(self) -> None: ...


HttpRequestFn = Callable[..., _HttpResponse]


def verify_composio_signature(
    *,
    signing_secret: str,
    raw_body: bytes | str,
    webhook_id: str,
    webhook_timestamp: str,
    signature_header: str,
    tolerance_seconds: int = DEFAULT_SIGNATURE_TOLERANCE_SECONDS,
    now_seconds: int | None = None,
    signed_payload: str | None = None,
) -> bool:
    """Validate Composio V3 Standard-Webhooks-style signatures."""

    if not signing_secret or not webhook_id or not webhook_timestamp:
        return False
    if not signature_header:
        return False
    try:
        timestamp = int(webhook_timestamp)
    except ValueError:
        return False
    current = int(time.time() if now_seconds is None else now_seconds)
    if tolerance_seconds > 0 and abs(current - timestamp) > tolerance_seconds:
        return False

    if signed_payload is None:
        body = raw_body.decode("utf-8") if isinstance(raw_body, bytes) else raw_body
        signed_payload = f"{webhook_id}.{webhook_timestamp}.{body}"
    expected = base64.b64encode(
        hmac.new(
            signing_secret.encode("utf-8"),
            signed_payload.encode("utf-8"),
            hashlib.sha256,
        ).digest(),
    ).decode("utf-8")

    provided: list[str] = []
    for part in signature_header.split(" "):
        part = part.strip()
        if part.startswith("v1,"):
            provided.append(part[3:])
        elif "," in part:
            _, _, value = part.partition(",")
            if value:
                provided.append(value)
    if not provided:
        return False
    return any(hmac.compare_digest(candidate, expected) for candidate in provided)


def sign_composio_webhook_headers(
    *,
    signing_secret: str,
    raw_body: bytes,
    webhook_id: str,
    timestamp: str | None = None,
) -> dict[str, str]:
    """Build Composio V3 delivery headers for one body."""

    webhook_timestamp = timestamp or str(int(time.time()))
    body_text = raw_body.decode("utf-8")
    digest = base64.b64encode(
        hmac.new(
            signing_secret.encode("utf-8"),
            f"{webhook_id}.{webhook_timestamp}.{body_text}".encode("utf-8"),
            hashlib.sha256,
        ).digest(),
    ).decode("utf-8")
    return {
        "webhook-id": webhook_id,
        "webhook-timestamp": webhook_timestamp,
        "webhook-signature": f"v1,{digest}",
    }


def _header_value(headers: Mapping[str, str], name: str) -> str:
    direct = headers.get(name)
    if isinstance(direct, str) and direct:
        return direct
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered and isinstance(value, str):
            return value
    return ""


def _extract_trigger_id(payload: Mapping[str, Any]) -> str | None:
    for key in ("id", "trigger_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    data = payload.get("data")
    if isinstance(data, Mapping):
        for key in ("id", "trigger_id"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _parse_delivery_payload(
    raw_body: bytes | Mapping[str, Any],
) -> dict[str, Any]:
    payload = (
        dict(raw_body)
        if isinstance(raw_body, Mapping)
        else json.loads(raw_body.decode("utf-8"))
    )
    if not isinstance(payload, dict):
        raise ValueError("Composio delivery payload must be a JSON object")
    return payload


def _delivery_trigger_id(payload: Mapping[str, Any]) -> str | None:
    metadata = payload.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    trigger_id = metadata.get("trigger_id")
    return (
        trigger_id.strip()
        if isinstance(trigger_id, str) and trigger_id.strip()
        else None
    )


class ComposioTriggerAdapter(TriggerProviderAdapter):
    """Composio transport for provider-native trigger subscriptions."""

    backend_id = COMPOSIO_BACKEND_ID

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        webhook_secret: str | None = None,
        account_subject_pepper: str | None = None,
        timeout_seconds: int = 30,
        request_fn: HttpRequestFn | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.getenv("COMPOSIO_API_KEY")
        self.base_url = (
            base_url or os.getenv("COMPOSIO_BASE_URL") or DEFAULT_COMPOSIO_BASE_URL
        ).rstrip("/")
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
        self._request = request_fn or self._default_request

    def _api_headers(self) -> dict[str, str]:
        if not self.api_key:
            raise ValueError("COMPOSIO_API_KEY is required for Composio trigger calls.")
        return {"x-api-key": self.api_key, "Content-Type": "application/json"}

    def _default_request(self, method: str, url: str, **kwargs: Any) -> _HttpResponse:
        return requests.request(method, url, timeout=self.timeout_seconds, **kwargs)

    def resolve_account_identity(
        self,
        *,
        provider_connection_id: str,
    ) -> ProviderAccountIdentity:
        if not provider_connection_id:
            raise ValueError("provider_connection_id is required")
        response = self._request(
            "GET",
            f"{self.base_url}/connected_accounts/{provider_connection_id}",
            headers=self._api_headers(),
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Composio connected account payload was not an object")
        provider_user_id = data.get("user_id") or data.get("userId")
        if not isinstance(provider_user_id, str) or not provider_user_id.strip():
            raise ValueError("Composio connected account is missing user_id")
        subject = provider_user_id.strip()
        display = (
            str(data.get("status") or "").strip()
            or str(data.get("toolkit_slug") or data.get("appName") or "").strip()
        )
        label = f"{display}:{subject}" if display else subject
        subject_hmac = None
        if self.account_subject_pepper:
            subject_hmac = provider_account_subject_hmac(
                subject,
                pepper=self.account_subject_pepper,
            )
        return ProviderAccountIdentity(
            subject=subject,
            display_label=label,
            subject_hmac=subject_hmac,
            connected_account_id=provider_connection_id,
            provider_user_id=subject,
            raw=data,
        )

    def provision(self, request: TriggerProvisionRequest) -> TriggerProvisionResult:
        _ = (
            request.callback_url,
            request.ingress_key,
            request.canonical_app_slug,
            request.idempotency_key,
        )
        slug = quote(str(request.provider_trigger_slug).strip(), safe="")
        if not slug:
            raise ValueError("provider_trigger_slug is required")
        try:
            response = self._request(
                "POST",
                f"{self.base_url}/trigger_instances/{slug}/upsert",
                headers=self._api_headers(),
                json={
                    "connected_account_id": request.provider_connection_id,
                    "user_id": request.provider_user_id,
                    "trigger_config": dict(request.trigger_config),
                },
            )
            response.raise_for_status()
        except Exception as exc:
            raise RuntimeError(
                f"Composio trigger provision failed: {exc}",
            ) from exc
        body = response.json()
        if not isinstance(body, Mapping):
            raise RuntimeError("Composio trigger provision response was not an object")
        trigger_id = _extract_trigger_id(body)
        if not trigger_id:
            raise RuntimeError("Composio trigger provision response missing trigger id")
        return TriggerProvisionResult(
            external_trigger_id=trigger_id,
            signing_secret_ref=COMPOSIO_WEBHOOK_SECRET_REF,
            signing_secret_version="project",
            raw=dict(body),
        )

    def delete(self, request: TriggerDeleteRequest) -> None:
        if not request.external_trigger_id:
            return
        _ = request.idempotency_key
        trigger_id = quote(str(request.external_trigger_id).strip(), safe="")
        if not trigger_id:
            return
        try:
            response = self._request(
                "DELETE",
                f"{self.base_url}/trigger_instances/manage/{trigger_id}",
                headers=self._api_headers(),
            )
        except Exception as exc:
            raise RuntimeError(
                f"Composio trigger delete failed: {exc}",
            ) from exc
        if response.status_code in {404, 410}:
            return
        try:
            response.raise_for_status()
        except Exception as exc:
            raise RuntimeError(
                f"Composio trigger delete failed: {exc}",
            ) from exc

    def verify_delivery(
        self,
        *,
        headers: Mapping[str, str],
        raw_body: bytes,
        signing_secrets: Sequence[str],
        tolerance_seconds: int | None = None,
    ) -> bool:
        webhook_id = _header_value(headers, "webhook-id")
        webhook_timestamp = _header_value(headers, "webhook-timestamp")
        signature_header = _header_value(headers, "webhook-signature")
        if not webhook_id or not webhook_timestamp or not signature_header:
            return False
        secrets = [secret for secret in signing_secrets if secret]
        if not secrets and self.webhook_secret:
            secrets = [self.webhook_secret]
        if not secrets:
            return False
        tolerance = (
            DEFAULT_SIGNATURE_TOLERANCE_SECONDS
            if tolerance_seconds is None
            else tolerance_seconds
        )
        signed_payload = f"{webhook_id}.{webhook_timestamp}.{raw_body.decode('utf-8')}"
        return any(
            verify_composio_signature(
                signing_secret=secret,
                raw_body=raw_body,
                webhook_id=webhook_id,
                webhook_timestamp=webhook_timestamp,
                signature_header=signature_header,
                tolerance_seconds=tolerance,
                signed_payload=signed_payload,
            )
            for secret in secrets
        )

    def delivery_external_trigger_id(
        self,
        *,
        headers: Mapping[str, str],
        raw_body: bytes,
    ) -> str | None:
        _ = headers
        return _delivery_trigger_id(_parse_delivery_payload(raw_body))

    def normalize_delivery(
        self,
        *,
        headers: Mapping[str, str],
        raw_body: bytes | Mapping[str, Any],
    ) -> NormalizedProviderDelivery:
        payload = _parse_delivery_payload(raw_body)
        identity = self.stable_event_identity(payload)
        if not identity:
            raise ValueError("Composio delivery is missing retry-stable identity")
        metadata = payload.get("metadata")
        if not isinstance(metadata, Mapping):
            metadata = {}
        trigger_slug = metadata.get("trigger_slug")
        if not isinstance(trigger_slug, str) or not trigger_slug.strip():
            raise ValueError("Composio delivery is missing trigger_slug")
        trigger_slug = trigger_slug.strip()
        connected_account_id = metadata.get("connected_account_id")
        provider_user_id = metadata.get("user_id")
        external_trigger_id = _delivery_trigger_id(payload)
        occurred_at = payload.get("timestamp")
        envelope = {
            "backend_id": self.backend_id,
            "provider_trigger_slug": trigger_slug,
            "provider_event_identity": identity,
            "external_trigger_id": external_trigger_id,
            "connected_account_id": connected_account_id,
            "provider_user_id": provider_user_id,
            "occurred_at": occurred_at,
            "webhook_id": _header_value(headers, "webhook-id") or None,
        }
        return NormalizedProviderDelivery(
            provider_event_identity=identity,
            provider_trigger_slug=trigger_slug,
            external_trigger_id=external_trigger_id,
            connected_account_id=(
                str(connected_account_id) if connected_account_id else None
            ),
            provider_user_id=str(provider_user_id) if provider_user_id else None,
            envelope=envelope,
            occurred_at=str(occurred_at) if occurred_at else None,
            source_body=payload,
        )

    def stable_event_identity(
        self,
        delivery: Mapping[str, Any] | NormalizedProviderDelivery,
    ) -> str | None:
        if isinstance(delivery, NormalizedProviderDelivery):
            return delivery.provider_event_identity
        return composio_v3_event_identity(delivery)

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
        try:
            response = self._request(
                "GET",
                f"{self.base_url}/connected_accounts/{provider_connection_id}",
                headers=self._api_headers(),
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.exception("Composio trigger health check failed")
            return TriggerHealthResult(
                status="error",
                error_code="provider_health_check_failed",
                detail={"message": str(exc)},
            )
        if not isinstance(data, dict):
            return TriggerHealthResult(
                status="error",
                error_code="provider_health_check_failed",
            )
        provider_status = str(data.get("status") or "").upper()
        if provider_status in {"ACTIVE", "CONNECTED"}:
            return TriggerHealthResult(
                status="ok",
                detail={
                    "connected_account_id": provider_connection_id,
                    "external_trigger_id": external_trigger_id,
                    "provider_status": provider_status,
                },
            )
        return TriggerHealthResult(
            status="error",
            error_code="provider_connection_not_active",
            detail={"provider_status": provider_status or "unknown"},
        )


def list_composio_trigger_types(
    *,
    api_key: str,
    base_url: str | None = None,
    request_fn: HttpRequestFn | None = None,
    timeout_seconds: int = 30,
    page_limit: int = 1000,
) -> list[dict[str, Any]]:
    """Fetch all Composio trigger types via the public REST catalog API."""

    if not api_key.strip():
        raise ValueError("COMPOSIO_API_KEY is required for Composio catalog import")
    resolved_base = (
        base_url or os.getenv("COMPOSIO_BASE_URL") or DEFAULT_COMPOSIO_BASE_URL
    ).rstrip("/")
    http = request_fn or (
        lambda method, url, **kwargs: requests.request(
            method,
            url,
            timeout=timeout_seconds,
            **kwargs,
        )
    )
    headers = {"x-api-key": api_key, "Content-Type": "application/json"}
    entries: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": page_limit}
        if cursor:
            params["cursor"] = cursor
        response = http(
            "GET",
            f"{resolved_base}/triggers_types",
            headers=headers,
            params=params,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise RuntimeError("Composio trigger catalog response was not an object")
        items = payload.get("items") or []
        if not isinstance(items, list):
            raise RuntimeError("Composio trigger catalog items were not a list")
        for item in items:
            if isinstance(item, Mapping):
                entries.append(dict(item))
        next_cursor = payload.get("next_cursor")
        if not isinstance(next_cursor, str) or not next_cursor.strip():
            break
        cursor = next_cursor.strip()
    return entries
