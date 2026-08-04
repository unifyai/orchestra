"""Read-plane tests: POST /v0/admin/canvas/{token}/query.

Real projects, real contexts, real log rows, real bindings — no mocks. The point
of this endpoint is that it accepts an **alias and nothing else**, so most of what
is worth asserting is about what it refuses to be told.
"""

import json

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.tests.utils import ADMIN_HEADERS, create_test_user

TASKS_CONTEXT = "Canvas/Data/Tasks"
VIEWS_CONTEXT = "Canvas/Views"

TASKS = [
    {"name": "Ship the kit", "status": "open", "points": 5},
    {"name": "Wire the read plane", "status": "open", "points": 3},
    {"name": "Review the gate", "status": "done", "points": 2},
]

# A second table nobody declared a binding for. Its only job is to be reachable
# by the owner and unreachable through this endpoint.
VAULT_CONTEXT = "Canvas/Data/Vault"
VAULT = [{"name": "api_key", "value": "super-secret"}]


async def _seed(client: AsyncClient, email: str, project: str) -> dict:
    """A project holding a task table, a private table, and one canvas record."""
    user = await create_test_user(client, email)
    await client.post("/v0/project", json={"name": project}, headers=user["headers"])

    for row in TASKS:
        resp = await client.post(
            "/v0/logs",
            json={"project_name": project, "context": TASKS_CONTEXT, "entries": row},
            headers=user["headers"],
        )
        assert resp.status_code == 200, resp.text

    for row in VAULT:
        resp = await client.post(
            "/v0/logs",
            json={
                "project_name": project,
                "context": VAULT_CONTEXT,
                "entries": row,
            },
            headers=user["headers"],
        )
        assert resp.status_code == 200, resp.text

    return user


async def _seed_canvas(
    client: AsyncClient,
    user: dict,
    project: str,
    token: str,
    bindings: list,
    *,
    canvas_status: str = "published",
) -> None:
    """Write the canvas record, then register its routing token."""
    resp = await client.post(
        "/v0/logs",
        json={
            "project_name": project,
            "context": VIEWS_CONTEXT,
            "entries": {
                "token": token,
                "title": "Tracker",
                "bindings_json": json.dumps(bindings),
            },
        },
        headers=user["headers"],
    )
    assert resp.status_code == 200, resp.text

    resp = await client.post(
        "/v0/canvas/tokens",
        json={
            "token": token,
            "context_name": VIEWS_CONTEXT,
            "project_name": project,
            "status": canvas_status,
        },
        headers=user["headers"],
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.text


def _filter_binding(alias: str = "tasks", **args) -> dict:
    merged = {"operation": "filter", "limit": 100}
    merged.update(args)
    return {
        "kind": "query",
        "alias": alias,
        "manager": "data",
        "table": TASKS_CONTEXT,
        "resolved_context": TASKS_CONTEXT,
        "args": merged,
    }


# ===========================================================================
# The contract: an alias, and nothing else
# ===========================================================================


@pytest.mark.anyio
async def test_an_alias_returns_the_stored_bindings_rows(
    client: AsyncClient,
    dbsession: Session,
):
    user = await _seed(client, "cq_ok@test.com", "cq-ok-proj")
    await _seed_canvas(client, user, "cq-ok-proj", "cq_ok_000001", [_filter_binding()])

    resp = await client.post(
        "/v0/admin/canvas/cq_ok_000001/query",
        json={"alias": "tasks"},
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["alias"] == "tasks"
    assert len(body["rows"]) == len(TASKS)
    assert {row["name"] for row in body["rows"]} == {t["name"] for t in TASKS}


@pytest.mark.anyio
async def test_the_stored_filter_is_applied(client: AsyncClient, dbsession: Session):
    """The filter comes from the record, so it cannot be relaxed by the caller."""
    user = await _seed(client, "cq_filter@test.com", "cq-filter-proj")
    await _seed_canvas(
        client,
        user,
        "cq-filter-proj",
        "cq_filter_01",
        [_filter_binding(filter="status == 'open'")],
    )

    resp = await client.post(
        "/v0/admin/canvas/cq_filter_01/query",
        json={"alias": "tasks"},
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    rows = resp.json()["rows"]
    assert len(rows) == 2
    assert all(row["status"] == "open" for row in rows)


@pytest.mark.anyio
async def test_a_client_supplied_context_and_filter_are_ignored(
    client: AsyncClient,
    dbsession: Session,
):
    """The hole this endpoint exists to close.

    The dashboard tile bridge takes `context` and `filter` from the body, so any
    token holder can read anything in the creator's project. Extra fields here
    must not reach the query — the canvas gets its own binding or nothing.
    """
    user = await _seed(client, "cq_inject@test.com", "cq-inject-proj")
    await _seed_canvas(
        client,
        user,
        "cq-inject-proj",
        "cq_inject_01",
        [_filter_binding(filter="status == 'open'")],
    )

    resp = await client.post(
        "/v0/admin/canvas/cq_inject_01/query",
        json={
            "alias": "tasks",
            # All of this is an attempt to reach the vault table and lift the cap.
            "context": VAULT_CONTEXT,
            "filter": None,
            "limit": 1000,
        },
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    rows = resp.json()["rows"]
    # Still the canvas's own binding: two open tasks, and no vault row anywhere.
    assert len(rows) == 2
    assert all("value" not in row for row in rows)


@pytest.mark.anyio
async def test_an_undeclared_alias_is_refused(client: AsyncClient, dbsession: Session):
    """404 rather than an empty list.

    Returning no rows would leave the canvas rendering its empty state, which
    looks exactly like real data being absent — the actor would have no way to
    tell a typo from a genuinely empty table.
    """
    user = await _seed(client, "cq_alias@test.com", "cq-alias-proj")
    await _seed_canvas(
        client,
        user,
        "cq-alias-proj",
        "cq_alias_001",
        [_filter_binding()],
    )

    resp = await client.post(
        "/v0/admin/canvas/cq_alias_001/query",
        json={"alias": "vault"},
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert "vault" in resp.json()["detail"]


@pytest.mark.anyio
async def test_a_malformed_alias_is_rejected_by_the_schema(
    client: AsyncClient,
    dbsession: Session,
):
    # Aliases are JS identifiers; anything else is a caller error rather than
    # something to look up and miss.
    user = await _seed(client, "cq_bad@test.com", "cq-bad-proj")
    await _seed_canvas(client, user, "cq-bad-proj", "cq_bad_00001", [_filter_binding()])

    resp = await client.post(
        "/v0/admin/canvas/cq_bad_00001/query",
        json={"alias": "tasks; drop"},
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# ===========================================================================
# Lifecycle and auth
# ===========================================================================


@pytest.mark.anyio
async def test_a_quarantined_canvas_serves_no_data(
    client: AsyncClient,
    dbsession: Session,
):
    """Quarantine has to stop reads, not just the bundle.

    A canvas pulled for leaking data would otherwise keep answering queries for
    every frame that was already open.
    """
    user = await _seed(client, "cq_quar@test.com", "cq-quar-proj")
    await _seed_canvas(
        client,
        user,
        "cq-quar-proj",
        "cq_quar_0001",
        [_filter_binding()],
        canvas_status="published",
    )
    patched = await client.patch(
        "/v0/canvas/tokens/cq_quar_0001",
        json={"status": "quarantined"},
        headers=user["headers"],
    )
    assert patched.status_code == status.HTTP_200_OK

    resp = await client.post(
        "/v0/admin/canvas/cq_quar_0001/query",
        json={"alias": "tasks"},
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_403_FORBIDDEN
    assert "quarantined" in resp.json()["detail"]


@pytest.mark.anyio
async def test_a_draft_canvas_serves_no_data(client: AsyncClient, dbsession: Session):
    user = await _seed(client, "cq_draft@test.com", "cq-draft-proj")
    await _seed_canvas(
        client,
        user,
        "cq-draft-proj",
        "cq_draft_001",
        [_filter_binding()],
        canvas_status="draft",
    )

    resp = await client.post(
        "/v0/admin/canvas/cq_draft_001/query",
        json={"alias": "tasks"},
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.anyio
async def test_an_unknown_token_is_a_404(client: AsyncClient, dbsession: Session):
    resp = await client.post(
        "/v0/admin/canvas/cq_nope_0001/query",
        json={"alias": "tasks"},
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.anyio
async def test_the_query_route_requires_the_admin_key(
    client: AsyncClient,
    dbsession: Session,
):
    # Console session-auths the viewer and then calls this with the admin key. A
    # user key reaching it directly would bypass that check entirely.
    user = await _seed(client, "cq_auth@test.com", "cq-auth-proj")
    await _seed_canvas(
        client,
        user,
        "cq-auth-proj",
        "cq_auth_0001",
        [_filter_binding()],
    )

    resp = await client.post(
        "/v0/admin/canvas/cq_auth_0001/query",
        json={"alias": "tasks"},
        headers=user["headers"],
    )

    assert resp.status_code in (
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
    )


# ===========================================================================
# Operations other than filter
# ===========================================================================


@pytest.mark.anyio
async def test_a_reduction_comes_back_as_rows(client: AsyncClient, dbsession: Session):
    """Every alias reaches the canvas as an array, whatever the operation.

    A reduction is a scalar; normalising it here is what lets an authored canvas
    read `canvas.data.<alias>` without branching on which operation produced it.
    """
    user = await _seed(client, "cq_reduce@test.com", "cq-reduce-proj")
    await _seed_canvas(
        client,
        user,
        "cq-reduce-proj",
        "cq_reduce_01",
        [
            {
                "kind": "query",
                "alias": "total",
                "manager": "data",
                "table": TASKS_CONTEXT,
                "resolved_context": TASKS_CONTEXT,
                "args": {
                    "operation": "reduce",
                    "metric": "sum",
                    "columns": "points",
                },
            },
        ],
    )

    resp = await client.post(
        "/v0/admin/canvas/cq_reduce_01/query",
        json={"alias": "total"},
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    rows = resp.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["value"] == pytest.approx(10)


@pytest.mark.anyio
async def test_reaching_the_row_cap_is_reported(
    client: AsyncClient,
    dbsession: Session,
):
    """`truncated` exists so a canvas can say it is showing a partial set.

    Presenting a truncated list as complete is a correctness problem the viewer
    cannot see, which is why this travels with the rows.
    """
    user = await _seed(client, "cq_trunc@test.com", "cq-trunc-proj")
    await _seed_canvas(
        client,
        user,
        "cq-trunc-proj",
        "cq_trunc_001",
        [_filter_binding(limit=2)],
    )

    resp = await client.post(
        "/v0/admin/canvas/cq_trunc_001/query",
        json={"alias": "tasks"},
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert len(body["rows"]) == 2
    assert body["truncated"] is True


@pytest.mark.anyio
async def test_a_binding_that_selects_several_columns_still_returns_rows(
    client: AsyncClient,
    dbsession: Session,
):
    """Guards the field-list separator, which fails silently when wrong.

    The log query joins field names with `&`. A comma-joined `from_fields` matches
    no field and returns zero rows, so a canvas binding naming two columns would
    render empty with no error anywhere — which is exactly how this was first
    written.
    """
    user = await _seed(client, "cq_cols@test.com", "cq-cols-proj")
    await _seed_canvas(
        client,
        user,
        "cq-cols-proj",
        "cq_cols_0001",
        [_filter_binding(columns=["name", "status"])],
    )

    resp = await client.post(
        "/v0/admin/canvas/cq_cols_0001/query",
        json={"alias": "tasks"},
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    rows = resp.json()["rows"]
    assert len(rows) == len(TASKS)
    # Projected to exactly the named columns, and nothing else leaks through.
    assert all(set(row) == {"name", "status"} for row in rows), rows


@pytest.mark.anyio
async def test_large_values_survive_the_read_untruncated(
    client: AsyncClient,
    dbsession: Session,
):
    """Guards against a display-style value limit on machine-consumed reads.

    The record read once trimmed every value to 1000 characters, so a canvas
    declaring eight bindings (2.5 kB of bindings_json) read back as truncated,
    unparseable JSON — and every alias 404'd as undeclared while the stored row
    was perfectly intact. The same limit silently corrupted any data cell past
    1000 characters with a trailing ellipsis. Both halves are pinned here: a
    bindings declaration past the old limit still resolves, and a long cell
    value comes back byte-complete.
    """
    project = "cq-big-proj"
    long_context = "Canvas/Data/Long"
    long_text = "x" * 1500

    user = await _seed(client, "cq_big@test.com", project)
    resp = await client.post(
        "/v0/logs",
        json={
            "project_name": project,
            "context": long_context,
            "entries": {"name": "novel", "body": long_text},
        },
        headers=user["headers"],
    )
    assert resp.status_code == 200, resp.text

    long_binding = dict(
        _filter_binding(alias="long"),
        table=long_context,
        resolved_context=long_context,
    )
    padding = [
        _filter_binding(alias=f"padding_{i}", filter=f"status == 'open-{i:04d}'")
        for i in range(20)
    ]
    bindings = [_filter_binding(), long_binding, *padding]
    assert len(json.dumps(bindings)) > 1000

    await _seed_canvas(client, user, project, "cq_big_00001", bindings)

    resp = await client.post(
        "/v0/admin/canvas/cq_big_00001/query",
        json={"alias": "tasks"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert len(resp.json()["rows"]) == len(TASKS)

    resp = await client.post(
        "/v0/admin/canvas/cq_big_00001/query",
        json={"alias": "long"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    rows = resp.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["body"] == long_text
