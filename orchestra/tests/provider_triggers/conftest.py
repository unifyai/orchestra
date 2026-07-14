"""Shared fixtures for provider-trigger tests."""

from __future__ import annotations

from pathlib import Path

import pytest

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
