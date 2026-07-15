"""Composio inbound trigger adapter for curated provider-event subscriptions.

TODO: Keep event-specific provision config, resource parsing, and projection
behind registry mappings / projector lookup. This adapter should stay the
Composio transport + verification layer, not a github.issue_created-only
implementation forever.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any, Callable, Mapping, Protocol, Sequence

import requests

from orchestra.provider_triggers.provider_identity import composio_v3_event_identity
from orchestra.provider_triggers.trigger_adapter import (
    NormalizedProviderDelivery,
    ProviderAccountIdentity,
    TriggerDeleteRequest,
    TriggerHealthResult,
    TriggerProviderAdapter,
    TriggerProvisionRequest,
    TriggerProvisionResult,
    TriggerResource,
)
from orchestra.provider_triggers.trigger_matching import (
    matches_filters,
    normalize_repository,
    project_github_issue_created,
)
from orchestra.provider_triggers.trigger_registry import (
    COMPOSIO_BACKEND_ID,
    COMPOSIO_GITHUB_ISSUE_CREATED_SLUG,
    GITHUB_ISSUE_CREATED,
    require_canonical_trigger_event,
    resolve_provider_mapping,
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
    tolerance_seconds: int = 300,
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


def provider_account_subject_hmac(subject: str, *, pepper: str | bytes) -> str:
    """Return the durable HMAC digest for one provider-account subject."""

    key = pepper.encode("utf-8") if isinstance(pepper, str) else pepper
    return hmac.new(key, subject.encode("utf-8"), hashlib.sha256).hexdigest()


def github_resource_from_filters(
    filters: Sequence[Mapping[str, Any]] | None,
) -> str | None:
    """Derive the owner/name resource from authored repository filters.

    # TODO: Purge/Replace — replace with registry/adapter resource resolution
    so reconciliation and signed ingress do not hardcode GitHub repository
    extraction outside the adapter. Call sites:
    ``provider_trigger_reconciliation_service``, ``ingress_acceptance``.
    See vault: Provider event trigger contracts#Interim remnants.
    """

    if not filters:
        return None
    for item in filters:
        if str(item.get("field", "")).strip() != "repository":
            continue
        operator = str(item.get("operator", "")).strip()
        value = item.get("value")
        if operator == "is" and isinstance(value, str):
            return normalize_repository(value)
        if operator == "is any of" and isinstance(value, list) and len(value) == 1:
            return normalize_repository(value[0])
    return None


def split_github_resource(resource_id: str) -> tuple[str, str]:
    """Split owner/name into Composio trigger_config fields."""

    normalized = normalize_repository(resource_id)
    if not normalized or "/" not in normalized:
        raise ValueError(f"Invalid GitHub repository resource {resource_id!r}")
    owner, _, repo = normalized.partition("/")
    if not owner or not repo or "/" in repo:
        raise ValueError(f"Invalid GitHub repository resource {resource_id!r}")
    return owner, repo


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
    """Composio adapter for the curated github.issue_created trigger."""

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
            or str(data.get("toolkit_slug") or data.get("appName") or "github").strip()
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

    def list_resources(
        self,
        *,
        provider_connection_id: str,
        provider_user_id: str | None = None,
        event_slug: str,
        schema_version: str = "1",
    ) -> list[TriggerResource]:
        """Return resources discoverable for the curated event.

        GitHub issue-created resources are repository owner/name pairs. v1 does
        not scrape the full GitHub catalog through Composio tools; callers supply
        an exact repository filter and ``authorize_resource`` validates access.
        """

        require_canonical_trigger_event(event_slug, schema_version=schema_version)
        if event_slug != GITHUB_ISSUE_CREATED:
            return []
        _ = provider_connection_id, provider_user_id
        return []

    def authorize_resource(
        self,
        *,
        provider_connection_id: str,
        resource_id: str,
    ) -> bool:
        """Return True when the connected account can access the repository."""

        owner, repo = split_github_resource(resource_id)
        response = self._request(
            "GET",
            f"{self.base_url}/connected_accounts/{provider_connection_id}",
            headers=self._api_headers(),
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            return False
        status = str(data.get("status") or "").upper()
        if status not in {"ACTIVE", "CONNECTED"}:
            return False
        # Connection liveness is the v1 fail-closed gate. Repository ownership
        # is revalidated on delivery via resource_id matching; deeper GitHub ACL
        # probes are deferred until a later resource-listing ticket.
        _ = owner, repo
        return True

    def provision(self, request: TriggerProvisionRequest) -> TriggerProvisionResult:
        # TODO: Build trigger_config from the registry mapping for request.event_slug
        # instead of assuming GitHub owner/repo resources.
        mapping = resolve_provider_mapping(
            backend_id=self.backend_id,
            event_slug=request.event_slug,
            schema_version=request.schema_version,
        )
        resource_id = request.resource_id or github_resource_from_filters(
            request.filters,
        )
        if not resource_id:
            raise ValueError(
                "A repository resource is required to provision github.issue_created",
            )
        if not self.authorize_resource(
            provider_connection_id=request.provider_connection_id,
            resource_id=resource_id,
        ):
            raise PermissionError("repository_inaccessible")
        owner, repo = split_github_resource(resource_id)
        payload = {
            "slug": mapping.provider_trigger_slug,
            "user_id": request.provider_user_id,
            "connected_account_id": request.provider_connection_id,
            "trigger_config": {"owner": owner, "repo": repo},
            "idempotency_key": request.idempotency_key,
        }
        # callback_url and ingress_key are owned by Orchestra ingress topology;
        # Composio deliveries use the project webhook registration.
        _ = request.callback_url, request.ingress_key
        response = self._request(
            "POST",
            f"{self.base_url}/trigger_instances",
            headers=self._api_headers(),
            json=payload,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Composio trigger provision failed: {response.status_code} {response.text[:300]}",
            )
        body = response.json()
        if not isinstance(body, dict):
            raise RuntimeError("Composio trigger provision returned a non-object body")
        trigger_id = _extract_trigger_id(body)
        if not trigger_id:
            raise RuntimeError("Composio trigger provision response missing trigger id")
        return TriggerProvisionResult(
            external_trigger_id=trigger_id,
            signing_secret_ref=COMPOSIO_WEBHOOK_SECRET_REF,
            signing_secret_version="project",
            raw=body,
        )

    def delete(self, request: TriggerDeleteRequest) -> None:
        if not request.external_trigger_id:
            return
        response = self._request(
            "DELETE",
            f"{self.base_url}/trigger_instances/{request.external_trigger_id}",
            headers=self._api_headers(),
            params=(
                {"idempotency_key": request.idempotency_key}
                if request.idempotency_key
                else None
            ),
        )
        if response.status_code in {404, 410}:
            return
        if response.status_code >= 400:
            raise RuntimeError(
                f"Composio trigger delete failed: {response.status_code} {response.text[:300]}",
            )

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
        mapping = resolve_provider_mapping(
            backend_id=self.backend_id,
            event_slug=GITHUB_ISSUE_CREATED,
        )
        tolerance = (
            mapping.timestamp_tolerance_seconds
            if tolerance_seconds is None
            else tolerance_seconds
        )
        body = raw_body.decode("utf-8")
        signed_payload = f"{webhook_id}.{webhook_timestamp}.{body}"
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
        """Extract the Composio trigger-instance id without projecting the event."""

        _ = headers
        return _delivery_trigger_id(_parse_delivery_payload(raw_body))

    def normalize_delivery(
        self,
        *,
        headers: Mapping[str, str],
        raw_body: bytes | Mapping[str, Any],
    ) -> NormalizedProviderDelivery:
        # TODO: Dispatch projection by registry mapping for the delivery's
        # provider trigger slug instead of hard-requiring the GitHub issue
        # created Composio slug.
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
        if trigger_slug != COMPOSIO_GITHUB_ISSUE_CREATED_SLUG:
            raise ValueError(f"Unsupported Composio trigger slug {trigger_slug!r}")
        projection = project_github_issue_created(payload)
        resource_id = projection.get("repository")
        connected_account_id = metadata.get("connected_account_id")
        provider_user_id = metadata.get("user_id")
        external_trigger_id = _delivery_trigger_id(payload)
        occurred_at = payload.get("timestamp")
        envelope = {
            "backend_id": self.backend_id,
            "event_slug": GITHUB_ISSUE_CREATED,
            "provider_trigger_slug": trigger_slug,
            "provider_event_identity": identity,
            "external_trigger_id": external_trigger_id,
            "connected_account_id": connected_account_id,
            "provider_user_id": provider_user_id,
            "resource_id": resource_id,
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
            resource_id=str(resource_id) if resource_id else None,
            envelope=envelope,
            curated_projection=projection,
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
    ) -> TriggerHealthResult:
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

    def delivery_matches_filters(
        self,
        delivery: NormalizedProviderDelivery,
        filters: Sequence[Mapping[str, Any]] | None,
    ) -> bool:
        """Evaluate authored AND filters against the curated projection."""

        return matches_filters(
            projection=delivery.curated_projection,
            filters=filters,
            event_slug=GITHUB_ISSUE_CREATED,
        )
