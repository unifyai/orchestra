"""Correctness tests for the POST /v0/logs/fields backfill path.

The endpoint runs one ``UPDATE log_event SET data = template ||
COALESCE(data, '{}'::jsonb) FROM log_event_context ... WHERE NOT (data ?&
names)`` gated by a ``field_type.backfilled_at`` check: ``create_fields``
returns only the fields whose ``backfilled_at IS NULL``, and ``mark_backfilled``
stamps them after a successful UPDATE. These cases lock down the invariants
that matter to production:

* Existing non-null values are never overwritten by the null template
  (right-biased merge: ``template || data``).
* Idempotent re-POSTs short-circuit at the DAO: once stamped, subsequent
  calls return an empty pending list and skip the UPDATE entirely (zero
  writes, zero scans, ``backfilled_count == 0``).
* The ``?&`` guard remains as a safety net within a single call: if some
  rows already have the pending field (e.g. via a log-creation side effect)
  those rows are filtered out before any write.
* Empty ``fields`` dict short-circuits before any DB work.
* Heterogeneous rows (different subsets of missing keys) all converge to the
  full requested schema in a single statement.
* ``backfilled_count`` in the response is the number of **log_event rows**
  the UPDATE touched (``result.rowcount``), **not** the old ``(row × missing
  field)`` pair count that the pre-refactor Python pipeline returned.

These complement ``test_create_fields*backfill*`` in ``test_log_fields.py``;
they intentionally exercise edge shapes the original tests did not (empty
fields dict, idempotent second call, mixed heterogeneous rows, preservation
across a batch of rows, and the log-creation side-effect path).
"""

import pytest
from httpx import AsyncClient

from . import HEADERS, _create_log, _create_project


@pytest.mark.anyio
async def test_backfill_no_op_when_all_rows_already_have_every_field(
    client: AsyncClient,
):
    """Second call with the same fields must short-circuit via the backfilled_at gate.

    Production hot path: clients re-POST the same field set repeatedly.
    First call: both fields are NEW, so ``create_fields`` returns them in
    the pending list, the UPDATE null-merges them into both rows, and the
    endpoint stamps ``field_type.backfilled_at = now()`` for both.
    Second call: ``create_fields`` sees ``backfilled_at`` already set for
    both and returns an empty pending list — the UPDATE is skipped
    entirely, never reaching the ``?&`` safety net. This is the fix for the
    staging Cloud SQL CPU spike: previously, even with `?&` suppressing
    writes, every idempotent re-POST still forced a full-context scan.
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
@pytest.mark.parametrize(
    "seed_field,new_field",
    [
        ("X", "Y"),
        ("alpha", "beta"),
    ],
)
async def test_backfill_handles_log_creation_side_effect_field(
    client: AsyncClient,
    seed_field: str,
    new_field: str,
):
    """Exercises the log-creation-side-effect path that the ``xmax=0`` attempt broke.

    Setup:

    1. ``_create_log`` with ``explicit_types={seed_field: ...}`` and a
       sibling log *without* ``seed_field``. This registers
       ``field_type[seed_field]`` via ``bulk_create_field_types`` with
       ``backfilled_at = NULL`` — the side-effect path. Only one of the two
       rows carries ``seed_field``.
    2. POST ``/v0/logs/fields`` with ``{seed_field, new_field}``.

    Correctness demands both fields are treated as pending-backfill even
    though ``seed_field`` already exists in ``field_type``:

    - ``create_fields`` upserts, ``set_`` excludes ``backfilled_at`` so the
      NULL stamp is preserved, both names end up in the pending list.
    - The UPDATE's ``?&`` safety net filters out the row that already has
      ``seed_field`` (it still gets ``new_field`` via the merge) and fully
      null-merges both keys into the sibling row.

    First call: ``backfilled_count`` is the number of rows actually
    touched (both rows are touched — one because it's missing both keys,
    one because it's missing ``new_field``). Sibling-row assertions
    guarantee the right-biased merge left existing values intact.

    Second call with the same payload: DAO-level short-circuit returns
    empty pending list, ``backfilled_count == 0``, no UPDATE issued.
    """
    project_name = f"test-backfill-side-effect-{seed_field}-{new_field}"
    await _create_project(client, project_name)

    resp_seeded = await _create_log(
        client,
        project_name,
        entries={
            seed_field: "preserve-me",
            "explicit_types": {seed_field: {"type": "str", "mutable": True}},
        },
    )
    assert resp_seeded.status_code == 200, resp_seeded.json()
    seeded_log_id = resp_seeded.json()["log_event_ids"][0]

    resp_sibling = await _create_log(
        client,
        project_name,
        entries={"other_field": "sibling"},
    )
    assert resp_sibling.status_code == 200, resp_sibling.json()
    sibling_log_id = resp_sibling.json()["log_event_ids"][0]

    payload = {
        "project_name": project_name,
        "fields": {seed_field: "str", new_field: "str"},
    }

    first = await client.post("/v0/logs/fields", json=payload, headers=HEADERS)
    assert first.status_code == 200, first.json()
    assert first.json()["backfilled_count"] == 2, (
        "Both rows must be touched on the first call: the seeded row is "
        "missing new_field and the sibling row is missing both keys. The "
        "seed_field being pre-registered as a log-creation side effect must "
        "NOT cause it to be skipped — backfilled_at was still NULL."
    )

    logs_resp = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert logs_resp.status_code == 200
    logs = {log["id"]: log for log in logs_resp.json()["logs"]}

    assert (
        logs[seeded_log_id]["entries"][seed_field] == "preserve-me"
    ), "Right-biased merge must leave the seeded row's existing value untouched."
    assert logs[seeded_log_id]["entries"][new_field] is None
    assert logs[sibling_log_id]["entries"][seed_field] is None
    assert logs[sibling_log_id]["entries"][new_field] is None

    second = await client.post("/v0/logs/fields", json=payload, headers=HEADERS)
    assert second.status_code == 200, second.json()
    assert second.json()["backfilled_count"] == 0, (
        "Second call must be a DAO-level no-op: both fields are now stamped, "
        "pending list is empty, UPDATE is never issued."
    )


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
