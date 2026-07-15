"""Shared fixtures for provider-trigger tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestra.provider_triggers.topology import ProviderTriggerTopologyStatus
from orchestra.settings import settings


@pytest.fixture(autouse=True)
def configure_provider_event_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Give provider-trigger tests a configured self-host storage backend."""

    private_root = tmp_path / "provider-event-private"
    monkeypatch.setattr(
        settings,
        "trigger_event_wrapping_master_key",
        "test-master-key-material",
    )
    monkeypatch.setattr(settings, "trigger_event_private_root", str(private_root))
    monkeypatch.setenv("SELF_HOST", "1")
    # Satisfy topology signing + callback gates used by reconciliation/catalog.
    monkeypatch.setenv("COMPOSIO_WEBHOOK_SECRET", "test-composio-webhook-secret")
    monkeypatch.setattr(
        settings,
        "orchestra_trigger_callback_base_url",
        "https://orchestra.example",
    )


def stub_healthy_provider_trigger_topology(
    monkeypatch: pytest.MonkeyPatch,
) -> ProviderTriggerTopologyStatus:
    """Force evaluate_provider_trigger_topology to report a healthy deployment."""

    healthy = ProviderTriggerTopologyStatus(
        available=True,
        unavailable_reason=None,
        callback_base_url="https://orchestra.example",
        event_storage_configured=True,
        signing_configured=True,
        worker_healthy=True,
    )
    monkeypatch.setattr(
        "orchestra.provider_triggers.topology.evaluate_provider_trigger_topology",
        lambda *args, **kwargs: healthy,
    )
    return healthy
