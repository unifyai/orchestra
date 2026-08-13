"""Tenant-scoping guard for the integration DAO.

Covers the fix for the cross-tenant isolation flaws: the integration DAO must
scope every connection/audit lookup to the authenticated request principal, and
fail closed when no principal is in context. Also verifies the async-generator
binder dependency propagates the principal into a sync path operation and resets
it after the request (no leak between callers).
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI, Request
from starlette.testclient import TestClient

from orchestra.db.dao.integration_provider_dao import IntegrationProviderDAO
from orchestra.db.request_principal import (
    RequestPrincipal,
    get_request_principal,
    system_principal,
    use_request_principal,
)
from orchestra.web.api.dependencies import bind_integration_principal


class _FakeQuery:
    """Records .filter() calls so we can assert whether scoping was applied."""

    def __init__(self) -> None:
        self.filters: list = []

    def filter(self, *clauses):
        self.filters.extend(clauses)
        return self


def _dao() -> IntegrationProviderDAO:
    return IntegrationProviderDAO(session=None)  # guards never touch the session


def test_no_principal_fails_closed() -> None:
    with pytest.raises(PermissionError):
        _dao()._scope_connections(_FakeQuery())
    with pytest.raises(PermissionError):
        _dao()._scope_audits(_FakeQuery())


def test_system_principal_bypasses_scoping() -> None:
    with system_principal():
        q = _FakeQuery()
        assert _dao()._scope_connections(q) is q
        assert q.filters == []  # no tenant predicate added


def test_tenant_principal_adds_scope() -> None:
    with use_request_principal(RequestPrincipal(user_id="u-1")):
        q = _FakeQuery()
        _dao()._scope_connections(q)
        assert len(q.filters) == 1  # an OR(...) tenant clause was applied


def test_identity_less_principal_fails_closed() -> None:
    # Authenticated but carrying neither user nor org id -> must not match all.
    with use_request_principal(RequestPrincipal()):
        with pytest.raises(PermissionError):
            _dao()._scope_connections(_FakeQuery())


def test_binder_propagates_to_sync_endpoint_and_resets() -> None:
    app = FastAPI()

    def fake_auth(request: Request):  # stands in for auth_api_key
        request.state.user_id = "user-abc"
        request.state.organization_id = None
        request.state.is_system_api_key = False

    @app.get(
        "/scoped",
        dependencies=[Depends(fake_auth), Depends(bind_integration_principal)],
    )
    def scoped():  # sync path operation, like the real integration views
        p = get_request_principal()
        return {
            "user_id": p.user_id if p else None,
            "is_system": p.is_system if p else None,
        }

    @app.get("/unscoped")
    def unscoped():  # no binder -> principal must be absent (no leak)
        return {"principal": get_request_principal()}

    client = TestClient(app)
    assert client.get("/scoped").json() == {"user_id": "user-abc", "is_system": False}
    assert client.get("/unscoped").json() == {"principal": None}
    assert client.get("/scoped").json() == {"user_id": "user-abc", "is_system": False}


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-q"]))
