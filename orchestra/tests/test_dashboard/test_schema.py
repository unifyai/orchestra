"""Pydantic schema validation tests for dashboard token request/response models."""

import pytest


class TestRegisterTokenRequestValidation:
    """Pydantic validation for RegisterTokenRequest."""

    def test_valid_tile(self):
        from orchestra.web.api.dashboard.schema import RegisterTokenRequest

        req = RegisterTokenRequest(
            token="abc123xyz456",
            entity_type="tile",
            context_name="my-project/Dashboards/Tiles",
            project_name="my-project",
        )
        assert req.entity_type == "tile"
        assert req.project_name == "my-project"

    def test_valid_dashboard(self):
        from orchestra.web.api.dashboard.schema import RegisterTokenRequest

        req = RegisterTokenRequest(
            token="tkn_dash_001",
            entity_type="dashboard",
            context_name="my-project/Dashboards/Layouts",
            project_name="my-project",
        )
        assert req.entity_type == "dashboard"

    def test_invalid_entity_type(self):
        from pydantic import ValidationError

        from orchestra.web.api.dashboard.schema import RegisterTokenRequest

        with pytest.raises(ValidationError):
            RegisterTokenRequest(
                token="abc123xyz456",
                entity_type="widget",
                context_name="some/path",
                project_name="some-project",
            )

    def test_empty_token_rejected(self):
        from pydantic import ValidationError

        from orchestra.web.api.dashboard.schema import RegisterTokenRequest

        with pytest.raises(ValidationError):
            RegisterTokenRequest(
                token="",
                entity_type="tile",
                context_name="some/path",
                project_name="some-project",
            )

    def test_token_too_long_rejected(self):
        from pydantic import ValidationError

        from orchestra.web.api.dashboard.schema import RegisterTokenRequest

        with pytest.raises(ValidationError):
            RegisterTokenRequest(
                token="a" * 13,
                entity_type="tile",
                context_name="some/path",
                project_name="some-project",
            )

    def test_empty_context_name_rejected(self):
        from pydantic import ValidationError

        from orchestra.web.api.dashboard.schema import RegisterTokenRequest

        with pytest.raises(ValidationError):
            RegisterTokenRequest(
                token="abc123xyz456",
                entity_type="tile",
                context_name="",
                project_name="some-project",
            )

    def test_missing_project_name_rejected(self):
        from pydantic import ValidationError

        from orchestra.web.api.dashboard.schema import RegisterTokenRequest

        with pytest.raises(ValidationError):
            RegisterTokenRequest(
                token="abc123xyz456",
                entity_type="tile",
                context_name="some/path",
            )

    def test_empty_project_name_rejected(self):
        from pydantic import ValidationError

        from orchestra.web.api.dashboard.schema import RegisterTokenRequest

        with pytest.raises(ValidationError):
            RegisterTokenRequest(
                token="abc123xyz456",
                entity_type="tile",
                context_name="some/path",
                project_name="",
            )


class TestDataBridgeRequestValidation:
    """Pydantic validation for DataBridgeRequest."""

    def test_minimal_request(self):
        from orchestra.web.api.dashboard.schema import DataBridgeRequest

        req = DataBridgeRequest(context="my-project/Logs")
        assert req.context == "my-project/Logs"
        assert req.filter_expr is None
        assert req.from_fields is None
        assert req.limit is None

    def test_full_request(self):
        from orchestra.web.api.dashboard.schema import DataBridgeRequest

        req = DataBridgeRequest(
            context="proj/Logs",
            filter_expr="model == 'gpt-4'",
            from_fields="model&latency&cost",
            exclude_fields="raw_data",
            sorting='{"cost": "ascending"}',
            group_by=["model"],
            limit=100,
            offset=50,
            column_context="sub/path",
            randomize=False,
        )
        assert req.from_fields == "model&latency&cost"
        assert req.limit == 100
        assert req.offset == 50
        assert req.group_by == ["model"]


class TestTokenResolutionResponse:
    """Pydantic validation for TokenResolutionResponse."""

    def test_without_org(self):
        from orchestra.web.api.dashboard.schema import TokenResolutionResponse

        resp = TokenResolutionResponse(
            entity_type="tile",
            context_name="proj/Dashboards/Tiles",
            user_id="uid_123",
            project_id=42,
        )
        assert resp.organization_id is None

    def test_with_org(self):
        from orchestra.web.api.dashboard.schema import TokenResolutionResponse

        resp = TokenResolutionResponse(
            entity_type="dashboard",
            context_name="proj/Dashboards/Layouts",
            user_id="uid_456",
            organization_id=7,
            project_id=99,
        )
        assert resp.organization_id == 7
