"""Registry for inbound trigger-provider adapters."""

from __future__ import annotations

import os
from typing import Type

from sqlalchemy.orm import Session

from orchestra.provider_triggers.backend_ids import (
    COMPOSIO_BACKEND_ID,
    NATIVE_GOOGLE_BACKEND_ID,
    NATIVE_MICROSOFT_BACKEND_ID,
    PIPEDREAM_BACKEND_ID,
)
from orchestra.provider_triggers.composio_trigger_adapter import ComposioTriggerAdapter
from orchestra.provider_triggers.local_composio_trigger_adapter import (
    LocalComposioTriggerAdapter,
)
from orchestra.provider_triggers.local_native_google_trigger_adapter import (
    LocalNativeGoogleTriggerAdapter,
)
from orchestra.provider_triggers.local_native_microsoft_trigger_adapter import (
    LocalNativeMicrosoftTriggerAdapter,
)
from orchestra.provider_triggers.local_pipedream_trigger_adapter import (
    LocalPipedreamTriggerAdapter,
)
from orchestra.provider_triggers.native_google_trigger_adapter import (
    NativeGoogleTriggerAdapter,
)
from orchestra.provider_triggers.native_microsoft_trigger_adapter import (
    NativeMicrosoftTriggerAdapter,
)
from orchestra.provider_triggers.pipedream_trigger_adapter import (
    PipedreamTriggerAdapter,
)
from orchestra.provider_triggers.trigger_adapter import TriggerProviderAdapter
from orchestra.provider_triggers.workspace_trigger_credentials import (
    WorkspaceTriggerCredentialLoader,
)

TRIGGER_PROVIDER_ADAPTERS: dict[str, Type[TriggerProviderAdapter]] = {
    COMPOSIO_BACKEND_ID: ComposioTriggerAdapter,
    PIPEDREAM_BACKEND_ID: PipedreamTriggerAdapter,
    NATIVE_GOOGLE_BACKEND_ID: NativeGoogleTriggerAdapter,
    NATIVE_MICROSOFT_BACKEND_ID: NativeMicrosoftTriggerAdapter,
}


def _pipedream_credentials_configured() -> bool:
    return all(
        os.getenv(name, "").strip()
        for name in (
            "PIPEDREAM_CLIENT_ID",
            "PIPEDREAM_CLIENT_SECRET",
            "PIPEDREAM_PROJECT_ID",
        )
    )


def get_trigger_provider_adapter(
    backend_id: str,
    *,
    timeout_seconds: int = 30,
    session: Session | None = None,
) -> TriggerProviderAdapter:
    """Return the inbound trigger adapter for one backend id."""

    adapter_cls = TRIGGER_PROVIDER_ADAPTERS.get(backend_id)
    if adapter_cls is ComposioTriggerAdapter:
        if not os.getenv("COMPOSIO_API_KEY"):
            return LocalComposioTriggerAdapter(timeout_seconds=timeout_seconds)
        return ComposioTriggerAdapter(timeout_seconds=timeout_seconds)
    if adapter_cls is PipedreamTriggerAdapter:
        if not _pipedream_credentials_configured():
            return LocalPipedreamTriggerAdapter(timeout_seconds=timeout_seconds)
        return PipedreamTriggerAdapter(timeout_seconds=timeout_seconds)
    if adapter_cls is NativeGoogleTriggerAdapter:
        if session is None:
            return LocalNativeGoogleTriggerAdapter()
        return NativeGoogleTriggerAdapter(
            credential_loader=WorkspaceTriggerCredentialLoader(session),
            timeout_seconds=timeout_seconds,
        )
    if adapter_cls is NativeMicrosoftTriggerAdapter:
        if session is None:
            return LocalNativeMicrosoftTriggerAdapter()
        return NativeMicrosoftTriggerAdapter(
            credential_loader=WorkspaceTriggerCredentialLoader(session),
            timeout_seconds=timeout_seconds,
        )
    raise LookupError(f"No trigger provider adapter registered for {backend_id!r}")
