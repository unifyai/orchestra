"""Tests for Orchestra provider transient classification and retry."""

from __future__ import annotations

from types import SimpleNamespace

from orchestra.integrations.transient import (
    call_with_transient_retries,
    is_transient_message,
    is_transient_provider_error,
    should_retry_adapter_result,
)


def test_classifies_rate_limit_and_platform_messages() -> None:
    assert is_transient_message("API rate limit exceeded")
    assert is_transient_message(
        "Something went wrong while executing your query on 2026-07-15T15:43:19Z",
    )
    assert is_transient_message("Expecting value: line 1 column 1 (char 0)")
    assert not is_transient_message("Could not resolve to a User with the login")


def test_provider_error_http_codes() -> None:
    assert is_transient_provider_error(
        {"code": "provider_error", "provider_status_code": 429, "message": "slow down"},
    )
    assert is_transient_provider_error(
        {
            "code": "provider_error",
            "provider_status_code": 502,
            "message": "bad gateway",
        },
    )
    assert not is_transient_provider_error(
        {"code": "connect_required", "message": "Connect github first"},
    )
    assert not is_transient_provider_error(
        {
            "code": "provider_error",
            "provider_status_code": 401,
            "message": "unauthorized",
        },
    )


def test_retry_ok_result_only_for_read_graphql_platform() -> None:
    result = {
        "errors": [
            {
                "message": (
                    "Something went wrong while executing your query "
                    "on 2026-07-15T15:43:19Z"
                ),
            },
        ],
    }
    retry, reason = should_retry_adapter_result(
        action_class="read",
        status="ok",
        error=None,
        result=result,
    )
    assert retry is True
    assert reason == "transient_graphql_platform"

    retry_write, _ = should_retry_adapter_result(
        action_class="write",
        status="ok",
        error=None,
        result=result,
    )
    assert retry_write is False

    retry_not_found, _ = should_retry_adapter_result(
        action_class="read",
        status="ok",
        error=None,
        result={"errors": [{"type": "NOT_FOUND", "message": "missing"}]},
    )
    assert retry_not_found is False


def test_call_with_transient_retries_succeeds_after_blip() -> None:
    calls = {"n": 0}

    def _fn():
        calls["n"] += 1
        if calls["n"] < 3:
            return SimpleNamespace(
                status="error",
                error={
                    "code": "provider_error",
                    "provider_status_code": 503,
                    "message": "unavailable",
                },
                result={},
            )
        return SimpleNamespace(status="ok", error=None, result={"data": {"ok": True}})

    def _should(value):
        return should_retry_adapter_result(
            action_class="read",
            status=value.status,
            error=value.error,
            result=value.result,
        )

    value, attempts = call_with_transient_retries(
        _fn,
        should_retry=_should,
        max_attempts=5,
        sleep=lambda _: None,
    )
    assert value.status == "ok"
    assert attempts == 3
    assert calls["n"] == 3
