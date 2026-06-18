from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestra.lib.deploy_env import env_suffix, resolve_deploy_env
from orchestra.web.api.utils import assistant_infra


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DEPLOY_ENV", "ORCHESTRA_URL"):
        monkeypatch.delenv(name, raising=False)


def test_resolve_deploy_env_uses_deploy_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("DEPLOY_ENV", "staging")
    monkeypatch.setenv("ORCHESTRA_URL", "https://api.unify.ai/v0")

    assert resolve_deploy_env() == "staging"
    assert env_suffix() == "-staging"


def test_resolve_deploy_env_explicit_production_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("DEPLOY_ENV", "production")
    monkeypatch.setenv("ORCHESTRA_URL", "https://internal.example.com/v0")

    assert resolve_deploy_env() == "production"
    assert env_suffix() == ""


def test_resolve_deploy_env_staging_orchestra_url_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("ORCHESTRA_URL", "https://internal.example.com/v0")

    assert resolve_deploy_env() == "staging"
    assert env_suffix() == "-staging"


def test_resolve_deploy_env_defaults_to_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)

    assert resolve_deploy_env() == "production"
    assert env_suffix() == ""


def test_comms_url_falls_back_to_communication_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(assistant_infra, "COMMS_URL", None)
    monkeypatch.setattr(
        assistant_infra,
        "COMMUNICATION_URL",
        "https://comms.staging.test/",
    )
    monkeypatch.setattr(assistant_infra, "COMMS_URL_LEGACY", None)

    assert assistant_infra._comms_url() == "https://comms.staging.test"


def test_comms_url_prefers_droid_comms_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(assistant_infra, "COMMS_URL", "https://comms.primary.test")
    monkeypatch.setattr(
        assistant_infra,
        "COMMUNICATION_URL",
        "https://comms.legacy.test",
    )
    monkeypatch.setattr(assistant_infra, "COMMS_URL_LEGACY", "https://comms.older.test")

    assert assistant_infra._comms_url() == "https://comms.primary.test"


@pytest.mark.anyio
async def test_create_pubsub_topic_skips_comms_in_self_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("SELF_HOST", "1")
    monkeypatch.setenv("DEPLOY_ENV", "staging")
    post = AsyncMock()
    monkeypatch.setattr(
        assistant_infra,
        "get_async_client",
        lambda: SimpleNamespace(post=post),
    )

    result = await assistant_infra.create_pubsub_topic("2101")

    assert result == {
        "success": True,
        "skipped": True,
        "reason": "self_host_local_provisioning",
        "topic_name": "droid-2101-staging",
    }
    post.assert_not_called()


@pytest.mark.anyio
async def test_create_pubsub_topic_uses_staging_topic_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("DEPLOY_ENV", "staging")
    post = AsyncMock(return_value=SimpleNamespace(json=lambda: {"success": True}))
    monkeypatch.setattr(
        assistant_infra,
        "get_async_client",
        lambda: SimpleNamespace(post=post),
    )
    monkeypatch.setattr(assistant_infra, "COMMS_URL", "https://comms.test")
    monkeypatch.setattr(assistant_infra, "ADMIN_KEY", "admin-key")

    result = await assistant_infra.create_pubsub_topic("2101")

    assert result == {"success": True}
    post.assert_awaited_once_with(
        "https://comms.test/infra/pubsub/topic",
        headers={"Authorization": "Bearer admin-key"},
        data={"topic_name": "droid-2101-staging"},
        timeout=30,
    )


@pytest.mark.anyio
async def test_delete_pubsub_topic_uses_staging_topic_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("ORCHESTRA_URL", "https://internal.example.com/v0")
    request_cleanup = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(assistant_infra, "_request_cleanup_step", request_cleanup)

    result = await assistant_infra.delete_pubsub_topic("2101")

    assert result == {"success": True}
    request_cleanup.assert_awaited_once_with(
        name="delete_pubsub_topic",
        method="DELETE",
        path="/infra/pubsub/topic",
        data={"topic_name": "droid-2101-staging"},
    )


@pytest.mark.anyio
async def test_create_pubsub_topic_unsuffixed_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("ORCHESTRA_URL", "https://api.unify.ai/v0")
    post = AsyncMock(return_value=SimpleNamespace(json=lambda: {"success": True}))
    monkeypatch.setattr(
        assistant_infra,
        "get_async_client",
        lambda: SimpleNamespace(post=post),
    )
    monkeypatch.setattr(assistant_infra, "COMMS_URL", "https://comms.test")
    monkeypatch.setattr(assistant_infra, "ADMIN_KEY", "admin-key")

    await assistant_infra.create_pubsub_topic("2101")

    assert post.await_args.kwargs["data"] == {"topic_name": "droid-2101"}


def test_settings_is_staging_follows_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orchestra.settings import settings

    _clear_env(monkeypatch)
    monkeypatch.setenv("ORCHESTRA_URL", "https://internal.example.com/v0")
    assert settings.is_staging is True

    monkeypatch.setenv("ORCHESTRA_URL", "https://api.unify.ai/v0")
    assert settings.is_staging is False
