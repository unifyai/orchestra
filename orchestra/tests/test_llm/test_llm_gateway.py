"""Tests for the server-side LLM gateway (`/v0/llm/*`).

Pure-helper tests cover model normalisation, cost extraction, stream-tail
parsing, and payload shaping. Endpoint tests drive the real route with the
provider HTTP client and billing lookup monkeypatched, so metering, the
balance gate, the spending-cap gate, and streaming are exercised end-to-end
without a live provider or seeded-balance coupling.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from orchestra.settings import settings
from orchestra.tests.utils import HEADERS
from orchestra.web.api.llm import views

# --------------------------------------------------------------------------- #
# Pure-helper unit tests
# --------------------------------------------------------------------------- #


class TestNormalizeModel:
    def test_strips_openrouter_suffix(self):
        assert views._normalize_model("openai/gpt-5.6-sol@openrouter") == (
            "openai/gpt-5.6-sol"
        )

    def test_passthrough_bare_id(self):
        assert views._normalize_model("openai/gpt-5.6-sol") == "openai/gpt-5.6-sol"

    def test_rejects_other_provider(self):
        with pytest.raises(HTTPException) as exc:
            views._normalize_model("claude-4.5-sonnet@vertex-ai")
        assert exc.value.status_code == 400
        assert "vertex-ai" in exc.value.detail


class TestUsageCost:
    def test_reads_cost(self):
        assert views._usage_cost({"cost": 0.42}) == pytest.approx(0.42)

    def test_missing_cost_returns_none(self):
        assert views._usage_cost({"prompt_tokens": 10}) is None

    def test_none_and_negative(self):
        assert views._usage_cost(None) is None
        assert views._usage_cost({"cost": -1}) is None

    def test_non_numeric(self):
        assert views._usage_cost({"cost": "abc"}) is None


class TestCostFromStreamTail:
    def test_parses_terminal_usage_chunk(self):
        tail = (
            'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            'data: {"choices":[],"usage":{"cost":0.75}}\n\n'
            "data: [DONE]\n\n"
        )
        assert views._cost_from_stream_tail(tail) == pytest.approx(0.75)

    def test_no_usage_returns_none(self):
        tail = 'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n'
        assert views._cost_from_stream_tail(tail) is None


class TestBuildPayload:
    def test_forces_usage_accounting_and_normalises_model(self):
        from orchestra.web.api.llm.schema import ChatCompletionRequest

        body = ChatCompletionRequest(
            model="openai/gpt-5.6-sol@openrouter",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.2,
        )
        payload = views._build_payload(body)
        assert payload["model"] == "openai/gpt-5.6-sol"
        assert payload["usage"] == {"include": True}
        assert payload["temperature"] == 0.2
        assert "assistant_id" not in payload
        assert "stream_options" not in payload  # non-stream

    def test_stream_sets_include_usage(self):
        from orchestra.web.api.llm.schema import ChatCompletionRequest

        body = ChatCompletionRequest(
            model="openai/gpt-5.6-sol",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
        )
        payload = views._build_payload(body)
        assert payload["stream"] is True
        assert payload["stream_options"]["include_usage"] is True


# --------------------------------------------------------------------------- #
# Endpoint test fakes + fixtures
# --------------------------------------------------------------------------- #


class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.content = json.dumps(self._payload).encode()

    def json(self):
        return self._payload


class _FakeStreamCtx:
    def __init__(self, status_code, chunks):
        self.status_code = status_code
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    async def aread(self):
        return b"".join(self._chunks)


class _FakeClient:
    def __init__(self, post_resp=None, stream_ctx=None, get_resp=None):
        self._post_resp = post_resp
        self._stream_ctx = stream_ctx
        self._get_resp = get_resp
        self.calls = []

    async def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(("post", url, json))
        return self._post_resp

    def stream(self, method, url, headers=None, json=None, timeout=None):
        self.calls.append(("stream", url, json))
        return self._stream_ctx

    async def get(self, url, headers=None, timeout=None):
        self.calls.append(("get", url, None))
        return self._get_resp


def _fake_entity(credits=100.0, mode="CREDITS", ba_id=1):
    return SimpleNamespace(
        billing_account_id=ba_id,
        billing_mode=SimpleNamespace(value=mode),
        credits=credits,
    )


@pytest.fixture
def deduct_calls(monkeypatch):
    """Record ledger deductions instead of writing them."""
    calls: list[dict] = []

    class _RecorderDAO:
        def __init__(self, session):
            pass

        def deduct_credits(self, billing_account_id, quantity, **kwargs):
            calls.append(
                {
                    "billing_account_id": billing_account_id,
                    "quantity": quantity,
                    **kwargs,
                },
            )
            return None

    monkeypatch.setattr(views, "BillingAccountDAO", _RecorderDAO)
    return calls


@pytest.fixture(autouse=True)
def _charging_on(monkeypatch):
    """Force a charging deployment and a configured provider key."""
    monkeypatch.setattr(type(settings), "charges_billing", property(lambda self: True))
    monkeypatch.setattr(settings, "openrouter_api_key", "test-key", raising=False)


# --------------------------------------------------------------------------- #
# Endpoint tests
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_chat_completions_meters_and_deducts(client, monkeypatch, deduct_calls):
    monkeypatch.setattr(views, "get_billing_entity", lambda *a, **k: _fake_entity())
    provider = _FakeClient(
        post_resp=_FakeResp(
            200,
            {
                "id": "cmpl-1",
                "choices": [{"message": {"content": "hello"}}],
                "usage": {"cost": 0.5},
            },
        ),
    )
    monkeypatch.setattr(views, "get_async_client", lambda: provider)

    resp = await client.post(
        "/v0/llm/chat/completions",
        headers=HEADERS,
        json={
            "model": "openai/gpt-5.6-sol@openrouter",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )

    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "hello"
    # Provider was called with the normalised model and forced usage accounting.
    _, url, sent = provider.calls[0]
    assert url.endswith("/chat/completions")
    assert sent["model"] == "openai/gpt-5.6-sol"
    assert sent["usage"] == {"include": True}
    # Cost metered with the platform markup.
    assert len(deduct_calls) == 1
    assert deduct_calls[0]["quantity"] == pytest.approx(
        0.5 * settings.chat_completions_markup_rate,
    )
    assert deduct_calls[0]["category"] == "llm"


@pytest.mark.anyio
async def test_insufficient_credits_blocks_before_provider(
    client,
    monkeypatch,
    deduct_calls,
):
    monkeypatch.setattr(
        views,
        "get_billing_entity",
        lambda *a, **k: _fake_entity(credits=0.0),
    )
    provider = _FakeClient(post_resp=_FakeResp(200, {}))
    monkeypatch.setattr(views, "get_async_client", lambda: provider)

    resp = await client.post(
        "/v0/llm/chat/completions",
        headers=HEADERS,
        json={
            "model": "openai/gpt-5.6-sol@openrouter",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )

    assert resp.status_code == 402
    assert provider.calls == []  # provider never touched
    assert deduct_calls == []


@pytest.mark.anyio
async def test_spending_cap_blocks_before_provider(client, monkeypatch, deduct_calls):
    monkeypatch.setattr(views, "get_billing_entity", lambda *a, **k: _fake_entity())
    # Personal context (HEADERS user has no org) -> UserDAO caps apply.
    from orchestra.db.dao.user_dao import UserDAO

    monkeypatch.setattr(UserDAO, "get_spending_cap", lambda self, uid: 1.0)
    monkeypatch.setattr(UserDAO, "get_cumulative_spend", lambda self, uid, month: 5.0)

    provider = _FakeClient(post_resp=_FakeResp(200, {}))
    monkeypatch.setattr(views, "get_async_client", lambda: provider)

    resp = await client.post(
        "/v0/llm/chat/completions",
        headers=HEADERS,
        json={
            "model": "openai/gpt-5.6-sol@openrouter",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )

    assert resp.status_code == 429
    assert provider.calls == []
    assert deduct_calls == []


@pytest.mark.anyio
async def test_non_openrouter_model_rejected(client, monkeypatch, deduct_calls):
    monkeypatch.setattr(views, "get_billing_entity", lambda *a, **k: _fake_entity())
    provider = _FakeClient(post_resp=_FakeResp(200, {}))
    monkeypatch.setattr(views, "get_async_client", lambda: provider)

    resp = await client.post(
        "/v0/llm/chat/completions",
        headers=HEADERS,
        json={
            "model": "claude-4.5-sonnet@vertex-ai",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )

    assert resp.status_code == 400
    assert provider.calls == []
    assert deduct_calls == []


@pytest.mark.anyio
async def test_streaming_passthrough_and_bills(client, monkeypatch, deduct_calls):
    monkeypatch.setattr(views, "get_billing_entity", lambda *a, **k: _fake_entity())
    chunks = [
        b'data: {"choices":[{"delta":{"content":"he"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n',
        b'data: {"choices":[],"usage":{"cost":0.25}}\n\n',
        b"data: [DONE]\n\n",
    ]
    provider = _FakeClient(stream_ctx=_FakeStreamCtx(200, chunks))
    monkeypatch.setattr(views, "get_async_client", lambda: provider)

    resp = await client.post(
        "/v0/llm/chat/completions",
        headers=HEADERS,
        json={
            "model": "openai/gpt-5.6-sol@openrouter",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )

    assert resp.status_code == 200
    body = resp.text
    assert "hello".replace("l", "l") in body or "llo" in body
    assert "[DONE]" in body
    # Terminal usage chunk metered with markup.
    assert len(deduct_calls) == 1
    assert deduct_calls[0]["quantity"] == pytest.approx(
        0.25 * settings.chat_completions_markup_rate,
    )


@pytest.mark.anyio
async def test_models_endpoint_proxies(client, monkeypatch):
    provider = _FakeClient(
        get_resp=_FakeResp(200, {"data": [{"id": "openai/gpt-5.6-sol"}]}),
    )
    monkeypatch.setattr(views, "get_async_client", lambda: provider)

    resp = await client.get("/v0/llm/models", headers=HEADERS)

    assert resp.status_code == 200
    assert resp.json()["data"][0]["id"] == "openai/gpt-5.6-sol"
    assert provider.calls[0][0] == "get"


# --------------------------------------------------------------------------- #
# Native-Anthropic leg
# --------------------------------------------------------------------------- #


class TestAnthropicPricing:
    """Anthropic bills in tokens and never in money, so we price it ourselves.

    That makes the rate table load-bearing in a way the OpenRouter leg's is
    not: there, a wrong number is a reporting error; here it is the charge.
    """

    def test_rates_come_from_the_curated_catalogue(self):
        assert views._anthropic_token_rates("claude-opus-5") == (5.0, 25.0)

    def test_the_endpoint_suffixed_form_resolves_too(self):
        """unillm speaks ``model@provider``; Anthropic is sent the bare id."""
        assert views._anthropic_token_rates("claude-opus-5@anthropic") == (5.0, 25.0)

    def test_an_unpriceable_model_resolves_to_nothing(self):
        assert views._anthropic_token_rates("gpt-4o") is None

    def test_cost_is_tokens_at_the_catalogue_rate(self):
        rates = (5.0, 25.0)
        cost = views._anthropic_usage_cost(
            {"input_tokens": 1000, "output_tokens": 500},
            rates,
        )
        assert cost == pytest.approx(1000 * 5.0 / 1e6 + 500 * 25.0 / 1e6)

    def test_cache_tokens_are_charged_rather_than_dropped(self):
        """Ignoring them would bill a cached call as though it were free."""
        rates = (5.0, 25.0)
        plain = views._anthropic_usage_cost({"input_tokens": 1000}, rates)
        cached = views._anthropic_usage_cost(
            {
                "input_tokens": 1000,
                "cache_read_input_tokens": 4000,
                "cache_creation_input_tokens": 1000,
            },
            rates,
        )
        assert cached > plain

    def test_usage_without_token_counts_prices_nothing(self):
        """Better no row than a zero-cost row implying the call was free."""
        assert views._anthropic_usage_cost({}, (5.0, 25.0)) is None


class TestAnthropicStreamUsage:
    """Anthropic splits usage across two events; the tail must merge them."""

    def test_input_and_output_counts_are_merged_across_events(self):
        tail = (
            'data: {"type":"message_start","message":{"usage":'
            '{"input_tokens":1000}}}\n\n'
            'data: {"type":"message_delta","usage":{"output_tokens":500}}\n\n'
        )
        assert views._anthropic_cost_from_stream_tail(tail, (5.0, 25.0)) == (
            pytest.approx(0.0175)
        )

    def test_a_stream_carrying_no_usage_prices_nothing(self):
        tail = 'data: {"type":"content_block_delta"}\n\n'
        assert views._anthropic_cost_from_stream_tail(tail, (5.0, 25.0)) is None


# --------------------------------------------------------------------------- #
# Metering API (used by the pod-local broker, which holds the provider key)
# --------------------------------------------------------------------------- #


class TestMeterability:
    """What can be charged decides what may be run.

    A caller streaming bytes itself can only be allowed to do so for a call
    this side could later price. Authorising something unpriceable would
    hand out inference nobody can bill.
    """

    def test_openrouter_models_are_always_meterable(self):
        """They report their own cost, so any of them can be settled."""
        assert views._is_meterable("openai/gpt-5.6-sol@openrouter") is True
        assert views._is_meterable("openrouter/openai/gpt-5.6-sol") is True

    def test_a_catalogued_anthropic_model_is_meterable(self):
        assert views._is_meterable("claude-opus-5@anthropic") is True

    def test_an_uncatalogued_native_model_is_not(self):
        assert views._is_meterable("mystery-model@vertex-ai") is False


class TestPriceUsageForEitherProvider:
    """One entry point, because the caller reports and never computes.

    Leaving the pricing decision here is what keeps refusing an unpriceable
    model meaningful: a caller that priced its own calls could simply
    declare a number.
    """

    def test_an_openrouter_cost_is_taken_as_authoritative(self):
        assert views._price_usage_for("openai/x@openrouter", {"cost": 0.000225}) == (
            pytest.approx(0.000225)
        )

    def test_anthropic_tokens_are_priced_from_the_catalogue(self):
        cost = views._price_usage_for(
            "claude-opus-5",
            {"input_tokens": 1000, "output_tokens": 500},
        )
        assert cost == pytest.approx(0.0175)

    def test_usage_for_an_unpriceable_model_yields_nothing(self):
        """Better no charge than an invented one; authorize refuses these."""
        assert views._price_usage_for("mystery@vertex-ai", {"input_tokens": 10}) is None
