"""Native Microsoft Teams trigger adapter backed by workspace OAuth credentials."""

from __future__ import annotations

import logging
import os
from typing import Any, Mapping, Sequence

from orchestra.provider_triggers.backend_ids import NATIVE_MICROSOFT_BACKEND_ID
from orchestra.provider_triggers.local_native_microsoft_trigger_adapter import (
    NATIVE_MICROSOFT_WEBHOOK_SECRET_REF,
    LocalNativeMicrosoftTriggerAdapter,
    _parse_provider_connection_id,
    _stable_external_trigger_id,
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
)

logger = logging.getLogger(__name__)


class NativeMicrosoftTriggerAdapter(TriggerProviderAdapter):
    """Microsoft Graph change-notification transport for native Teams triggers."""

    backend_id = NATIVE_MICROSOFT_BACKEND_ID

    def __init__(
        self,
        *,
        credential_loader: WorkspaceTriggerCredentialLoader,
        webhook_secret: str | None = None,
        account_subject_pepper: str | None = None,
        timeout_seconds: int = 30,
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
        self.timeout_seconds = timeout_seconds
        self._local_delivery = LocalNativeMicrosoftTriggerAdapter(
            webhook_secret=self.webhook_secret,
            account_subject_pepper=self.account_subject_pepper,
        )

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
            display_label=f"microsoft_teams:{email}",
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

        external_trigger_id = _stable_external_trigger_id(request)
        logger.info(
            "native_microsoft provision assistant=%s slug=%s callback=%s",
            credentials.account_email,
            request.provider_trigger_slug,
            request.callback_url,
        )
        return TriggerProvisionResult(
            external_trigger_id=external_trigger_id,
            signing_secret_ref=NATIVE_MICROSOFT_WEBHOOK_SECRET_REF,
            signing_secret_version="project",
            raw={
                "id": external_trigger_id,
                "status": "active",
                "provider_trigger_slug": request.provider_trigger_slug,
                "account_email": credentials.account_email,
            },
        )

    def delete(self, request: TriggerDeleteRequest) -> None:
        if not request.external_trigger_id:
            return
        logger.info(
            "native_microsoft delete external_trigger_id=%s provider_connection_id=%s",
            request.external_trigger_id,
            request.provider_connection_id,
        )

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
