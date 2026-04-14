"""
Tests for assistant secrets CRUD endpoints and integration with admin responses
and assistant cleanup.

Covers:
1. AssistantSecret model constraints (PK, cascade, not-null)
2. AssistantSecretDAO (get_all, get, upsert, delete, delete_all)
3. POST /assistant/{id}/secret  (create)
4. PUT  /assistant/{id}/secret/{name}  (update)
5. DELETE /assistant/{id}/secret/{name}  (delete)
6. Admin GET /admin/assistant includes secrets
7. Non-admin responses exclude secrets
8. Cleanup integration (secrets removed on assistant deletion)
9. End-to-end lifecycle
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchestra.db.dao.assistant_secret_dao import AssistantSecretDAO
from orchestra.db.models.orchestra_models import (
    Assistant,
    AssistantSecret,
    BillingAccount,
    User,
)
from orchestra.tests.utils import ADMIN_HEADERS, HEADERS

# ============================================================================
# Helpers
# ============================================================================


def _make_user_ba(
    dbsession: Session,
    uid: str,
    email: str | None = None,
    credits: float = 10000,
) -> tuple[User, BillingAccount]:
    ba = BillingAccount(
        credits=Decimal(str(credits)),
        account_status="ACTIVE",
    )
    dbsession.add(ba)
    dbsession.flush()
    user = User(
        id=uid,
        email=email or f"{uid}@test.com",
        billing_account_id=ba.id,
    )
    dbsession.add(user)
    dbsession.flush()
    return user, ba


def _make_assistant(
    dbsession: Session,
    user_id: str,
    first_name: str = "SecretTest",
    surname: str = "Bot",
) -> Assistant:
    a = Assistant(
        user_id=user_id,
        first_name=first_name,
        surname=surname,
    )
    dbsession.add(a)
    dbsession.flush()
    return a


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def mock_assistant_infra_calls(request):
    if "no_mock_infra" in request.keywords:
        yield
        return

    with patch(
        "orchestra.web.api.assistant.views.wake_up_assistant",
        new_callable=AsyncMock,
    ) as mock_wake_up, patch(
        "orchestra.web.api.assistant.views.reawaken_assistant",
        new_callable=AsyncMock,
    ) as mock_reawaken, patch(
        "orchestra.services.bucket_service.BucketService.__init__",
        lambda self: None,
    ):
        mock_wake_up.return_value = MagicMock(status_code=200)
        mock_reawaken.return_value = MagicMock(status_code=200, json=lambda: {})
        yield


def _mock_get_db_session_generator(real_session):
    def mock_get_db_session(request):
        yield real_session

    return mock_get_db_session


@pytest.fixture
def mock_infra(dbsession):
    """Light infrastructure mocking for secret endpoint tests."""
    patches = {
        "wake_up_assistant": AsyncMock(return_value=MagicMock(status_code=200)),
        "reawaken_assistant": AsyncMock(
            return_value=MagicMock(status_code=200, json=lambda: {}),
        ),
    }
    with patch.multiple("orchestra.web.api.assistant.views", **patches):
        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.is_staging = True
            with patch(
                "orchestra.web.api.assistant.views.get_db_session",
                side_effect=_mock_get_db_session_generator(dbsession),
            ):
                with patch(
                    "orchestra.services.bucket_service.BucketService.__init__",
                    lambda self: None,
                ):
                    yield patches


async def _create_assistant(client: AsyncClient) -> int:
    """Create an assistant via the API and return its agent_id."""
    resp = await client.post(
        "/v0/assistant",
        json={"first_name": "Secret", "surname": "Test", "create_infra": False},
        headers=HEADERS,
    )
    assert resp.status_code == status.HTTP_200_OK, resp.json()
    return int(resp.json()["info"]["agent_id"])


# ============================================================================
# 1. Model Constraint Tests
# ============================================================================


class TestAssistantSecretModel:

    def test_create_secret(self, dbsession: Session):
        user, _ba = _make_user_ba(dbsession, "sec-model-1")
        a = _make_assistant(dbsession, user.id)
        row = AssistantSecret(
            user_id=user.id,
            agent_id=a.agent_id,
            secret_name="TOKEN",
            secret_value="abc123",
        )
        dbsession.add(row)
        dbsession.flush()
        assert row.secret_name == "TOKEN"
        assert row.secret_value == "abc123"
        assert row.created_at is not None

    def test_composite_primary_key_rejects_duplicate(self, dbsession: Session):
        user, _ba = _make_user_ba(dbsession, "sec-model-2")
        a = _make_assistant(dbsession, user.id)
        dbsession.add(
            AssistantSecret(
                user_id=user.id,
                agent_id=a.agent_id,
                secret_name="DUP",
                secret_value="v1",
            ),
        )
        dbsession.flush()
        dbsession.add(
            AssistantSecret(
                user_id=user.id,
                agent_id=a.agent_id,
                secret_name="DUP",
                secret_value="v2",
            ),
        )
        with pytest.raises(IntegrityError):
            dbsession.flush()
        dbsession.rollback()

    def test_different_names_same_assistant(self, dbsession: Session):
        user, _ba = _make_user_ba(dbsession, "sec-model-3")
        a = _make_assistant(dbsession, user.id)
        for name in ("ACCESS_TOKEN", "REFRESH_TOKEN", "EXPIRES_AT"):
            dbsession.add(
                AssistantSecret(
                    user_id=user.id,
                    agent_id=a.agent_id,
                    secret_name=name,
                    secret_value=f"val-{name}",
                ),
            )
        dbsession.flush()
        count = (
            dbsession.query(AssistantSecret)
            .filter(AssistantSecret.agent_id == a.agent_id)
            .count()
        )
        assert count == 3

    def test_same_name_different_assistants(self, dbsession: Session):
        user, _ba = _make_user_ba(dbsession, "sec-model-4")
        a1 = _make_assistant(dbsession, user.id, first_name="Bot1")
        a2 = _make_assistant(dbsession, user.id, first_name="Bot2")
        for a in (a1, a2):
            dbsession.add(
                AssistantSecret(
                    user_id=user.id,
                    agent_id=a.agent_id,
                    secret_name="SHARED_NAME",
                    secret_value=f"val-{a.agent_id}",
                ),
            )
        dbsession.flush()

    def test_cascade_delete_on_assistant(self, dbsession: Session):
        user, _ba = _make_user_ba(dbsession, "sec-model-5")
        a = _make_assistant(dbsession, user.id)
        aid = a.agent_id
        dbsession.add(
            AssistantSecret(
                user_id=user.id,
                agent_id=aid,
                secret_name="GONE",
                secret_value="x",
            ),
        )
        dbsession.flush()
        dbsession.delete(a)
        dbsession.flush()
        remaining = (
            dbsession.query(AssistantSecret)
            .filter(AssistantSecret.agent_id == aid)
            .count()
        )
        assert remaining == 0

    def test_secret_value_not_nullable(self, dbsession: Session):
        user, _ba = _make_user_ba(dbsession, "sec-model-6")
        a = _make_assistant(dbsession, user.id)
        dbsession.add(
            AssistantSecret(
                user_id=user.id,
                agent_id=a.agent_id,
                secret_name="NULLVAL",
                secret_value=None,
            ),
        )
        with pytest.raises(IntegrityError):
            dbsession.flush()
        dbsession.rollback()


# ============================================================================
# 2. DAO Tests
# ============================================================================


class TestAssistantSecretDAO:

    def test_get_all_empty(self, dbsession: Session):
        user, _ba = _make_user_ba(dbsession, "sec-dao-1")
        a = _make_assistant(dbsession, user.id)
        dao = AssistantSecretDAO(dbsession)
        assert dao.get_all(a.agent_id) == {}

    def test_upsert_create(self, dbsession: Session):
        user, _ba = _make_user_ba(dbsession, "sec-dao-2")
        a = _make_assistant(dbsession, user.id)
        dao = AssistantSecretDAO(dbsession)
        dao.upsert(user.id, a.agent_id, "MY_TOKEN", "tok123")
        result = dao.get_all(a.agent_id)
        assert result == {"MY_TOKEN": "tok123"}

    def test_upsert_update(self, dbsession: Session):
        user, _ba = _make_user_ba(dbsession, "sec-dao-3")
        a = _make_assistant(dbsession, user.id)
        dao = AssistantSecretDAO(dbsession)
        dao.upsert(user.id, a.agent_id, "KEY", "old")
        dao.upsert(user.id, a.agent_id, "KEY", "new")
        assert dao.get(a.agent_id, "KEY") == "new"
        assert len(dao.get_all(a.agent_id)) == 1

    def test_get_single(self, dbsession: Session):
        user, _ba = _make_user_ba(dbsession, "sec-dao-4")
        a = _make_assistant(dbsession, user.id)
        dao = AssistantSecretDAO(dbsession)
        dao.upsert(user.id, a.agent_id, "A", "1")
        dao.upsert(user.id, a.agent_id, "B", "2")
        assert dao.get(a.agent_id, "A") == "1"
        assert dao.get(a.agent_id, "B") == "2"

    def test_get_single_missing(self, dbsession: Session):
        user, _ba = _make_user_ba(dbsession, "sec-dao-5")
        a = _make_assistant(dbsession, user.id)
        dao = AssistantSecretDAO(dbsession)
        assert dao.get(a.agent_id, "NOPE") is None

    def test_delete_one(self, dbsession: Session):
        user, _ba = _make_user_ba(dbsession, "sec-dao-6")
        a = _make_assistant(dbsession, user.id)
        dao = AssistantSecretDAO(dbsession)
        dao.upsert(user.id, a.agent_id, "KEEP", "v1")
        dao.upsert(user.id, a.agent_id, "DROP", "v2")
        assert dao.delete(a.agent_id, "DROP") is True
        assert dao.get(a.agent_id, "DROP") is None
        assert dao.get(a.agent_id, "KEEP") == "v1"

    def test_delete_nonexistent(self, dbsession: Session):
        user, _ba = _make_user_ba(dbsession, "sec-dao-7")
        a = _make_assistant(dbsession, user.id)
        dao = AssistantSecretDAO(dbsession)
        assert dao.delete(a.agent_id, "MISSING") is False

    def test_delete_all(self, dbsession: Session):
        user, _ba = _make_user_ba(dbsession, "sec-dao-8")
        a = _make_assistant(dbsession, user.id)
        dao = AssistantSecretDAO(dbsession)
        for name in ("X", "Y", "Z"):
            dao.upsert(user.id, a.agent_id, name, f"v-{name}")
        removed = dao.delete_all(a.agent_id)
        assert removed == 3
        assert dao.get_all(a.agent_id) == {}

    def test_delete_all_empty(self, dbsession: Session):
        user, _ba = _make_user_ba(dbsession, "sec-dao-9")
        a = _make_assistant(dbsession, user.id)
        dao = AssistantSecretDAO(dbsession)
        assert dao.delete_all(a.agent_id) == 0

    def test_multiple_secrets(self, dbsession: Session):
        user, _ba = _make_user_ba(dbsession, "sec-dao-10")
        a = _make_assistant(dbsession, user.id)
        dao = AssistantSecretDAO(dbsession)
        expected = {}
        for i in range(5):
            name = f"SECRET_{i}"
            value = f"value_{i}"
            dao.upsert(user.id, a.agent_id, name, value)
            expected[name] = value
        assert dao.get_all(a.agent_id) == expected


# ============================================================================
# 3. POST /assistant/{id}/secret
# ============================================================================


class TestCreateSecretEndpoint:

    @pytest.mark.anyio
    async def test_create_secret_success(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_infra,
    ):
        agent_id = await _create_assistant(client)
        resp = await client.post(
            f"/v0/assistant/{agent_id}/secret",
            json={"secret_name": "MY_TOKEN", "secret_value": "abc123"},
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        assert resp.json()["info"]["status"] == "created"

        dao = AssistantSecretDAO(dbsession)
        assert dao.get(agent_id, "MY_TOKEN") == "abc123"

    @pytest.mark.anyio
    async def test_create_duplicate_name_returns_409(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_infra,
    ):
        agent_id = await _create_assistant(client)
        await client.post(
            f"/v0/assistant/{agent_id}/secret",
            json={"secret_name": "DUP", "secret_value": "first"},
            headers=HEADERS,
        )
        resp = await client.post(
            f"/v0/assistant/{agent_id}/secret",
            json={"secret_name": "DUP", "secret_value": "second"},
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_409_CONFLICT

    @pytest.mark.anyio
    async def test_create_missing_secret_name(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_infra,
    ):
        agent_id = await _create_assistant(client)
        resp = await client.post(
            f"/v0/assistant/{agent_id}/secret",
            json={"secret_value": "abc"},
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    @pytest.mark.anyio
    async def test_create_missing_secret_value(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_infra,
    ):
        agent_id = await _create_assistant(client)
        resp = await client.post(
            f"/v0/assistant/{agent_id}/secret",
            json={"secret_name": "NOVALUE"},
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    @pytest.mark.anyio
    async def test_create_nonexistent_assistant_404(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_infra,
    ):
        resp = await client.post(
            "/v0/assistant/999999/secret",
            json={"secret_name": "X", "secret_value": "Y"},
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_404_NOT_FOUND


# ============================================================================
# 4. PUT /assistant/{id}/secret/{name}
# ============================================================================


class TestUpdateSecretEndpoint:

    @pytest.mark.anyio
    async def test_update_existing_secret(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_infra,
    ):
        agent_id = await _create_assistant(client)
        await client.post(
            f"/v0/assistant/{agent_id}/secret",
            json={"secret_name": "TOKEN", "secret_value": "old"},
            headers=HEADERS,
        )
        resp = await client.put(
            f"/v0/assistant/{agent_id}/secret/TOKEN",
            json={"secret_value": "new"},
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["info"]["status"] == "updated"

        dao = AssistantSecretDAO(dbsession)
        assert dao.get(agent_id, "TOKEN") == "new"

    @pytest.mark.anyio
    async def test_update_nonexistent_secret_404(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_infra,
    ):
        agent_id = await _create_assistant(client)
        resp = await client.put(
            f"/v0/assistant/{agent_id}/secret/NOPE",
            json={"secret_value": "val"},
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.anyio
    async def test_update_missing_value_422(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_infra,
    ):
        agent_id = await _create_assistant(client)
        resp = await client.put(
            f"/v0/assistant/{agent_id}/secret/X",
            json={},
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    @pytest.mark.anyio
    async def test_update_nonexistent_assistant_404(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_infra,
    ):
        resp = await client.put(
            "/v0/assistant/999999/secret/X",
            json={"secret_value": "v"},
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_404_NOT_FOUND


# ============================================================================
# 5. DELETE /assistant/{id}/secret/{name}
# ============================================================================


class TestDeleteSecretEndpoint:

    @pytest.mark.anyio
    async def test_delete_existing_secret(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_infra,
    ):
        agent_id = await _create_assistant(client)
        await client.post(
            f"/v0/assistant/{agent_id}/secret",
            json={"secret_name": "KILL", "secret_value": "v"},
            headers=HEADERS,
        )
        resp = await client.delete(
            f"/v0/assistant/{agent_id}/secret/KILL",
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["info"]["status"] == "deleted"

        dao = AssistantSecretDAO(dbsession)
        assert dao.get(agent_id, "KILL") is None

    @pytest.mark.anyio
    async def test_delete_nonexistent_secret_404(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_infra,
    ):
        agent_id = await _create_assistant(client)
        resp = await client.delete(
            f"/v0/assistant/{agent_id}/secret/MISSING",
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.anyio
    async def test_delete_nonexistent_assistant_404(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_infra,
    ):
        resp = await client.delete(
            "/v0/assistant/999999/secret/X",
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_404_NOT_FOUND


# ============================================================================
# 6. Admin GET includes secrets
# ============================================================================


class TestAdminSecretsInResponse:

    @pytest.mark.anyio
    async def test_admin_response_includes_secrets(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_infra,
    ):
        agent_id = await _create_assistant(client)

        # Store a secret
        await client.post(
            f"/v0/assistant/{agent_id}/secret",
            json={"secret_name": "MS_TOKEN", "secret_value": "tok-abc"},
            headers=HEADERS,
        )

        resp = await client.get(
            "/v0/admin/assistant",
            params={"agent_id": agent_id},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        assistants = resp.json()["info"]
        assert len(assistants) >= 1
        target = next(a for a in assistants if int(a["agent_id"]) == agent_id)
        assert target["secrets"] == {"MS_TOKEN": "tok-abc"}

    @pytest.mark.anyio
    async def test_admin_response_secrets_empty_dict(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_infra,
    ):
        agent_id = await _create_assistant(client)
        resp = await client.get(
            "/v0/admin/assistant",
            params={"agent_id": agent_id},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        target = next(a for a in resp.json()["info"] if int(a["agent_id"]) == agent_id)
        assert target["secrets"] == {}

    @pytest.mark.anyio
    async def test_admin_response_multiple_secrets(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_infra,
    ):
        agent_id = await _create_assistant(client)
        for name, val in [
            ("MICROSOFT_ACCESS_TOKEN", "at-xyz"),
            ("MICROSOFT_REFRESH_TOKEN", "rt-xyz"),
            ("MICROSOFT_TOKEN_EXPIRES_AT", "2099-01-01T00:00:00Z"),
        ]:
            await client.post(
                f"/v0/assistant/{agent_id}/secret",
                json={"secret_name": name, "secret_value": val},
                headers=HEADERS,
            )

        resp = await client.get(
            "/v0/admin/assistant",
            params={"agent_id": agent_id},
            headers=ADMIN_HEADERS,
        )
        target = next(a for a in resp.json()["info"] if int(a["agent_id"]) == agent_id)
        assert target["secrets"] == {
            "MICROSOFT_ACCESS_TOKEN": "at-xyz",
            "MICROSOFT_REFRESH_TOKEN": "rt-xyz",
            "MICROSOFT_TOKEN_EXPIRES_AT": "2099-01-01T00:00:00Z",
        }

    @pytest.mark.anyio
    async def test_admin_from_fields_secrets(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_infra,
    ):
        agent_id = await _create_assistant(client)
        await client.post(
            f"/v0/assistant/{agent_id}/secret",
            json={"secret_name": "KEY", "secret_value": "val"},
            headers=HEADERS,
        )

        resp = await client.get(
            "/v0/admin/assistant",
            params={"agent_id": agent_id, "from_fields": "agent_id,secrets"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        items = resp.json()["info"]
        target = next(i for i in items if int(i["agent_id"]) == agent_id)
        assert set(target.keys()) == {"agent_id", "secrets"}
        assert target["secrets"] == {"KEY": "val"}

    @pytest.mark.anyio
    async def test_non_admin_response_excludes_secrets(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_infra,
    ):
        agent_id = await _create_assistant(client)
        await client.post(
            f"/v0/assistant/{agent_id}/secret",
            json={"secret_name": "HIDDEN", "secret_value": "shhh"},
            headers=HEADERS,
        )

        resp = await client.get(
            "/v0/assistant",
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        assistants = resp.json()["info"]
        target = next(
            (a for a in assistants if int(a["agent_id"]) == agent_id),
            None,
        )
        assert target is not None
        assert target.get("secrets") is None


# ============================================================================
# 7. Cleanup integration
# ============================================================================


class TestSecretCleanupOnAssistantDeletion:

    @pytest.mark.anyio
    async def test_secrets_deleted_on_assistant_delete(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_infra,
    ):
        agent_id = await _create_assistant(client)
        await client.post(
            f"/v0/assistant/{agent_id}/secret",
            json={"secret_name": "S1", "secret_value": "v1"},
            headers=HEADERS,
        )
        await client.post(
            f"/v0/assistant/{agent_id}/secret",
            json={"secret_name": "S2", "secret_value": "v2"},
            headers=HEADERS,
        )

        dao = AssistantSecretDAO(dbsession)
        assert len(dao.get_all(agent_id)) == 2

        with patch(
            "orchestra.web.api.assistant.views.process_assistant_cleanup_tasks",
            new_callable=AsyncMock,
            return_value={
                "processed": 1,
                "completed": 1,
                "retried": 0,
                "failed": 0,
                "errors": [],
            },
        ):
            del_resp = await client.delete(
                f"/v0/assistant/{agent_id}",
                headers=HEADERS,
            )
            assert del_resp.status_code == status.HTTP_200_OK

        remaining = (
            dbsession.query(AssistantSecret)
            .filter(AssistantSecret.agent_id == agent_id)
            .count()
        )
        assert remaining == 0


# ============================================================================
# 8. End-to-end lifecycle
# ============================================================================


class TestSecretEndToEnd:

    @pytest.mark.anyio
    async def test_full_secret_lifecycle(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_infra,
    ):
        agent_id = await _create_assistant(client)

        # Create
        resp = await client.post(
            f"/v0/assistant/{agent_id}/secret",
            json={"secret_name": "LIFECYCLE", "secret_value": "v1"},
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK

        # Visible in admin GET
        admin_resp = await client.get(
            "/v0/admin/assistant",
            params={"agent_id": agent_id},
            headers=ADMIN_HEADERS,
        )
        target = next(
            a for a in admin_resp.json()["info"] if int(a["agent_id"]) == agent_id
        )
        assert target["secrets"]["LIFECYCLE"] == "v1"

        # Update
        resp = await client.put(
            f"/v0/assistant/{agent_id}/secret/LIFECYCLE",
            json={"secret_value": "v2"},
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK

        # Verify updated via admin
        admin_resp = await client.get(
            "/v0/admin/assistant",
            params={"agent_id": agent_id},
            headers=ADMIN_HEADERS,
        )
        target = next(
            a for a in admin_resp.json()["info"] if int(a["agent_id"]) == agent_id
        )
        assert target["secrets"]["LIFECYCLE"] == "v2"

        # Delete
        resp = await client.delete(
            f"/v0/assistant/{agent_id}/secret/LIFECYCLE",
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK

        # Gone from admin
        admin_resp = await client.get(
            "/v0/admin/assistant",
            params={"agent_id": agent_id},
            headers=ADMIN_HEADERS,
        )
        target = next(
            a for a in admin_resp.json()["info"] if int(a["agent_id"]) == agent_id
        )
        assert "LIFECYCLE" not in target["secrets"]

    @pytest.mark.anyio
    async def test_multiple_secret_operations(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_infra,
    ):
        """Simulate Communication's token storage pattern: create multiple
        secrets, update one, verify all via admin."""
        agent_id = await _create_assistant(client)

        secrets = {
            "MICROSOFT_ACCESS_TOKEN": "at-initial",
            "MICROSOFT_REFRESH_TOKEN": "rt-initial",
            "MICROSOFT_TOKEN_EXPIRES_AT": "2099-01-01",
        }
        for name, val in secrets.items():
            resp = await client.post(
                f"/v0/assistant/{agent_id}/secret",
                json={"secret_name": name, "secret_value": val},
                headers=HEADERS,
            )
            assert resp.status_code == status.HTTP_200_OK

        # Refresh: update access token
        resp = await client.put(
            f"/v0/assistant/{agent_id}/secret/MICROSOFT_ACCESS_TOKEN",
            json={"secret_value": "at-refreshed"},
            headers=HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK

        # Verify via admin
        admin_resp = await client.get(
            "/v0/admin/assistant",
            params={"agent_id": agent_id},
            headers=ADMIN_HEADERS,
        )
        target = next(
            a for a in admin_resp.json()["info"] if int(a["agent_id"]) == agent_id
        )
        assert target["secrets"]["MICROSOFT_ACCESS_TOKEN"] == "at-refreshed"
        assert target["secrets"]["MICROSOFT_REFRESH_TOKEN"] == "rt-initial"
        assert target["secrets"]["MICROSOFT_TOKEN_EXPIRES_AT"] == "2099-01-01"
