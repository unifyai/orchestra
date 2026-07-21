"""Unit tests for external field binding hydrate planner (no DB)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from orchestra.external_bindings.planner import (
    HydrateMode,
    compute_input_hash,
    hydrate_logs,
    sidecar_key,
)
from orchestra.external_bindings.registry import register_connector
from orchestra.external_bindings.types import BindingResult, WriteResult


class _CountingConnector:
    id = "test.counting"

    def __init__(self):
        self.calls = 0
        self.items_seen: list[int] = []

    def batch_fetch(self, *, binding, items, auth):
        self.calls += 1
        self.items_seen.extend(i.log_event_id for i in items)
        return [
            BindingResult(log_event_id=i.log_event_id, value=f"v-{i.inputs.get('x')}")
            for i in items
        ]


@pytest.fixture
def counting_connector():
    c = _CountingConnector()
    register_connector(c)
    return c


def test_compute_input_hash_stable():
    a = compute_input_hash(
        binding_version=1,
        connector_id="http.generic",
        inputs={"a": 1, "b": "x"},
    )
    b = compute_input_hash(
        binding_version=1,
        connector_id="http.generic",
        inputs={"b": "x", "a": 1},
    )
    assert a == b
    c = compute_input_hash(
        binding_version=2,
        connector_id="http.generic",
        inputs={"a": 1, "b": "x"},
    )
    assert a != c


def test_hydrate_batches_once(counting_connector):
    logs = [
        {"id": 1, "entries": {"x": 10}, "derived_entries": {}},
        {"id": 2, "entries": {"x": 20}, "derived_entries": {}},
        {"id": 3, "entries": {"x": 30}, "derived_entries": {}},
    ]
    bindings = [
        {
            "field_name": "remote",
            "connector_id": "test.counting",
            "binding_version": 1,
            "is_active": True,
            "binding": {
                "inputs": [{"name": "x", "column": "x"}],
                "cache": {"ttl_seconds": 300},
                "on_error": "fail",
                "batch": {"max": 100},
            },
        },
    ]
    out, ops = hydrate_logs(logs, bindings=bindings, mode=HydrateMode.FORCE)
    assert counting_connector.calls == 1
    assert sorted(counting_connector.items_seen) == [1, 2, 3]
    assert out[0]["external_entries"]["remote"] == "v-10"
    assert out[1]["external_entries"]["remote"] == "v-20"
    assert len(ops) == 6  # value + sidecar per row


def test_hash_hit_skips_http(counting_connector):
    digest = compute_input_hash(
        binding_version=1,
        connector_id="test.counting",
        inputs={"x": 10},
    )
    now = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    logs = [{"id": 1, "entries": {"x": 10}, "derived_entries": {}}]
    raw = {
        1: {
            "x": 10,
            "remote": "cached",
            sidecar_key("remote"): {"hash": digest, "fetched_at": now, "token": None},
        },
    }
    bindings = [
        {
            "field_name": "remote",
            "connector_id": "test.counting",
            "binding_version": 1,
            "is_active": True,
            "binding": {
                "inputs": [{"name": "x", "column": "x"}],
                "cache": {"ttl_seconds": 300},
                "on_error": "fail",
            },
        },
    ]
    out, ops = hydrate_logs(
        logs,
        bindings=bindings,
        mode=HydrateMode.STALE_OK,
        raw_data_by_id=raw,
    )
    assert counting_connector.calls == 0
    assert out[0]["external_entries"]["remote"] == "cached"
    assert ops == []


def test_ttl_expiry_refetches(counting_connector):
    digest = compute_input_hash(
        binding_version=1,
        connector_id="test.counting",
        inputs={"x": 10},
    )
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0)
    old_iso = old.isoformat().replace("+00:00", "Z")
    logs = [{"id": 1, "entries": {"x": 10}, "derived_entries": {}}]
    raw = {
        1: {
            "x": 10,
            "remote": "stale",
            sidecar_key("remote"): {"hash": digest, "fetched_at": old_iso},
        },
    }
    bindings = [
        {
            "field_name": "remote",
            "connector_id": "test.counting",
            "binding_version": 1,
            "is_active": True,
            "binding": {
                "inputs": [{"name": "x", "column": "x"}],
                "cache": {"ttl_seconds": 60},
                "on_error": "fail",
            },
        },
    ]
    out, _ops = hydrate_logs(
        logs,
        bindings=bindings,
        mode=HydrateMode.STALE_OK,
        raw_data_by_id=raw,
    )
    assert counting_connector.calls == 1
    assert out[0]["external_entries"]["remote"] == "v-10"


def test_on_error_null(counting_connector):
    class _FailConnector:
        id = "test.fail"

        def batch_fetch(self, *, binding, items, auth):
            return [
                BindingResult(log_event_id=i.log_event_id, error="boom") for i in items
            ]

    register_connector(_FailConnector())
    logs = [{"id": 1, "entries": {"x": 1}, "derived_entries": {}}]
    bindings = [
        {
            "field_name": "remote",
            "connector_id": "test.fail",
            "binding_version": 1,
            "is_active": True,
            "binding": {
                "inputs": [{"name": "x", "column": "x"}],
                "on_error": "null",
                "cache": {"ttl_seconds": 300},
            },
        },
    ]
    out, ops = hydrate_logs(logs, bindings=bindings, mode=HydrateMode.FORCE)
    assert out[0]["external_entries"]["remote"] is None
    assert any(o["key"] == "remote" and o["value"] is None for o in ops)


def test_on_error_fail_raises():
    class _FailConnector:
        id = "test.fail2"

        def batch_fetch(self, *, binding, items, auth):
            return [
                BindingResult(log_event_id=i.log_event_id, error="boom") for i in items
            ]

    register_connector(_FailConnector())
    logs = [{"id": 1, "entries": {"x": 1}, "derived_entries": {}}]
    bindings = [
        {
            "field_name": "remote",
            "connector_id": "test.fail2",
            "binding_version": 1,
            "is_active": True,
            "binding": {
                "inputs": [{"name": "x", "column": "x"}],
                "on_error": "fail",
            },
        },
    ]
    with pytest.raises(RuntimeError, match="External hydrate failed"):
        hydrate_logs(logs, bindings=bindings, mode=HydrateMode.FORCE)


def test_hydrate_none_noop(counting_connector):
    logs = [{"id": 1, "entries": {"x": 1}, "derived_entries": {}}]
    bindings = [
        {
            "field_name": "remote",
            "connector_id": "test.counting",
            "binding_version": 1,
            "is_active": True,
            "binding": {"inputs": [{"name": "x", "column": "x"}]},
        },
    ]
    out, ops = hydrate_logs(logs, bindings=bindings, mode=HydrateMode.NONE)
    assert counting_connector.calls == 0
    assert ops == []
    assert out[0].get("external_entries", {}) == {} or "remote" not in out[0].get(
        "external_entries",
        {},
    )


class _WriteConnector:
    id = "test.write"

    def __init__(self):
        self.writes = []

    def batch_fetch(self, *, binding, items, auth):
        return [
            BindingResult(log_event_id=i.log_event_id, value="ignored") for i in items
        ]

    def execute_write(self, *, binding, payload, idempotency_key, auth):
        self.writes.append(
            {
                "payload": payload,
                "idempotency_key": idempotency_key,
                "binding": binding,
            },
        )
        if payload.get("fail"):
            return WriteResult(ok=False, error="forced failure")
        return WriteResult(
            ok=True,
            response={"ok": True, "id": "ext-1"},
            external_token="tok-1",
        )


def test_enqueue_sync_write_confirms(monkeypatch):
    from orchestra.external_bindings import write as write_mod
    from orchestra.external_bindings.registry import register_connector

    connector = _WriteConnector()
    register_connector(connector)

    class _Intent:
        def __init__(self):
            self.id = 1
            self.status = "pending"
            self.project_id = 1
            self.context_id = 2
            self.connector_id = "test.write"
            self.binding = {
                "write": {"url_template": "https://example.com/{id}"},
            }
            self.payload = {"id": "abc"}
            self.idempotency_key = "k1"
            self.field_name = "remote"
            self.log_event_ids = [10]
            self.attempts = 0
            self.last_error = None
            self.result = None
            self.created_at = None
            self.confirmed_at = None

    intent = _Intent()
    store = {"intent": intent}

    class _FakeDAO:
        def __init__(self, session):
            pass

        def create(self, **kwargs):
            return store["intent"]

        def get(self, intent_id):
            return store["intent"]

        def get_by_idempotency(self, **kwargs):
            return None

        def mark_in_progress(self, row):
            row.status = "in_progress"
            row.attempts += 1

        def mark_confirmed(self, row, result):
            row.status = "confirmed"
            row.result = result

        def mark_failed(self, row, error):
            row.status = "failed"
            row.last_error = error

        def list_pending(self, *, limit=50):
            return [store["intent"]] if store["intent"].status == "pending" else []

    invalidations = []

    monkeypatch.setattr(write_mod, "ExternalWriteIntentDAO", _FakeDAO)
    monkeypatch.setattr(
        write_mod,
        "_invalidate_hydrate_cache",
        lambda *a, **k: invalidations.append(k),
    )

    class _Session:
        def commit(self):
            pass

    out = write_mod.enqueue_external_write(
        _Session(),
        project_id=1,
        context_id=2,
        connector_id="test.write",
        payload={"id": "abc"},
        idempotency_key="k1",
        binding={"write": {"url_template": "https://example.com/{id}"}},
        field_name="remote",
        log_event_ids=[10],
        deliver="sync",
    )
    assert out["status"] == "confirmed"
    assert len(connector.writes) == 1
    assert connector.writes[0]["idempotency_key"] == "k1"
    assert invalidations and invalidations[0]["log_event_ids"] == [10]


def test_enqueue_async_stays_pending(monkeypatch):
    from orchestra.external_bindings import write as write_mod

    connector = _WriteConnector()
    register_connector(connector)

    class _Intent:
        def __init__(self):
            self.id = 2
            self.status = "pending"
            self.connector_id = "test.write"
            self.binding = {}
            self.payload = {"id": "x"}
            self.idempotency_key = "k2"
            self.field_name = None
            self.log_event_ids = []
            self.attempts = 0
            self.last_error = None
            self.result = None
            self.created_at = None
            self.confirmed_at = None

    intent = _Intent()

    class _FakeDAO:
        def __init__(self, session):
            pass

        def create(self, **kwargs):
            return intent

        def get(self, intent_id):
            return intent

    monkeypatch.setattr(write_mod, "ExternalWriteIntentDAO", _FakeDAO)

    class _Session:
        def commit(self):
            pass

    out = write_mod.enqueue_external_write(
        _Session(),
        project_id=1,
        context_id=2,
        connector_id="test.write",
        payload={"id": "x"},
        idempotency_key="k2",
        binding={"write": {"url_template": "https://example.com/{id}"}},
        deliver="async",
    )
    assert out["status"] == "pending"
    assert connector.writes == []


def test_deliver_write_failure(monkeypatch):
    from orchestra.external_bindings import write as write_mod

    connector = _WriteConnector()
    register_connector(connector)

    class _Intent:
        def __init__(self):
            self.id = 3
            self.status = "pending"
            self.connector_id = "test.write"
            self.binding = {"write": {"url_template": "https://example.com/{id}"}}
            self.payload = {"id": "z", "fail": True}
            self.idempotency_key = "k3"
            self.field_name = None
            self.log_event_ids = []
            self.attempts = 0
            self.last_error = None
            self.result = None
            self.created_at = None
            self.confirmed_at = None

    intent = _Intent()

    class _FakeDAO:
        def __init__(self, session):
            pass

        def get(self, intent_id):
            return intent

        def mark_in_progress(self, row):
            row.status = "in_progress"
            row.attempts += 1

        def mark_confirmed(self, row, result):
            row.status = "confirmed"
            row.result = result

        def mark_failed(self, row, error):
            row.status = "failed"
            row.last_error = error

    monkeypatch.setattr(write_mod, "ExternalWriteIntentDAO", _FakeDAO)

    class _Session:
        def commit(self):
            pass

    out = write_mod.deliver_intent(_Session(), 3)
    assert out["status"] == "failed"
    assert "forced failure" in (out["last_error"] or "")
