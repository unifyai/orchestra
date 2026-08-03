"""Endpoint tests for the canvas token routes.

- POST   /v0/canvas/tokens                  (register)
- PATCH  /v0/canvas/tokens/{token}          (visibility / status)
- DELETE /v0/canvas/tokens/{token}          (revoke)
- GET    /v0/admin/canvas/tokens/{token}    (resolve)

Every case here is a property something downstream depends on. The routing row is
the only thing standing between a token in a URL and somebody else's data, so the
ownership and enumeration checks matter more than the happy path.
"""

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.tests.utils import ADMIN_HEADERS, create_test_user

from .conftest import token_body


async def _user_with_project(client: AsyncClient, email: str, project: str) -> dict:
    user = await create_test_user(client, email)
    await client.post("/v0/project", json={"name": project}, headers=user["headers"])
    return user


async def _register(client: AsyncClient, user: dict, token: str, project: str, **kw):
    """Register a canvas token, asserting it worked.

    Registration is setup for most of the cases below, and an unchecked setup
    failure surfaces later as a confusing 404 on the assertion under test rather
    than as the schema rejection it actually was.
    """
    resp = await client.post(
        "/v0/canvas/tokens",
        json=token_body(token, f"{project}/Canvas/Views", project, **kw),
        headers=user["headers"],
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    return resp


# ===========================================================================
# Registration
# ===========================================================================


@pytest.mark.anyio
async def test_register_defaults_to_private_draft(
    client: AsyncClient,
    dbsession: Session,
):
    """A newly registered canvas is neither shared nor servable by default.

    Defaults are the failure mode nobody notices: if registration defaulted to
    published or to team visibility, every canvas the assistant authored would be
    live the moment it existed.
    """
    user = await _user_with_project(client, "canvas_reg@test.com", "canvas-reg-proj")

    resp = await client.post(
        "/v0/canvas/tokens",
        json=token_body(
            "canvas_reg01",
            "canvas-reg-proj/Canvas/Views",
            "canvas-reg-proj",
        ),
        headers=user["headers"],
    )

    assert resp.status_code == status.HTTP_201_CREATED
    data = resp.json()
    assert data["token"] == "canvas_reg01"
    assert data["visibility"] == "private"
    assert data["status"] == "draft"


@pytest.mark.anyio
async def test_register_rejects_an_unknown_visibility(
    client: AsyncClient,
    dbsession: Session,
):
    # The value is an authorization decision, so an unrecognised one must be a
    # request error rather than something stored and interpreted later.
    user = await _user_with_project(client, "canvas_vis@test.com", "canvas-vis-proj")

    resp = await client.post(
        "/v0/canvas/tokens",
        json=token_body(
            "canvas_vis01",
            "canvas-vis-proj/Canvas/Views",
            "canvas-vis-proj",
            visibility="everyone",
        ),
        headers=user["headers"],
    )

    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


@pytest.mark.anyio
async def test_register_conflicts_on_a_duplicate_token(
    client: AsyncClient,
    dbsession: Session,
):
    user = await _user_with_project(client, "canvas_dup@test.com", "canvas-dup-proj")
    body = token_body(
        "canvas_dup01",
        "canvas-dup-proj/Canvas/Views",
        "canvas-dup-proj",
    )

    first = await client.post("/v0/canvas/tokens", json=body, headers=user["headers"])
    second = await client.post("/v0/canvas/tokens", json=body, headers=user["headers"])

    assert first.status_code == status.HTTP_201_CREATED
    assert second.status_code == status.HTTP_409_CONFLICT


@pytest.mark.anyio
async def test_register_refuses_a_project_the_caller_cannot_reach(
    client: AsyncClient,
    dbsession: Session,
):
    """Naming somebody else's project must not create a token pointing at it.

    This is the check that makes the admin resolve endpoint safe: console trusts
    the row's project and identity completely, so the row has to be trustworthy.
    """
    owner = await _user_with_project(
        client,
        "canvas_own@test.com",
        "canvas-private-proj",
    )
    assert owner
    intruder = await create_test_user(client, "canvas_intruder@test.com")

    resp = await client.post(
        "/v0/canvas/tokens",
        json=token_body(
            "canvas_int01",
            "canvas-private-proj/Canvas/Views",
            "canvas-private-proj",
        ),
        headers=intruder["headers"],
    )

    assert resp.status_code == status.HTTP_404_NOT_FOUND


# ===========================================================================
# Lifecycle
# ===========================================================================


@pytest.mark.anyio
async def test_publishing_and_quarantining_take_effect(
    client: AsyncClient,
    dbsession: Session,
):
    """Status moves without reissuing the URL.

    Quarantine is the kill switch for a canvas already in front of users, so it
    has to work on the live token rather than by replacing it.
    """
    user = await _user_with_project(client, "canvas_pub@test.com", "canvas-pub-proj")
    await _register(client, user, "canvas_pub01", "canvas-pub-proj")

    published = await client.patch(
        "/v0/canvas/tokens/canvas_pub01",
        json={"status": "published", "visibility": "team"},
        headers=user["headers"],
    )
    assert published.status_code == status.HTTP_200_OK
    assert published.json()["status"] == "published"
    assert published.json()["visibility"] == "team"

    quarantined = await client.patch(
        "/v0/canvas/tokens/canvas_pub01",
        json={"status": "quarantined"},
        headers=user["headers"],
    )
    assert quarantined.status_code == status.HTTP_200_OK
    assert quarantined.json()["status"] == "quarantined"
    # Visibility is untouched by a status-only patch.
    assert quarantined.json()["visibility"] == "team"


@pytest.mark.anyio
async def test_an_empty_patch_is_rejected(client: AsyncClient, dbsession: Session):
    # A patch that changes nothing but returns 200 reads as success; the caller
    # would have no way to tell its field name was wrong.
    user = await _user_with_project(client, "canvas_noop@test.com", "canvas-noop-proj")
    await _register(client, user, "canvas_noop1", "canvas-noop-proj")

    resp = await client.patch(
        "/v0/canvas/tokens/canvas_noop1",
        json={},
        headers=user["headers"],
    )

    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


@pytest.mark.anyio
async def test_only_the_owner_can_patch_or_delete(
    client: AsyncClient,
    dbsession: Session,
):
    """Holding the token is not authority to change it.

    A `public_link` canvas hands its token to anyone who opens the URL, so
    ownership has to be checked on every mutation rather than inferred from
    knowing the token.
    """
    owner = await _user_with_project(
        client,
        "canvas_owner@test.com",
        "canvas-owner-proj",
    )
    await _register(
        client,
        owner,
        "canvas_own01",
        "canvas-owner-proj",
        visibility="public_link",
    )
    other = await create_test_user(client, "canvas_other@test.com")

    patched = await client.patch(
        "/v0/canvas/tokens/canvas_own01",
        json={"status": "published"},
        headers=other["headers"],
    )
    deleted = await client.delete(
        "/v0/canvas/tokens/canvas_own01",
        headers=other["headers"],
    )

    assert patched.status_code == status.HTTP_403_FORBIDDEN
    assert deleted.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.anyio
async def test_delete_stops_the_token_resolving(
    client: AsyncClient,
    dbsession: Session,
):
    user = await _user_with_project(client, "canvas_del@test.com", "canvas-del-proj")
    await _register(client, user, "canvas_del01", "canvas-del-proj")

    deleted = await client.delete(
        "/v0/canvas/tokens/canvas_del01",
        headers=user["headers"],
    )
    resolved = await client.get(
        "/v0/admin/canvas/tokens/canvas_del01",
        headers=ADMIN_HEADERS,
    )

    assert deleted.status_code == status.HTTP_200_OK
    assert resolved.status_code == status.HTTP_404_NOT_FOUND


# ===========================================================================
# Admin resolution
# ===========================================================================


@pytest.mark.anyio
async def test_resolve_returns_the_identity_and_the_access_state(
    client: AsyncClient,
    dbsession: Session,
):
    """One call gives console everything it needs before using the admin key.

    Identity to read as, plus visibility and status to decide whether to. Split
    across two calls this would be latency on every canvas load.
    """
    user = await _user_with_project(client, "canvas_res@test.com", "canvas-res-proj")
    await _register(
        client,
        user,
        "canvas_res01",
        "canvas-res-proj",
        visibility="team",
        status="published",
    )

    resp = await client.get(
        "/v0/admin/canvas/tokens/canvas_res01",
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["context_name"] == "canvas-res-proj/Canvas/Views"
    assert data["project_name"] == "canvas-res-proj"
    assert data["user_id"]
    assert data["visibility"] == "team"
    assert data["status"] == "published"


@pytest.mark.anyio
async def test_resolve_still_reports_a_quarantined_canvas(
    client: AsyncClient,
    dbsession: Session,
):
    """Quarantined resolves with its status rather than 404-ing.

    The caller has to distinguish "no such canvas" from "exists but is not
    servable" — the owner's editor should still load what a viewer must not.
    """
    user = await _user_with_project(client, "canvas_qr@test.com", "canvas-qr-proj")
    await _register(
        client,
        user,
        "canvas_qr001",
        "canvas-qr-proj",
        status="quarantined",
    )

    resp = await client.get(
        "/v0/admin/canvas/tokens/canvas_qr001",
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["status"] == "quarantined"


@pytest.mark.anyio
async def test_resolve_requires_the_admin_key(client: AsyncClient, dbsession: Session):
    # A user key on the admin route would make the whole ownership model
    # decorative, since resolution hands back the identity to read as.
    user = await _user_with_project(client, "canvas_auth@test.com", "canvas-auth-proj")
    await _register(client, user, "canvas_aut01", "canvas-auth-proj")

    resp = await client.get(
        "/v0/admin/canvas/tokens/canvas_aut01",
        headers=user["headers"],
    )

    assert resp.status_code in (
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
    )


@pytest.mark.anyio
async def test_the_owner_can_read_back_their_own_registration(
    client: AsyncClient,
    dbsession: Session,
):
    """The writer needs this to tell its own retry from a token collision.

    Registration answers 409 for both, so without a way to confirm which mapping
    exists, a collision would be accepted as success and the canvas URL would
    resolve to somebody else's view.
    """
    user = await _user_with_project(client, "canvas_read@test.com", "canvas-read-proj")
    await _register(client, user, "canvas_rd001", "canvas-read-proj")

    mine = await client.get(
        "/v0/canvas/tokens/canvas_rd001",
        headers=user["headers"],
    )

    assert mine.status_code == status.HTTP_200_OK, mine.text
    assert mine.json()["context_name"] == "canvas-read-proj/Canvas/Views"


@pytest.mark.anyio
async def test_reading_someone_elses_registration_is_refused(
    client: AsyncClient,
    dbsession: Session,
):
    user = await _user_with_project(client, "canvas_rdo@test.com", "canvas-rdo-proj")
    await _register(client, user, "canvas_rdo01", "canvas-rdo-proj")
    other = await create_test_user(client, "canvas_rdx@test.com")

    resp = await client.get(
        "/v0/canvas/tokens/canvas_rdo01",
        headers=other["headers"],
    )

    assert resp.status_code == status.HTTP_403_FORBIDDEN
