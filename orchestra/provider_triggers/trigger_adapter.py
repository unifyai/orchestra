"""Trigger-provider adapter contract separate from outbound action adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class ProviderAccountIdentity:
    """Stable provider-account identity pinned onto a trigger binding."""

    subject: str
    display_label: str
    subject_hmac: str | None = None
    connected_account_id: str | None = None
    provider_user_id: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TriggerResource:
    """One resource a user may bind a curated trigger against."""

    resource_id: str
    display_label: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TriggerProvisionRequest:
    """Inputs required to create one provider subscription generation."""

    connection_id: str
    provider_connection_id: str
    provider_user_id: str
    event_slug: str
    schema_version: str
    canonical_app_slug: str
    callback_url: str
    idempotency_key: str
    ingress_key: str
    resource_id: str
    filters: Sequence[Mapping[str, Any]] = ()
    generation_id: str | None = None


@dataclass(frozen=True)
class TriggerProvisionResult:
    """Provider-facing result of a successful subscription create."""

    external_trigger_id: str
    signing_secret_ref: str | None = None
    signing_secret_version: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TriggerDeleteRequest:
    """Inputs required to tear down one provider subscription generation."""

    external_trigger_id: str
    idempotency_key: str
    provider_connection_id: str | None = None
    provider_user_id: str | None = None


@dataclass(frozen=True)
class NormalizedProviderDelivery:
    """Provider-neutral delivery after verification and normalization."""

    provider_event_identity: str
    provider_trigger_slug: str
    external_trigger_id: str | None
    connected_account_id: str | None
    provider_user_id: str | None
    resource_id: str | None
    envelope: dict[str, Any]
    curated_projection: dict[str, Any]
    occurred_at: str | None
    source_body: dict[str, Any]


@dataclass(frozen=True)
class TriggerHealthResult:
    """Health observation for one provisioned subscription generation."""

    status: str
    detail: Mapping[str, Any] = field(default_factory=dict)
    error_code: str | None = None


class TriggerProviderAdapter(ABC):
    """Inheritance contract for inbound trigger-provider backends.

    Outbound tool/action adapters remain on ``BaseIntegrationProviderAdapter``.
    Trigger adapters own account pinning, subscription lifecycle, delivery
    verification, and curated projection for one ``backend_id``.
    """

    backend_id: str = "custom"

    @abstractmethod
    def resolve_account_identity(
        self,
        *,
        provider_connection_id: str,
    ) -> ProviderAccountIdentity:
        """Resolve the immutable provider-account subject for one connection."""

    @abstractmethod
    def list_resources(
        self,
        *,
        provider_connection_id: str,
        provider_user_id: str | None = None,
        event_slug: str,
        schema_version: str = "1",
    ) -> list[TriggerResource]:
        """List resources visible through the connection for one event."""

    @abstractmethod
    def provision(self, request: TriggerProvisionRequest) -> TriggerProvisionResult:
        """Create or adopt a provider subscription for one generation."""

    @abstractmethod
    def delete(self, request: TriggerDeleteRequest) -> None:
        """Delete one provider subscription, adopting provider idempotency."""

    @abstractmethod
    def verify_delivery(
        self,
        *,
        headers: Mapping[str, str],
        raw_body: bytes,
        signing_secrets: Sequence[str],
        tolerance_seconds: int | None = None,
    ) -> bool:
        """Return True when the delivery authenticates against accepted secrets."""

    @abstractmethod
    def normalize_delivery(
        self,
        *,
        headers: Mapping[str, str],
        raw_body: bytes | Mapping[str, Any],
    ) -> NormalizedProviderDelivery:
        """Normalize a verified delivery into the curated projection contract."""

    @abstractmethod
    def stable_event_identity(
        self,
        delivery: Mapping[str, Any] | NormalizedProviderDelivery,
    ) -> str | None:
        """Return the retry-stable provider event identity when present."""

    @abstractmethod
    def health(
        self,
        *,
        external_trigger_id: str | None,
        provider_connection_id: str | None,
    ) -> TriggerHealthResult:
        """Probe provider-side health for one generation or connection."""

    def authorize_delivery(
        self,
        *,
        delivery: NormalizedProviderDelivery,
        expected_connected_account_id: str,
        expected_external_trigger_id: str | None,
        expected_provider_user_id: str | None,
        expected_resource_id: str | None,
    ) -> str | None:
        """Return a stable error code when delivery authorization fails.

        When the binding pins an expected value, the delivery must present a
        matching value. Missing delivery fields fail closed.
        """

        if not delivery.connected_account_id:
            return "connected_account_mismatch"
        if delivery.connected_account_id != expected_connected_account_id:
            return "connected_account_mismatch"
        if expected_external_trigger_id:
            if not delivery.external_trigger_id:
                return "subscription_mismatch"
            if delivery.external_trigger_id != expected_external_trigger_id:
                return "subscription_mismatch"
        if expected_provider_user_id:
            if not delivery.provider_user_id:
                return "provider_user_mismatch"
            if delivery.provider_user_id != expected_provider_user_id:
                return "provider_user_mismatch"
        if expected_resource_id:
            if not delivery.resource_id:
                return "resource_mismatch"
            if delivery.resource_id.casefold() != expected_resource_id.casefold():
                return "resource_mismatch"
        return None
