"""Write-plane tests: the canvas action endpoints.

- GET  /v0/admin/canvas/{token}/actions
- POST /v0/admin/canvas/{token}/action
- GET  /v0/admin/canvas/{token}/invocations/{id}

The frame is untrusted, so client-side validation is a usability feature and
everything that actually decides lives here. These tests are mostly about what the
server refuses and what it records, in that order: an invocation row exists before
anything is dispatched, because a run nobody recorded cannot be recovered.
"""

import json

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.tests.utils import ADMIN_HEADERS, create_test_user

# Contexts are `{user}/{assistant}/Canvas/*` in production; the assistant segment
# is where the dispatch target comes from, so the tests use the real shape.
VIEWS_CONTEXT = "u1/7/Canvas/Views"
ACTIONS_CONTEXT = "u1/7/Canvas/Actions"
INVOCATIONS_CONTEXT = "u1/7/Canvas/Invocations"

BULK_SEND_SCHEMA = {
    "type": "object",
    "required": ["recipients", "subject"],
    "properties": {
        "recipients": {
            "type": "array",
            "maxItems": 3,
            "items": {"type": "string"},
        },
        "subject": {"type": "string", "maxLength": 20},
    },
}

VALID_ARGS = {"recipients": ["a@b.com"], "subject": "Hello"}


async def _log(
    client: AsyncClient,
    user: dict,
    project: str,
    context: str,
    entries: dict,
):
    resp = await client.post(
        "/v0/logs",
        json={"project_name": project, "context": context, "entries": entries},
        headers=user["headers"],
    )
    assert resp.status_code == 200, resp.text
    return resp


async def _seed(
    client: AsyncClient,
    email: str,
    project: str,
    token: str,
    *,
    action: dict | None = None,
    canvas_status: str = "published",
) -> dict:
    """A project with a canvas, one declared action, and an invocations context."""
    user = await create_test_user(client, email)
    await client.post("/v0/project", json={"name": project}, headers=user["headers"])

    await _log(client, user, project, VIEWS_CONTEXT, {"token": token, "title": "T"})

    declared = {
        "canvas_token": token,
        "action_name": "bulk_send",
        "label": "Send to everyone listed",
        "kind": "function",
        "function_id": 42,
        "input_schema_json": json.dumps(BULK_SEND_SCHEMA),
        "destructive": True,
        "confirm": "This sends real email.",
        "max_invocations_per_hour": 20,
    }
    declared.update(action or {})
    await _log(client, user, project, ACTIONS_CONTEXT, declared)

    # Provisioned the way the assistant runtime provisions it: `invocation_id` is
    # auto-counted, and everything downstream addresses a run by that id. A context
    # created by a bare log write has no such field, which is a different context
    # wearing the same name.
    created = await client.post(
        f"/v0/project/{project}/contexts",
        json={
            "name": INVOCATIONS_CONTEXT,
            "unique_keys": {"invocation_id": "int"},
            "auto_counting": {"invocation_id": None},
        },
        headers=user["headers"],
    )
    assert created.status_code == 200, created.text

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
    return user


async def _invoke(client: AsyncClient, token: str, **body):
    payload = {"action_name": "bulk_send", "args": VALID_ARGS}
    payload.update(body)
    return await client.post(
        f"/v0/admin/canvas/{token}/action",
        json=payload,
        headers=ADMIN_HEADERS,
    )


# ===========================================================================
# What the frame is told
# ===========================================================================


@pytest.mark.anyio
async def test_actions_never_expose_their_targets(
    client: AsyncClient,
    dbsession: Session,
):
    """The canvas learns names and input shapes, never what runs.

    Filtered here rather than in console so no caller of this endpoint can leak a
    target by forgetting to strip it.
    """
    await _seed(client, "ca_list@test.com", "ca-list-proj", "ca_list_0001")

    resp = await client.get(
        "/v0/admin/canvas/ca_list_0001/actions",
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.text
    assert "function_id" not in body
    assert "42" not in body
    action = resp.json()["actions"][0]
    assert action["name"] == "bulk_send"
    assert action["input_schema"]["properties"]["subject"]["maxLength"] == 20


@pytest.mark.anyio
async def test_a_destructive_action_always_requires_confirmation(
    client: AsyncClient,
    dbsession: Session,
):
    # The copy is optional; the pause is not. An author who forgets `confirm` must
    # not thereby get a destructive action that fires on one click.
    await _seed(
        client,
        "ca_conf@test.com",
        "ca-conf-proj",
        "ca_conf_0001",
        action={"destructive": True, "confirm": None},
    )

    resp = await client.get(
        "/v0/admin/canvas/ca_conf_0001/actions",
        headers=ADMIN_HEADERS,
    )

    assert resp.json()["actions"][0]["requires_confirmation"] is True


# ===========================================================================
# Validation
# ===========================================================================


@pytest.mark.anyio
async def test_a_valid_invocation_is_recorded(client: AsyncClient, dbsession: Session):
    await _seed(client, "ca_ok@test.com", "ca-ok-proj", "ca_ok_000001")

    resp = await _invoke(client, "ca_ok_000001")

    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    # Auto-counted ids are 0-based, so the first run of the first canvas is 0 — a
    # magnitude assertion here would be asserting the counter rather than the row.
    assert isinstance(body["invocation_id"], int)
    assert body["invocation_id"] >= 0
    assert body["action_name"] == "bulk_send"
    assert body["status"] == "pending"
    assert body["deduplicated"] is False
    assert body["run_key"]


@pytest.mark.anyio
async def test_arguments_are_revalidated_against_the_stored_schema(
    client: AsyncClient,
    dbsession: Session,
):
    """The bound the author declared is enforced here, not in the browser.

    `maxItems` was made mandatory at author time precisely so this check has
    something to enforce — it is the blast radius of the action.
    """
    await _seed(client, "ca_bound@test.com", "ca-bound-proj", "ca_bound_001")

    resp = await _invoke(
        client,
        "ca_bound_001",
        args={
            "recipients": ["a@b.com", "c@d.com", "e@f.com", "g@h.com"],
            "subject": "Hi",
        },
    )

    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    # The message names the field, which is what lets the canvas say what to fix.
    assert "recipients" in resp.json()["detail"]


@pytest.mark.anyio
async def test_a_missing_required_argument_is_refused(
    client: AsyncClient,
    dbsession: Session,
):
    await _seed(client, "ca_req@test.com", "ca-req-proj", "ca_req_00001")

    resp = await _invoke(client, "ca_req_00001", args={"recipients": ["a@b.com"]})

    assert resp.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.anyio
async def test_an_overlong_string_is_refused(client: AsyncClient, dbsession: Session):
    await _seed(client, "ca_len@test.com", "ca-len-proj", "ca_len_00001")

    resp = await _invoke(
        client,
        "ca_len_00001",
        args={"recipients": ["a@b.com"], "subject": "x" * 100},
    )

    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "subject" in resp.json()["detail"]


@pytest.mark.anyio
async def test_arguments_to_a_schemaless_action_are_refused(
    client: AsyncClient,
    dbsession: Session,
):
    """An action with no schema takes nothing.

    Accepting arguments anyway would smuggle a payload past a validation step
    that does not exist for it.
    """
    await _seed(
        client,
        "ca_none@test.com",
        "ca-none-proj",
        "ca_none_0001",
        action={"input_schema_json": None},
    )

    with_args = await _invoke(client, "ca_none_0001", args={"anything": 1})
    without = await _invoke(client, "ca_none_0001", args={})

    assert with_args.status_code == status.HTTP_400_BAD_REQUEST
    assert without.status_code == status.HTTP_200_OK, without.text


@pytest.mark.anyio
async def test_an_undeclared_action_is_refused(client: AsyncClient, dbsession: Session):
    await _seed(client, "ca_undec@test.com", "ca-undec-proj", "ca_undec_001")

    resp = await _invoke(client, "ca_undec_001", action_name="delete_everything")

    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert "delete_everything" in resp.json()["detail"]


# ===========================================================================
# Idempotency and rate limiting
# ===========================================================================


@pytest.mark.anyio
async def test_the_same_arguments_collapse_onto_one_run(
    client: AsyncClient,
    dbsession: Session,
):
    """A double-click must send one email, not two.

    Two presses with the same input are the same intent, so the second returns the
    run that already exists rather than starting another.
    """
    await _seed(client, "ca_idem@test.com", "ca-idem-proj", "ca_idem_0001")

    first = await _invoke(client, "ca_idem_0001")
    second = await _invoke(client, "ca_idem_0001")

    assert first.status_code == status.HTTP_200_OK, first.text
    assert second.status_code == status.HTTP_200_OK, second.text
    assert second.json()["invocation_id"] == first.json()["invocation_id"]
    assert second.json()["deduplicated"] is True
    assert first.json()["deduplicated"] is False


@pytest.mark.anyio
async def test_different_arguments_are_different_runs(
    client: AsyncClient,
    dbsession: Session,
):
    # Deduplicating on the action alone would silently drop a second, genuinely
    # different send.
    await _seed(client, "ca_diff@test.com", "ca-diff-proj", "ca_diff_0001")

    first = await _invoke(client, "ca_diff_0001")
    second = await _invoke(
        client,
        "ca_diff_0001",
        args={"recipients": ["z@z.com"], "subject": "Other"},
    )

    assert second.json()["invocation_id"] != first.json()["invocation_id"]
    assert second.json()["deduplicated"] is False


@pytest.mark.anyio
async def test_the_rate_limit_is_enforced_per_action(
    client: AsyncClient,
    dbsession: Session,
):
    await _seed(
        client,
        "ca_rate@test.com",
        "ca-rate-proj",
        "ca_rate_0001",
        action={"max_invocations_per_hour": 2},
    )

    # Distinct arguments so deduplication does not mask the limit.
    first = await _invoke(
        client,
        "ca_rate_0001",
        args={"recipients": ["1@x.com"], "subject": "a"},
    )
    second = await _invoke(
        client,
        "ca_rate_0001",
        args={"recipients": ["2@x.com"], "subject": "b"},
    )
    third = await _invoke(
        client,
        "ca_rate_0001",
        args={"recipients": ["3@x.com"], "subject": "c"},
    )

    assert first.status_code == status.HTTP_200_OK, first.text
    assert second.status_code == status.HTTP_200_OK, second.text
    assert third.status_code == status.HTTP_429_TOO_MANY_REQUESTS
    assert "2 runs an hour" in third.json()["detail"]


# ===========================================================================
# Lifecycle
# ===========================================================================


@pytest.mark.anyio
async def test_a_quarantined_canvas_cannot_act(client: AsyncClient, dbsession: Session):
    """Quarantine stops the write path too.

    Stopping reads but not actions would leave the most dangerous half of a pulled
    canvas working.
    """
    user = await _seed(client, "ca_quar@test.com", "ca-quar-proj", "ca_quar_0001")
    patched = await client.patch(
        "/v0/canvas/tokens/ca_quar_0001",
        json={"status": "quarantined"},
        headers=user["headers"],
    )
    assert patched.status_code == status.HTTP_200_OK

    invoked = await _invoke(client, "ca_quar_0001")
    listed = await client.get(
        "/v0/admin/canvas/ca_quar_0001/actions",
        headers=ADMIN_HEADERS,
    )

    assert invoked.status_code == status.HTTP_403_FORBIDDEN
    assert listed.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.anyio
async def test_the_action_route_requires_the_admin_key(
    client: AsyncClient,
    dbsession: Session,
):
    user = await _seed(client, "ca_auth@test.com", "ca-auth-proj", "ca_auth_0001")

    resp = await client.post(
        "/v0/admin/canvas/ca_auth_0001/action",
        json={"action_name": "bulk_send", "args": VALID_ARGS},
        headers=user["headers"],
    )

    assert resp.status_code in (
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
    )


# ===========================================================================
# Polling
# ===========================================================================


@pytest.mark.anyio
async def test_an_invocation_can_be_read_back(client: AsyncClient, dbsession: Session):
    await _seed(client, "ca_poll@test.com", "ca-poll-proj", "ca_poll_0001")
    created = await _invoke(client, "ca_poll_0001")
    invocation_id = created.json()["invocation_id"]

    resp = await client.get(
        f"/v0/admin/canvas/ca_poll_0001/invocations/{invocation_id}",
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["invocation_id"] == invocation_id
    assert resp.json()["action_name"] == "bulk_send"


@pytest.mark.anyio
async def test_an_invocation_cannot_be_read_through_another_canvas(
    client: AsyncClient,
    dbsession: Session,
):
    """The id is scoped to the canvas in the path.

    Invocation ids are sequential per context, so without this scoping a token
    holder could walk another canvas's runs by guessing numbers.
    """
    await _seed(client, "ca_a@test.com", "ca-a-proj", "ca_scope_a01")
    await _seed(client, "ca_b@test.com", "ca-b-proj", "ca_scope_b01")

    created = await _invoke(client, "ca_scope_a01")
    invocation_id = created.json()["invocation_id"]

    resp = await client.get(
        f"/v0/admin/canvas/ca_scope_b01/invocations/{invocation_id}",
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.anyio
async def test_identical_arguments_do_not_collapse_forever(
    client: AsyncClient,
    dbsession: Session,
):
    """The dedup key is windowed, so a later repeat is a new run.

    Hashing only the canvas, action and arguments would make "send that reminder
    again tomorrow" return yesterday's run and send nothing — a silent no-op, which
    is worse than the occasional missed dedup at a window boundary.
    """
    from orchestra.web.api.canvas import views

    await _seed(client, "ca_win@test.com", "ca-win-proj", "ca_win_00001")

    first = await _invoke(client, "ca_win_00001")
    assert first.status_code == status.HTTP_200_OK, first.text

    # Advance past the window rather than sleeping through it.
    real = views.DEDUP_WINDOW_SECONDS
    views.DEDUP_WINDOW_SECONDS = 1
    try:
        import time

        time.sleep(1.1)
        later = await _invoke(client, "ca_win_00001")
    finally:
        views.DEDUP_WINDOW_SECONDS = real

    assert later.status_code == status.HTTP_200_OK, later.text
    assert later.json()["deduplicated"] is False
    assert later.json()["invocation_id"] != first.json()["invocation_id"]


@pytest.mark.anyio
async def test_a_caller_supplied_run_key_is_honoured(
    client: AsyncClient,
    dbsession: Session,
):
    # Console sends its own so a retry after a dropped response lands on the same
    # run even if the derived window has since rolled over.
    await _seed(client, "ca_key@test.com", "ca-key-proj", "ca_key_00001")

    first = await _invoke(client, "ca_key_00001", run_key="explicit-key-1")
    second = await _invoke(client, "ca_key_00001", run_key="explicit-key-1")

    assert first.json()["run_key"] == "explicit-key-1"
    assert second.json()["deduplicated"] is True
    assert second.json()["invocation_id"] == first.json()["invocation_id"]
