"""Correctness tests for the POST /v0/logs/fields backfill path.

After the single-UPDATE refactor (one ``UPDATE log_event SET data = template ||
COALESCE(data, '{}'::jsonb) FROM log_event_context ... WHERE NOT (data ?&
names)``), these cases lock down the invariants that matter to production:

* Existing non-null values are never overwritten by the null template
  (right-biased merge: ``template || data``).
* Rows that already have every requested key are skipped by the ``?&`` guard
  (zero writes, zero WAL, ``backfilled_count == 0``).
* Empty ``fields`` dict short-circuits before any DB work.
* Heterogeneous rows (different subsets of missing keys) all converge to the
  full requested schema in a single statement.
* ``backfilled_count`` in the response is the number of **log_event rows**
  the UPDATE touched (``result.rowcount``), **not** the old ``(row × missing
  field)`` pair count that the pre-refactor Python pipeline returned.

These complement ``test_create_fields*backfill*`` in ``test_log_fields.py``;
they intentionally exercise edge shapes the original tests did not (empty
fields dict, idempotent second call, mixed heterogeneous rows, and
preservation across a batch of rows).
"""

import pytest
from httpx import AsyncClient

from . import HEADERS, _create_log, _create_project


@pytest.mark.anyio
async def test_backfill_no_op_when_all_rows_already_have_every_field(
    client: AsyncClient,
):
    """Second call with the same fields must short-circuit via the ?& guard.

    Production hot path: clients re-POST the same field set repeatedly.
    The new UPDATE must not rewrite rows whose data already contains every
    requested key — ``backfilled_count`` should drop to 0 on the second call
    and no row's data should change.
    """
    project_name = "test-backfill-idempotent"
    await _create_project(client, project_name)

    for value in ("a", "b"):
        resp = await _create_log(
            client,
            project_name,
            entries={
                "existing_field": value,
                "explicit_types": {
                    "existing_field": {"type": "str", "mutable": True},
                },
            },
        )
        assert resp.status_code == 200, resp.json()

    payload = {
        "project_name": project_name,
        "fields": {"alpha": "str", "beta": "int"},
    }

    first = await client.post("/v0/logs/fields", json=payload, headers=HEADERS)
    assert first.status_code == 200, first.json()
    assert first.json()["backfilled_count"] == 2

    second = await client.post("/v0/logs/fields", json=payload, headers=HEADERS)
    assert second.status_code == 200, second.json()
    assert second.json()["backfilled_count"] == 0, (
        "Second call must be a no-op: every row already has every requested key, "
        "so the ?& guard should filter them all out before any write happens."
    )

    logs_resp = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert logs_resp.status_code == 200
    logs = logs_resp.json()["logs"]
    assert len(logs) == 2
    for log in logs:
        assert log["entries"]["alpha"] is None
        assert log["entries"]["beta"] is None
        assert log["entries"]["existing_field"] in {"a", "b"}


@pytest.mark.anyio
async def test_backfill_heterogeneous_rows_converge_to_full_schema(
    client: AsyncClient,
):
    """Rows missing different subsets of the requested keys all converge.

    Row A has {X}, row B has {Y}; requesting {X, Y} must leave both rows with
    both keys and must not overwrite X on row A or Y on row B. This is the
    invariant that makes the ``template || data`` right-biased merge safe.
    """
    project_name = "test-backfill-heterogeneous"
    await _create_project(client, project_name)

    resp_a = await _create_log(
        client,
        project_name,
        entries={
            "X": "kept-on-A",
            "explicit_types": {"X": {"type": "str", "mutable": True}},
        },
    )
    assert resp_a.status_code == 200, resp_a.json()
    log_a = resp_a.json()["log_event_ids"][0]

    resp_b = await _create_log(
        client,
        project_name,
        entries={
            "Y": "kept-on-B",
            "explicit_types": {"Y": {"type": "str", "mutable": True}},
        },
    )
    assert resp_b.status_code == 200, resp_b.json()
    log_b = resp_b.json()["log_event_ids"][0]

    fields_resp = await client.post(
        "/v0/logs/fields",
        json={
            "project_name": project_name,
            "fields": {"X": "str", "Y": "str"},
        },
        headers=HEADERS,
    )
    assert fields_resp.status_code == 200, fields_resp.json()
    assert fields_resp.json()["backfilled_count"] == 2

    logs_resp = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert logs_resp.status_code == 200
    logs = {log["id"]: log for log in logs_resp.json()["logs"]}

    assert logs[log_a]["entries"]["X"] == "kept-on-A"
    assert logs[log_a]["entries"]["Y"] is None
    assert logs[log_b]["entries"]["X"] is None
    assert logs[log_b]["entries"]["Y"] == "kept-on-B"


@pytest.mark.anyio
async def test_backfill_partial_overlap_preserves_existing_and_fills_missing(
    client: AsyncClient,
):
    """Request {X, Y} on a row with {X:"real"} must preserve X and add Y as null.

    Only one row in the context, missing exactly one of the two requested
    fields. The ``?&`` "all keys present" check is false for this row, so the
    UPDATE's ``NOT (...)`` filter admits it; the merge is right-biased
    (``template || data``) so ``X`` stays as ``"real"`` and ``Y`` becomes
    ``null``. One row touched → ``backfilled_count == 1``.
    """
    project_name = "test-backfill-partial-overlap"
    await _create_project(client, project_name)

    resp = await _create_log(
        client,
        project_name,
        entries={
            "X": "real",
            "explicit_types": {"X": {"type": "str", "mutable": True}},
        },
    )
    assert resp.status_code == 200, resp.json()

    fields_resp = await client.post(
        "/v0/logs/fields",
        json={
            "project_name": project_name,
            "fields": {"X": "str", "Y": "str"},
        },
        headers=HEADERS,
    )
    assert fields_resp.status_code == 200, fields_resp.json()
    assert fields_resp.json()["backfilled_count"] == 1

    logs_resp = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert logs_resp.status_code == 200
    logs = logs_resp.json()["logs"]
    assert len(logs) == 1
    entries = logs[0]["entries"]
    assert entries["X"] == "real", (
        "Right-biased merge (template || data) must preserve existing non-null "
        "values — X stays 'real', never gets overwritten by null."
    )
    assert entries["Y"] is None


@pytest.mark.anyio
async def test_backfill_request_fields_already_all_present_is_noop(
    client: AsyncClient,
):
    """Request {X} on a row with {X:"real"} must be a total no-op.

    ``data ?& ARRAY['X']`` is true → the single UPDATE filters the row out
    entirely, ``rowcount`` is 0, and the existing value is untouched.
    """
    project_name = "test-backfill-already-present"
    await _create_project(client, project_name)

    resp = await _create_log(
        client,
        project_name,
        entries={
            "X": "real",
            "explicit_types": {"X": {"type": "str", "mutable": True}},
        },
    )
    assert resp.status_code == 200, resp.json()

    fields_resp = await client.post(
        "/v0/logs/fields",
        json={
            "project_name": project_name,
            "fields": {"X": "str"},
        },
        headers=HEADERS,
    )
    assert fields_resp.status_code == 200, fields_resp.json()
    assert fields_resp.json()["backfilled_count"] == 0

    logs_resp = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert logs_resp.status_code == 200
    logs = logs_resp.json()["logs"]
    assert len(logs) == 1
    assert logs[0]["entries"]["X"] == "real"


@pytest.mark.anyio
async def test_backfill_with_empty_fields_dict_is_noop(client: AsyncClient):
    """An empty ``fields`` dict must short-circuit before any DB work.

    The new code gates the whole UPDATE behind ``if request.backfill_logs and
    request.fields``. With no fields, we should get 0 backfilled and the
    existing log should be untouched.
    """
    project_name = "test-backfill-empty-fields"
    await _create_project(client, project_name)

    resp = await _create_log(
        client,
        project_name,
        entries={
            "existing_field": "value",
            "explicit_types": {
                "existing_field": {"type": "str", "mutable": True},
            },
        },
    )
    assert resp.status_code == 200, resp.json()

    fields_resp = await client.post(
        "/v0/logs/fields",
        json={
            "project_name": project_name,
            "fields": {},
        },
        headers=HEADERS,
    )
    assert fields_resp.status_code == 200, fields_resp.json()
    assert fields_resp.json()["backfilled_count"] == 0

    logs_resp = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert logs_resp.status_code == 200
    logs = logs_resp.json()["logs"]
    assert len(logs) == 1
    assert logs[0]["entries"] == {"existing_field": "value"}


@pytest.mark.anyio
async def test_backfill_preserves_existing_values_across_many_rows(
    client: AsyncClient,
):
    """Across a batch of rows, every pre-existing value must survive the merge.

    Seeds three rows each with a distinct value for ``X``, requests ``{X, Y}``,
    and asserts each row still has its original ``X`` value while ``Y`` is
    added as ``null``. This is the production invariant under partial overlap.
    """
    project_name = "test-backfill-preserve-many"
    await _create_project(client, project_name)

    seeded = []
    for value in ("alpha", "beta", "gamma"):
        resp = await _create_log(
            client,
            project_name,
            entries={
                "X": value,
                "explicit_types": {"X": {"type": "str", "mutable": True}},
            },
        )
        assert resp.status_code == 200, resp.json()
        seeded.append((resp.json()["log_event_ids"][0], value))

    fields_resp = await client.post(
        "/v0/logs/fields",
        json={
            "project_name": project_name,
            "fields": {"X": "str", "Y": "str"},
        },
        headers=HEADERS,
    )
    assert fields_resp.status_code == 200, fields_resp.json()
    assert fields_resp.json()["backfilled_count"] == 3

    logs_resp = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert logs_resp.status_code == 200
    logs = {log["id"]: log for log in logs_resp.json()["logs"]}
    for log_id, expected_x in seeded:
        entries = logs[log_id]["entries"]
        assert entries["X"] == expected_x, (
            f"Existing value for X on log {log_id} must be preserved; "
            f"expected {expected_x!r}, got {entries['X']!r}"
        )
        assert entries["Y"] is None
