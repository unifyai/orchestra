"""Native Google Meet trigger adapter backed by workspace OAuth credentials."""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Mapping, Protocol, Sequence

import requests

from orchestra.provider_triggers.backend_ids import (
    NATIVE_GOOGLE_BACKEND_ID,
    NATIVE_GOOGLE_MEET_TRANSCRIPT_SLUG,
)
from orchestra.provider_triggers.local_native_google_trigger_adapter import (
    NATIVE_GOOGLE_WEBHOOK_SECRET_REF,
    LocalNativeGoogleTriggerAdapter,
    _parse_provider_connection_id,
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
from orchestra.settings import settings

logger = logging.getLogger(__name__)

# Google Workspace Events REST surface. ``subscriptions.create`` returns a
# completed long-running operation whose ``response`` carries the created
# ``Subscription`` resource; ``subscriptions.delete`` removes it by name.
WORKSPACE_EVENTS_BASE_URL = "https://workspaceevents.googleapis.com/v1"
# OpenID userinfo returns ``sub`` — the numeric Cloud Identity user id used to
# form the user-level ``targetResource`` for Meet subscriptions. ``userinfo.email``
# (granted on every Google connect) is sufficient for this field.
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
CLOUD_IDENTITY_USER_RESOURCE_PREFIX = "//cloudidentity.googleapis.com/users/"
# subscriptions.create returns an LRO; poll it a few times when it is not
# immediately marked done before failing closed.
_OPERATION_POLL_ATTEMPTS = 5
_OPERATION_POLL_INTERVAL_SECONDS = 1.0


class _HttpResponse(Protocol):
    status_code: int
    text: str

    def json(self) -> Any: ...

    def raise_for_status(self) -> None: ...


HttpRequestFn = Callable[..., _HttpResponse]


def _subscription_name_from_operation(payload: Mapping[str, Any]) -> str | None:
    """Return the Google subscription resource name from a create response."""

    response = payload.get("response")
    if isinstance(response, Mapping):
        name = response.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    # Some responses return the Subscription resource directly.
    name = payload.get("name")
    if isinstance(name, str) and name.strip().startswith("subscriptions/"):
        return name.strip()
    return None


class NativeGoogleTriggerAdapter(TriggerProviderAdapter):
    """Google Workspace Events transport for native Meet trigger subscriptions."""

    backend_id = NATIVE_GOOGLE_BACKEND_ID

    def __init__(
        self,
        *,
        credential_loader: WorkspaceTriggerCredentialLoader,
        webhook_secret: str | None = None,
        account_subject_pepper: str | None = None,
        pubsub_topic: str | None = None,
        timeout_seconds: int = 30,
        request_fn: HttpRequestFn | None = None,
    ) -> None:
        self._credential_loader = credential_loader
        self.webhook_secret = (
            webhook_secret
            if webhook_secret is not None
            else os.getenv("NATIVE_GOOGLE_WEBHOOK_SECRET")
        )
        self.account_subject_pepper = (
            account_subject_pepper
            if account_subject_pepper is not None
            else os.getenv("TRIGGER_ACCOUNT_SUBJECT_PEPPER")
            or os.getenv("TRIGGER_EVENT_WRAPPING_MASTER_KEY")
            or ""
        )
        self._pubsub_topic = pubsub_topic
        self.timeout_seconds = timeout_seconds
        self._request = request_fn or self._default_request
        self._local_delivery = LocalNativeGoogleTriggerAdapter(
            webhook_secret=self.webhook_secret,
            account_subject_pepper=self.account_subject_pepper,
        )

    def _default_request(self, method: str, url: str, **kwargs: Any) -> _HttpResponse:
        return requests.request(method, url, timeout=self.timeout_seconds, **kwargs)

    def _resolved_pubsub_topic(self) -> str:
        topic = self._pubsub_topic
        if topic is None:
            topic = settings.native_google_meet_events_pubsub_topic or ""
        return topic.strip()

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
            display_label=f"google_meet:{email}",
            subject_hmac=subject_hmac,
            connected_account_id=provider_connection_id,
            provider_user_id=email,
            raw={"provider": self.backend_id},
        )

    def _resolve_cloud_identity_user(
        self,
        credentials: WorkspaceTriggerCredentials,
    ) -> str:
        """Return the connected account's numeric Cloud Identity user id."""

        response = self._request(
            "GET",
            GOOGLE_USERINFO_URL,
            headers=self._auth_headers(credentials.access_token),
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, Mapping):
            raise RuntimeError("Google userinfo response was not an object")
        user_id = data.get("sub") or data.get("id")
        if not isinstance(user_id, str) or not user_id.strip():
            raise RuntimeError("Google userinfo response missing user id")
        return user_id.strip()

    def provision(self, request: TriggerProvisionRequest) -> TriggerProvisionResult:
        credentials = self._credential_loader.load_for_connection_id(
            request.connection_id,
        )
        if not credentials.access_token:
            raise PermissionError("workspace access token missing for native Google")

        slug = str(request.provider_trigger_slug).strip()
        if slug and not slug.startswith("google.workspace.meet."):
            # Drive/Chat families need a resource-scoped ``targetResource`` in the
            # Workspace Events create body (ticket 26). Until that lands, fail
            # closed rather than register a user-level subscription that would
            # falsely report healthy for the wrong resource.
            raise RuntimeError(
                "native Google provisioning for non-Meet families requires "
                f"resource targeting that is not yet available: {slug}",
            )

        pubsub_topic = self._resolved_pubsub_topic()
        if not pubsub_topic:
            # Fail closed: without the shared Meet events topic no subscription
            # can ever deliver, so never register a false-healthy generation.
            raise RuntimeError(
                "native Google Meet events pubsub topic is not configured",
            )

        user_id = self._resolve_cloud_identity_user(credentials)
        target_resource = f"{CLOUD_IDENTITY_USER_RESOURCE_PREFIX}{user_id}"
        event_type = (
            str(request.provider_trigger_slug).strip()
            or NATIVE_GOOGLE_MEET_TRANSCRIPT_SLUG
        )
        body: dict[str, Any] = {
            "targetResource": target_resource,
            "eventTypes": [event_type],
            "notificationEndpoint": {"pubsubTopic": pubsub_topic},
            # Meet events never include resource data in the payload; excluding it
            # also grants the maximum subscription TTL for the staging window.
            "payloadOptions": {"includeResource": False},
        }
        logger.info(
            "native_google provision assistant=%s slug=%s target=%s callback=%s",
            credentials.account_email,
            event_type,
            target_resource,
            request.callback_url,
        )
        response = self._request(
            "POST",
            f"{WORKSPACE_EVENTS_BASE_URL}/subscriptions",
            headers=self._auth_headers(credentials.access_token),
            json=body,
        )
        response.raise_for_status()
        operation = response.json()
        if not isinstance(operation, Mapping):
            raise RuntimeError(
                "Google Workspace Events create response was not an object",
            )

        subscription_name = self._await_subscription_name(
            operation,
            credentials=credentials,
        )
        if not subscription_name:
            # Fail closed rather than persist the deterministic ``ng_*`` stub id.
            raise RuntimeError(
                "Google Workspace Events create returned no subscription name",
            )
        return TriggerProvisionResult(
            external_trigger_id=subscription_name,
            signing_secret_ref=NATIVE_GOOGLE_WEBHOOK_SECRET_REF,
            signing_secret_version="project",
            raw={
                "id": subscription_name,
                "target_resource": target_resource,
                "event_type": event_type,
                "pubsub_topic": pubsub_topic,
                "account_email": credentials.account_email,
            },
        )

    def _await_subscription_name(
        self,
        operation: Mapping[str, Any],
        *,
        credentials: WorkspaceTriggerCredentials,
    ) -> str | None:
        name = _subscription_name_from_operation(operation)
        if name:
            return name
        operation_name = operation.get("name")
        if operation.get("done") is False and isinstance(operation_name, str):
            for _ in range(_OPERATION_POLL_ATTEMPTS):
                time.sleep(_OPERATION_POLL_INTERVAL_SECONDS)
                poll = self._request(
                    "GET",
                    f"{WORKSPACE_EVENTS_BASE_URL}/{operation_name}",
                    headers=self._auth_headers(credentials.access_token),
                )
                poll.raise_for_status()
                polled = poll.json()
                if not isinstance(polled, Mapping):
                    continue
                name = _subscription_name_from_operation(polled)
                if name:
                    return name
                if polled.get("done") and polled.get("error"):
                    break
        return None

    def delete(self, request: TriggerDeleteRequest) -> None:
        if not request.external_trigger_id:
            return
        subscription_name = request.external_trigger_id.strip()
        if not subscription_name.startswith("subscriptions/"):
            # Legacy deterministic ``ng_*`` stub ids never created a real Google
            # subscription; nothing to delete provider-side.
            logger.info(
                "native_google delete skipped non-live external_trigger_id=%s",
                subscription_name,
            )
            return

        access_token = self._delete_access_token(request)
        if not access_token:
            # Credentials are gone (connection removed); the subscription lapses
            # on its own TTL. Do not block teardown.
            logger.warning(
                "native_google delete could not load workspace token for %s; "
                "relying on subscription TTL expiry",
                subscription_name,
            )
            return

        response = self._request(
            "DELETE",
            f"{WORKSPACE_EVENTS_BASE_URL}/{subscription_name}",
            headers=self._auth_headers(access_token),
        )
        if response.status_code in {404, 410}:
            return
        response.raise_for_status()
        logger.info(
            "native_google delete external_trigger_id=%s provider_connection_id=%s",
            subscription_name,
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
    ) -> TriggerHealthResult:
        _ = external_trigger_id
        if not provider_connection_id:
            return TriggerHealthResult(
                status="error",
                error_code="provider_connection_missing",
            )
        return TriggerHealthResult(status="ok")
