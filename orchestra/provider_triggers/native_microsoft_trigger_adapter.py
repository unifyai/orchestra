"""Native Microsoft Graph trigger adapter backed by workspace OAuth credentials."""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Protocol, Sequence
from uuid import UUID

import requests

from orchestra.provider_triggers.backend_ids import NATIVE_MICROSOFT_BACKEND_ID
from orchestra.provider_triggers.local_native_microsoft_trigger_adapter import (
    NATIVE_MICROSOFT_WEBHOOK_SECRET_REF,
    LocalNativeMicrosoftTriggerAdapter,
    _parse_provider_connection_id,
)
from orchestra.provider_triggers.native_microsoft_subscription import (
    build_graph_subscription_body,
    graph_client_state,
    renew_expiration_datetime,
)
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
from orchestra.provider_triggers.workspace_trigger_credentials import (
    WorkspaceTriggerCredentialLoader,
    WorkspaceTriggerCredentials,
)

logger = logging.getLogger(__name__)

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
_RENEW_BEFORE = timedelta(hours=12)
_GRAPH_SUBSCRIPTION_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
)
_NATIVE_TRIGGERS_PATH = "/microsoft/native-triggers"


class _HttpResponse(Protocol):
    status_code: int
    text: str

    def json(self) -> Any: ...

    def raise_for_status(self) -> None: ...


HttpRequestFn = Callable[..., _HttpResponse]


def _is_graph_subscription_id(value: str) -> bool:
    candidate = value.strip()
    if not _GRAPH_SUBSCRIPTION_ID_RE.match(candidate):
        return False
    try:
        UUID(candidate)
    except ValueError:
        return False
    return True


def _parse_expiration(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    # Graph may emit seven fractional digits; fromisoformat accepts up to six.
    if "." in raw:
        head, frac_and_tz = raw.split(".", 1)
        digits = ""
        tz = ""
        for index, char in enumerate(frac_and_tz):
            if char.isdigit():
                digits += char
            else:
                tz = frac_and_tz[index:]
                break
        raw = f"{head}.{digits[:6]}{tz}"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class NativeMicrosoftTriggerAdapter(TriggerProviderAdapter):
    """Microsoft Graph change-notification transport for native triggers."""

    backend_id = NATIVE_MICROSOFT_BACKEND_ID

    def __init__(
        self,
        *,
        credential_loader: WorkspaceTriggerCredentialLoader,
        webhook_secret: str | None = None,
        account_subject_pepper: str | None = None,
        adapters_base_url: str | None = None,
        timeout_seconds: int = 30,
        request_fn: HttpRequestFn | None = None,
    ) -> None:
        self._credential_loader = credential_loader
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
        self._adapters_base_url = adapters_base_url
        self.timeout_seconds = timeout_seconds
        self._request = request_fn or self._default_request
        self._local_delivery = LocalNativeMicrosoftTriggerAdapter(
            webhook_secret=self.webhook_secret,
            account_subject_pepper=self.account_subject_pepper,
        )

    def _default_request(self, method: str, url: str, **kwargs: Any) -> _HttpResponse:
        return requests.request(method, url, timeout=self.timeout_seconds, **kwargs)

    def _resolved_adapters_base_url(self) -> str:
        if self._adapters_base_url is not None:
            return self._adapters_base_url.strip().rstrip("/")
        return (os.getenv("UNIFY_ADAPTERS_URL") or "").strip().rstrip("/")

    def _notification_url(self) -> str:
        base = self._resolved_adapters_base_url()
        if not base:
            raise RuntimeError(
                "UNIFY_ADAPTERS_URL is not configured for native Microsoft "
                "Graph notificationUrl",
            )
        return f"{base}{_NATIVE_TRIGGERS_PATH}"

    def _auth_headers(self, access_token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    def resolve_account_identity(
        self,
        *,
        provider_connection_id: str,
    ) -> ProviderAccountIdentity:
        email = _parse_provider_connection_id(provider_connection_id)
        subject_hmac = None
        if self.account_subject_pepper:
            subject_hmac = provider_account_subject_hmac(
                email,
                pepper=self.account_subject_pepper,
            )
        return ProviderAccountIdentity(
            subject=email,
            display_label=f"microsoft_workspace:{email}",
            subject_hmac=subject_hmac,
            connected_account_id=provider_connection_id,
            provider_user_id=email,
            raw={"provider": self.backend_id},
        )

    def provision(self, request: TriggerProvisionRequest) -> TriggerProvisionResult:
        credentials = self._credential_loader.load_for_connection_id(
            request.connection_id,
        )
        if not credentials.access_token:
            raise PermissionError(
                "workspace access token missing for native Microsoft",
            )

        slug = str(request.provider_trigger_slug or "").strip()
        if not slug:
            raise RuntimeError("native Microsoft provider_trigger_slug is required")
        if not self.webhook_secret:
            raise RuntimeError(
                "NATIVE_MICROSOFT_WEBHOOK_SECRET is not configured",
            )

        notification_url = self._notification_url()
        try:
            body = build_graph_subscription_body(
                provider_trigger_slug=slug,
                trigger_config=request.trigger_config,
                notification_url=notification_url,
                client_state=graph_client_state(self.webhook_secret),
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc

        logger.info(
            "native_microsoft provision assistant=%s slug=%s resource=%s "
            "notification=%s callback=%s",
            credentials.account_email,
            slug,
            body.get("resource"),
            notification_url,
            request.callback_url,
        )
        response = self._request(
            "POST",
            f"{GRAPH_BASE_URL}/subscriptions",
            headers=self._auth_headers(credentials.access_token),
            json=body,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise RuntimeError("Microsoft Graph create response was not an object")

        subscription_id = str(payload.get("id") or "").strip()
        if not subscription_id or not _is_graph_subscription_id(subscription_id):
            raise RuntimeError(
                "Microsoft Graph create returned no subscription id",
            )
        return TriggerProvisionResult(
            external_trigger_id=subscription_id,
            signing_secret_ref=NATIVE_MICROSOFT_WEBHOOK_SECRET_REF,
            signing_secret_version="project",
            raw={
                "id": subscription_id,
                "resource": body.get("resource"),
                "change_type": body.get("changeType"),
                "event_type": slug,
                "notification_url": notification_url,
                "expiration_date_time": payload.get("expirationDateTime"),
                "account_email": credentials.account_email,
            },
        )

    def delete(self, request: TriggerDeleteRequest) -> None:
        if not request.external_trigger_id:
            return
        subscription_id = request.external_trigger_id.strip()
        if not _is_graph_subscription_id(subscription_id):
            # Legacy deterministic ``nm_*`` stub ids never created a real Graph
            # subscription; nothing to delete provider-side.
            logger.info(
                "native_microsoft delete skipped non-live external_trigger_id=%s",
                subscription_id,
            )
            return

        access_token = self._delete_access_token(request)
        if not access_token:
            logger.warning(
                "native_microsoft delete could not load workspace token for %s; "
                "relying on subscription TTL expiry",
                subscription_id,
            )
            return

        response = self._request(
            "DELETE",
            f"{GRAPH_BASE_URL}/subscriptions/{subscription_id}",
            headers=self._auth_headers(access_token),
        )
        if response.status_code in {404, 410}:
            return
        response.raise_for_status()
        logger.info(
            "native_microsoft delete external_trigger_id=%s provider_connection_id=%s",
            subscription_id,
            request.provider_connection_id,
        )

    def _delete_access_token(self, request: TriggerDeleteRequest) -> str | None:
        if not request.connection_id:
            return None
        try:
            credentials = self._credential_loader.load_for_connection_id(
                request.connection_id,
            )
        except (LookupError, ValueError):
            return None
        return credentials.access_token or None

    def verify_delivery(
        self,
        *,
        headers: Mapping[str, str],
        raw_body: bytes,
        signing_secrets: Sequence[str],
        tolerance_seconds: int | None = None,
    ) -> bool:
        return self._local_delivery.verify_delivery(
            headers=headers,
            raw_body=raw_body,
            signing_secrets=signing_secrets,
            tolerance_seconds=tolerance_seconds,
        )

    def normalize_delivery(
        self,
        *,
        headers: Mapping[str, str],
        raw_body: bytes | Mapping[str, Any],
    ) -> NormalizedProviderDelivery:
        return self._local_delivery.normalize_delivery(
            headers=headers,
            raw_body=raw_body,
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
        connection_id: str | None = None,
    ) -> TriggerHealthResult:
        if not provider_connection_id:
            return TriggerHealthResult(
                status="error",
                error_code="provider_connection_missing",
            )
        subscription_id = (external_trigger_id or "").strip()
        if not _is_graph_subscription_id(subscription_id):
            return TriggerHealthResult(
                status="error",
                error_code="provider_subscription_missing",
                detail={"external_trigger_id": external_trigger_id},
            )
        if not connection_id:
            return TriggerHealthResult(
                status="error",
                error_code="provider_connection_missing",
            )
        try:
            credentials = self._credential_loader.load_for_connection_id(connection_id)
        except (LookupError, ValueError) as exc:
            return TriggerHealthResult(
                status="error",
                error_code="provider_connection_missing",
                detail={"message": str(exc)},
            )
        if not credentials.access_token:
            return TriggerHealthResult(
                status="error",
                error_code="provider_connection_not_active",
            )

        try:
            response = self._request(
                "GET",
                f"{GRAPH_BASE_URL}/subscriptions/{subscription_id}",
                headers=self._auth_headers(credentials.access_token),
            )
        except Exception as exc:
            logger.exception(
                "native_microsoft health get failed external_trigger_id=%s",
                subscription_id,
            )
            return TriggerHealthResult(
                status="error",
                error_code="provider_health_check_failed",
                detail={"message": str(exc)},
            )

        if response.status_code in {404, 410}:
            return TriggerHealthResult(
                status="error",
                error_code="provider_subscription_missing",
                detail={"external_trigger_id": subscription_id},
            )
        if response.status_code >= 400:
            return TriggerHealthResult(
                status="error",
                error_code="provider_health_check_failed",
                detail={
                    "status_code": response.status_code,
                    "external_trigger_id": subscription_id,
                },
            )

        payload = response.json()
        if not isinstance(payload, Mapping):
            return TriggerHealthResult(
                status="error",
                error_code="provider_health_check_failed",
            )

        expire_time = _parse_expiration(payload.get("expirationDateTime"))
        if expire_time is None:
            return TriggerHealthResult(
                status="error",
                error_code="provider_health_check_failed",
                detail={
                    "external_trigger_id": subscription_id,
                    "reason": "expiration_missing",
                },
            )
        remaining = expire_time - datetime.now(timezone.utc)
        if remaining <= timedelta(0):
            return TriggerHealthResult(
                status="error",
                error_code="provider_subscription_missing",
                detail={
                    "external_trigger_id": subscription_id,
                    "expiration_date_time": payload.get("expirationDateTime"),
                    "state": "expired",
                },
            )
        renewed = False
        if remaining <= _RENEW_BEFORE:
            renew_error = self._renew_subscription(
                subscription_id,
                credentials=credentials,
            )
            if renew_error is not None:
                return renew_error
            renewed = True

        return TriggerHealthResult(
            status="ok",
            detail={
                "external_trigger_id": subscription_id,
                "connected_account_id": provider_connection_id,
                "expiration_date_time": payload.get("expirationDateTime"),
                "renewed": renewed,
            },
        )

    def _renew_subscription(
        self,
        subscription_id: str,
        *,
        credentials: WorkspaceTriggerCredentials,
    ) -> TriggerHealthResult | None:
        """Extend Graph subscription expiration. None means success."""

        try:
            response = self._request(
                "PATCH",
                f"{GRAPH_BASE_URL}/subscriptions/{subscription_id}",
                headers=self._auth_headers(credentials.access_token),
                json={"expirationDateTime": renew_expiration_datetime()},
            )
        except Exception as exc:
            logger.exception(
                "native_microsoft renew failed external_trigger_id=%s",
                subscription_id,
            )
            return TriggerHealthResult(
                status="error",
                error_code="provider_health_check_failed",
                detail={"message": str(exc), "renewal": "failed"},
            )
        if response.status_code in {404, 410}:
            return TriggerHealthResult(
                status="error",
                error_code="provider_subscription_missing",
                detail={"external_trigger_id": subscription_id, "renewal": "failed"},
            )
        if response.status_code >= 400:
            return TriggerHealthResult(
                status="error",
                error_code="provider_health_check_failed",
                detail={
                    "status_code": response.status_code,
                    "external_trigger_id": subscription_id,
                    "renewal": "failed",
                },
            )
        logger.info(
            "native_microsoft renewed external_trigger_id=%s account=%s",
            subscription_id,
            credentials.account_email,
        )
        return None
