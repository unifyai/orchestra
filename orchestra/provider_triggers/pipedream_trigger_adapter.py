"""Pipedream inbound trigger adapter for provider-native subscriptions."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable, Mapping, Protocol, Sequence

import requests

from orchestra.provider_triggers.backend_ids import (
    DEFAULT_SIGNATURE_TOLERANCE_SECONDS,
    PIPEDREAM_BACKEND_ID,
)
from orchestra.provider_triggers.pipedream_signing import verify_pipedream_signature
from orchestra.provider_triggers.provider_identity import (
    pipedream_delivery_identity,
    provider_account_subject_hmac,
)
from orchestra.provider_triggers.signing_secret_storage import (
    wrap_signing_secret_for_generation,
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

DEFAULT_PIPEDREAM_CONNECT_BASE_URL = "https://api.pipedream.com/v1/connect"
DEFAULT_PIPEDREAM_OAUTH_TOKEN_URL = "https://api.pipedream.com/v1/oauth/token"


class _HttpResponse(Protocol):
    status_code: int
    text: str

    def json(self) -> Any: ...

    def raise_for_status(self) -> None: ...


HttpRequestFn = Callable[..., _HttpResponse]


def _header_value(headers: Mapping[str, str], name: str) -> str:
    direct = headers.get(name)
    if isinstance(direct, str) and direct:
        return direct
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered and isinstance(value, str):
            return value
    return ""


def _parse_delivery_payload(
    raw_body: bytes | Mapping[str, Any],
) -> dict[str, Any]:
    payload = (
        dict(raw_body)
        if isinstance(raw_body, Mapping)
        else json.loads(raw_body.decode("utf-8"))
    )
    if not isinstance(payload, dict):
        raise ValueError("Pipedream delivery payload must be a JSON object")
    return payload


def _extract_deployed_trigger_id(body: Mapping[str, Any]) -> str | None:
    data = body.get("data")
    if isinstance(data, Mapping):
        trigger_id = data.get("id")
        if isinstance(trigger_id, str) and trigger_id.strip():
            return trigger_id.strip()
    trigger_id = body.get("id")
    if isinstance(trigger_id, str) and trigger_id.strip():
        return trigger_id.strip()
    return None


def _extract_webhook_signing_key(body: Mapping[str, Any]) -> str | None:
    data = body.get("data")
    if isinstance(data, Mapping):
        signing_key = data.get("webhook_signing_key")
        if isinstance(signing_key, str) and signing_key.strip():
            return signing_key.strip()
    signing_key = body.get("webhook_signing_key")
    if isinstance(signing_key, str) and signing_key.strip():
        return signing_key.strip()
    return None


def _configured_props(request: TriggerProvisionRequest) -> dict[str, Any]:
    """Merge authored trigger_config with the connection auth binding."""

    props = dict(request.trigger_config)
    app_key = request.canonical_app_slug
    app_binding = props.get(app_key)
    if isinstance(app_binding, dict):
        merged = dict(app_binding)
        merged.setdefault("authProvisionId", request.provider_connection_id)
        props[app_key] = merged
    else:
        props[app_key] = {"authProvisionId": request.provider_connection_id}
    return props


class PipedreamTriggerAdapter(TriggerProviderAdapter):
    """Pipedream transport for provider-native trigger subscriptions."""

    backend_id = PIPEDREAM_BACKEND_ID

    def __init__(
        self,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        project_id: str | None = None,
        environment: str | None = None,
        base_url: str | None = None,
        oauth_token_url: str | None = None,
        account_subject_pepper: str | None = None,
        timeout_seconds: int = 30,
        request_fn: HttpRequestFn | None = None,
    ) -> None:
        self.client_id = client_id or os.getenv("PIPEDREAM_CLIENT_ID")
        self.client_secret = client_secret or os.getenv("PIPEDREAM_CLIENT_SECRET")
        self.project_id = project_id or os.getenv("PIPEDREAM_PROJECT_ID")
        self.environment = (
            environment or os.getenv("PIPEDREAM_ENVIRONMENT") or "production"
        ).strip()
        self.base_url = (
            base_url
            or os.getenv("PIPEDREAM_CONNECT_BASE_URL")
            or DEFAULT_PIPEDREAM_CONNECT_BASE_URL
        ).rstrip("/")
        self.oauth_token_url = (
            oauth_token_url
            or os.getenv("PIPEDREAM_OAUTH_TOKEN_URL")
            or DEFAULT_PIPEDREAM_OAUTH_TOKEN_URL
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
        self._access_token_cache: str | None = None

    def _default_request(self, method: str, url: str, **kwargs: Any) -> _HttpResponse:
        return requests.request(method, url, timeout=self.timeout_seconds, **kwargs)

    def _access_token(self) -> str:
        if self._access_token_cache:
            return self._access_token_cache
        if not self.client_id or not self.client_secret:
            raise ValueError(
                "PIPEDREAM_CLIENT_ID and PIPEDREAM_CLIENT_SECRET are required",
            )
        response = self._request(
            "POST",
            self.oauth_token_url,
            json={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        token = response.json().get("access_token")
        if not isinstance(token, str) or not token.strip():
            raise ValueError("Pipedream token response did not include access_token")
        self._access_token_cache = token.strip()
        return self._access_token_cache

    def _api_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token()}",
            "Content-Type": "application/json",
            "X-PD-Environment": self.environment,
        }

    def resolve_account_identity(
        self,
        *,
        provider_connection_id: str,
    ) -> ProviderAccountIdentity:
        if not provider_connection_id:
            raise ValueError("provider_connection_id is required")
        if not self.project_id:
            raise ValueError("PIPEDREAM_PROJECT_ID is required")
        response = self._request(
            "GET",
            f"{self.base_url}/{self.project_id}/accounts/{provider_connection_id}",
            headers=self._api_headers(),
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Pipedream account payload was not an object")
        account = data.get("data") if isinstance(data.get("data"), dict) else data
        if not isinstance(account, dict):
            raise ValueError("Pipedream account payload was not an object")
        subject = (
            account.get("external_user_id")
            or account.get("externalUserId")
            or account.get("external_id")
            or account.get("id")
        )
        if not isinstance(subject, str) or not subject.strip():
            raise ValueError("Pipedream account is missing external_user_id")
        subject = subject.strip()
        app = account.get("app")
        app_name = app.get("name") if isinstance(app, Mapping) else None
        display = str(account.get("name") or app_name or "").strip() or subject
        label = f"{display}:{subject}"
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
            raw=account,
        )

    def provision(self, request: TriggerProvisionRequest) -> TriggerProvisionResult:
        if not self.project_id:
            raise ValueError("PIPEDREAM_PROJECT_ID is required")
        if not request.callback_url:
            raise ValueError("callback_url is required for Pipedream trigger deploy")
        payload = {
            "external_user_id": request.provider_user_id,
            "id": request.provider_trigger_slug,
            "configured_props": _configured_props(request),
            "webhook_url": request.callback_url,
            "emit_on_deploy": False,
        }
        _ = request.ingress_key, request.idempotency_key
        response = self._request(
            "POST",
            f"{self.base_url}/{self.project_id}/triggers/deploy",
            headers=self._api_headers(),
            json=payload,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Pipedream trigger deploy failed: {response.status_code} "
                f"{response.text[:300]}",
            )
        body = response.json()
        if not isinstance(body, dict):
            raise RuntimeError("Pipedream trigger deploy returned a non-object body")
        external_trigger_id = _extract_deployed_trigger_id(body)
        if not external_trigger_id:
            raise RuntimeError("Pipedream trigger deploy response missing trigger id")
        signing_key = _extract_webhook_signing_key(body)
        if not signing_key:
            raise RuntimeError(
                "Pipedream trigger deploy response missing webhook_signing_key",
            )
        generation_id = request.generation_id or request.idempotency_key
        signing_secret_ref, signing_secret_version = wrap_signing_secret_for_generation(
            generation_id=generation_id,
            signing_key=signing_key,
        )
        return TriggerProvisionResult(
            external_trigger_id=external_trigger_id,
            signing_secret_ref=signing_secret_ref,
            signing_secret_version=signing_secret_version,
            raw=body,
        )

    def delete(self, request: TriggerDeleteRequest) -> None:
        if not request.external_trigger_id or not self.project_id:
            return
        params: dict[str, str] = {}
        if request.provider_user_id:
            params["external_user_id"] = request.provider_user_id
        response = self._request(
            "DELETE",
            f"{self.base_url}/{self.project_id}/deployed-triggers/"
            f"{request.external_trigger_id}",
            headers=self._api_headers(),
            params=params or None,
        )
        if response.status_code in {404, 410}:
            return
        if response.status_code >= 400:
            raise RuntimeError(
                f"Pipedream trigger delete failed: {response.status_code} "
                f"{response.text[:300]}",
            )

    def verify_delivery(
        self,
        *,
        headers: Mapping[str, str],
        raw_body: bytes,
        signing_secrets: Sequence[str],
        tolerance_seconds: int | None = None,
    ) -> bool:
        signature_header = _header_value(headers, "x-pd-signature")
        if not signature_header:
            return False
        secrets = [secret for secret in signing_secrets if secret]
        if not secrets:
            return False
        tolerance = (
            DEFAULT_SIGNATURE_TOLERANCE_SECONDS
            if tolerance_seconds is None
            else tolerance_seconds
        )
        return any(
            verify_pipedream_signature(
                signing_key=secret,
                raw_body=raw_body,
                signature_header=signature_header,
                tolerance_seconds=tolerance,
            )
            for secret in secrets
        )

    def verify_unrouted_delivery(
        self,
        *,
        headers: Mapping[str, str],
        raw_body: bytes,
    ) -> bool:
        _ = headers, raw_body
        return False

    def delivery_external_trigger_id(
        self,
        *,
        headers: Mapping[str, str],
        raw_body: bytes,
    ) -> str | None:
        _ = headers
        payload = _parse_delivery_payload(raw_body)
        for field in ("trigger_id", "deployed_component_id", "dc_id"):
            value = payload.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def normalize_delivery(
        self,
        *,
        headers: Mapping[str, str],
        raw_body: bytes | Mapping[str, Any],
    ) -> NormalizedProviderDelivery:
        payload = _parse_delivery_payload(raw_body)
        identity = pipedream_delivery_identity(headers, payload)
        if not identity:
            raise ValueError("Pipedream delivery is missing retry-stable identity")
        external_trigger_id = self.delivery_external_trigger_id(
            headers=headers,
            raw_body=(
                raw_body
                if isinstance(raw_body, bytes)
                else json.dumps(payload, separators=(",", ":")).encode("utf-8")
            ),
        )
        envelope = {
            "backend_id": self.backend_id,
            "provider_trigger_slug": payload.get("component_key")
            or payload.get("component_id"),
            "provider_event_identity": identity,
            "external_trigger_id": external_trigger_id,
        }
        return NormalizedProviderDelivery(
            provider_event_identity=identity,
            provider_trigger_slug=str(envelope.get("provider_trigger_slug") or ""),
            external_trigger_id=external_trigger_id,
            connected_account_id=None,
            provider_user_id=None,
            envelope=envelope,
            occurred_at=None,
            source_body=payload,
        )

    def stable_event_identity(
        self,
        delivery: Mapping[str, Any] | NormalizedProviderDelivery,
    ) -> str | None:
        if isinstance(delivery, NormalizedProviderDelivery):
            return delivery.provider_event_identity
        return pipedream_delivery_identity({}, delivery)

    def health(
        self,
        *,
        external_trigger_id: str | None,
        provider_connection_id: str | None,
    ) -> TriggerHealthResult:
        if not provider_connection_id:
            return TriggerHealthResult(
                status="error",
                error_code="provider_connection_missing",
            )
        if not self.project_id:
            return TriggerHealthResult(
                status="error",
                error_code="provider_not_configured",
            )
        try:
            response = self._request(
                "GET",
                f"{self.base_url}/{self.project_id}/accounts/{provider_connection_id}",
                headers=self._api_headers(),
            )
            response.raise_for_status()
            account_body = response.json()
        except Exception as exc:
            logger.exception("Pipedream trigger health check failed")
            return TriggerHealthResult(
                status="error",
                error_code="provider_health_check_failed",
                detail={"message": str(exc)},
            )
        account = account_body.get("data") if isinstance(account_body, dict) else None
        if not isinstance(account, dict):
            account = account_body if isinstance(account_body, dict) else {}
        if account.get("dead") is True:
            return TriggerHealthResult(
                status="error",
                error_code="provider_connection_not_active",
                detail={"provider_status": "dead"},
            )
        if external_trigger_id:
            try:
                trigger_response = self._request(
                    "GET",
                    f"{self.base_url}/{self.project_id}/deployed-triggers/"
                    f"{external_trigger_id}",
                    headers=self._api_headers(),
                )
                if trigger_response.status_code == 404:
                    return TriggerHealthResult(
                        status="error",
                        error_code="provider_subscription_missing",
                    )
                trigger_response.raise_for_status()
                trigger_body = trigger_response.json()
                trigger = (
                    trigger_body.get("data") if isinstance(trigger_body, dict) else None
                )
                if not isinstance(trigger, dict):
                    trigger = trigger_body if isinstance(trigger_body, dict) else {}
                if trigger.get("active") is False:
                    return TriggerHealthResult(
                        status="error",
                        error_code="provider_subscription_inactive",
                    )
            except Exception as exc:
                logger.exception("Pipedream deployed trigger health check failed")
                return TriggerHealthResult(
                    status="error",
                    error_code="provider_health_check_failed",
                    detail={"message": str(exc)},
                )
        return TriggerHealthResult(
            status="ok",
            detail={
                "connected_account_id": provider_connection_id,
                "external_trigger_id": external_trigger_id,
            },
        )
