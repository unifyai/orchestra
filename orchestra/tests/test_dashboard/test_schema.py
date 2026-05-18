"""Pydantic schema validation tests for dashboard token request/response models."""

import pytest
from pydantic import ValidationError

from orchestra.web.api.dashboard.schema import (
    FilterBridgeRequest,
    JoinBridgeRequest,
    JoinBridgeResponse,
    JoinReduceBridgeRequest,
    JoinReduceBridgeResponse,
    ReduceBridgeRequest,
    ReduceBridgeResponse,
    RegisterTokenRequest,
    TokenResolutionResponse,
)


class TestRegisterTokenRequestValidation:
    """Pydantic validation for RegisterTokenRequest."""

    def test_valid_tile(self):
        req = RegisterTokenRequest(
            token="abc123xyz456",
            entity_type="tile",
            context_name="my-project/Dashboards/Tiles",
            project_name="my-project",
        )
        assert req.entity_type == "tile"
        assert req.project_name == "my-project"

    def test_valid_dashboard(self):
        req = RegisterTokenRequest(
            token="tkn_dash_001",
            entity_type="dashboard",
            context_name="my-project/Dashboards/Layouts",
            project_name="my-project",
        )
        assert req.entity_type == "dashboard"

    def test_invalid_entity_type(self):
        with pytest.raises(ValidationError):
            RegisterTokenRequest(
                token="abc123xyz456",
                entity_type="widget",
                context_name="some/path",
                project_name="some-project",
            )

    def test_empty_token_rejected(self):
        with pytest.raises(ValidationError):
            RegisterTokenRequest(
                token="",
                entity_type="tile",
                context_name="some/path",
                project_name="some-project",
            )

    def test_token_too_long_rejected(self):
        with pytest.raises(ValidationError):
            RegisterTokenRequest(
                token="a" * 13,
                entity_type="tile",
                context_name="some/path",
                project_name="some-project",
            )

    def test_empty_context_name_rejected(self):
        with pytest.raises(ValidationError):
            RegisterTokenRequest(
                token="abc123xyz456",
                entity_type="tile",
                context_name="",
                project_name="some-project",
            )

    def test_missing_project_name_rejected(self):
        with pytest.raises(ValidationError):
            RegisterTokenRequest(
                token="abc123xyz456",
                entity_type="tile",
                context_name="some/path",
            )

    def test_empty_project_name_rejected(self):
        with pytest.raises(ValidationError):
            RegisterTokenRequest(
                token="abc123xyz456",
                entity_type="tile",
                context_name="some/path",
                project_name="",
            )


class TestFilterBridgeRequestValidation:
    """Pydantic validation for FilterBridgeRequest."""

    def test_minimal_request(self):
        req = FilterBridgeRequest(context="my-project/Logs")
        assert req.context == "my-project/Logs"
        assert req.filter_expr is None
        assert req.from_fields is None
        assert req.limit is None

    def test_full_request(self):
        req = FilterBridgeRequest(
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

    def test_console_alias_filter(self):
        """Console proxy sends 'filter' instead of 'filter_expr'."""
        req = FilterBridgeRequest(context="proj/Logs", filter="model == 'gpt-4'")
        assert req.filter_expr == "model == 'gpt-4'"

    def test_console_alias_columns(self):
        """Console proxy sends 'columns' instead of 'from_fields'."""
        req = FilterBridgeRequest(context="proj/Logs", columns="model&latency")
        assert req.from_fields == "model&latency"

    def test_console_alias_exclude_columns(self):
        """Console proxy sends 'exclude_columns' instead of 'exclude_fields'."""
        req = FilterBridgeRequest(context="proj/Logs", exclude_columns="raw")
        assert req.exclude_fields == "raw"

    def test_canonical_names_take_precedence(self):
        """When both alias and canonical are sent, canonical wins."""
        req = FilterBridgeRequest(
            context="proj/Logs",
            filter_expr="canonical",
            filter="alias",
        )
        assert req.filter_expr == "canonical"

    def test_all_console_aliases_together(self):
        req = FilterBridgeRequest(
            context="proj/Logs",
            filter="x == 1",
            columns="a&b",
            exclude_columns="c",
        )
        assert req.filter_expr == "x == 1"
        assert req.from_fields == "a&b"
        assert req.exclude_fields == "c"


class TestTokenResolutionResponse:
    """Pydantic validation for TokenResolutionResponse."""

    def test_without_org(self):
        resp = TokenResolutionResponse(
            entity_type="tile",
            context_name="proj/Dashboards/Tiles",
            user_id="uid_123",
            project_id=42,
            project_name="proj",
        )
        assert resp.organization_id is None

    def test_with_org(self):
        resp = TokenResolutionResponse(
            entity_type="dashboard",
            context_name="proj/Dashboards/Layouts",
            user_id="uid_456",
            organization_id=7,
            project_id=99,
            project_name="proj",
        )
        assert resp.organization_id == 7


# ===========================================================================
# ReduceBridgeRequest
# ===========================================================================


class TestReduceBridgeRequestValidation:
    def test_minimal_single_key(self):
        req = ReduceBridgeRequest(
            context="proj/Logs",
            metric="count",
            columns="score",
        )
        assert req.columns == "score"
        assert req.metric == "count"
        assert req.filter_expr is None
        assert req.group_by is None

    def test_multi_key(self):
        req = ReduceBridgeRequest(
            context="proj/Logs",
            metric="sum",
            columns=["cost", "latency"],
        )
        assert req.columns == ["cost", "latency"]

    def test_with_group_by(self):
        req = ReduceBridgeRequest(
            context="proj/Logs",
            metric="mean",
            columns="score",
            group_by=["model"],
        )
        assert req.group_by == ["model"]

    def test_with_filter_expr(self):
        req = ReduceBridgeRequest(
            context="proj/Logs",
            metric="max",
            columns="latency",
            filter_expr="model == 'gpt-4'",
        )
        assert req.filter_expr == "model == 'gpt-4'"

    def test_console_alias_filter(self):
        req = ReduceBridgeRequest(
            context="proj/Logs",
            metric="count",
            columns="score",
            filter="model == 'gpt-4'",
        )
        assert req.filter_expr == "model == 'gpt-4'"

    def test_missing_metric_rejected(self):
        with pytest.raises(ValidationError):
            ReduceBridgeRequest(context="proj/Logs", columns="score")

    def test_missing_columns_rejected(self):
        with pytest.raises(ValidationError):
            ReduceBridgeRequest(context="proj/Logs", metric="count")

    def test_missing_context_rejected(self):
        with pytest.raises(ValidationError):
            ReduceBridgeRequest(metric="count", columns="score")


# ===========================================================================
# JoinBridgeRequest
# ===========================================================================


class TestJoinBridgeRequestValidation:
    def test_minimal(self):
        req = JoinBridgeRequest(
            tables=["proj/users", "proj/orders"],
            join_expr="A.user_id == B.user_id",
        )
        assert req.tables == ["proj/users", "proj/orders"]
        assert req.mode == "inner"
        assert req.result_limit == 100
        assert req.result_offset == 0
        assert req.select is None

    def test_full(self):
        req = JoinBridgeRequest(
            tables=["proj/users", "proj/orders"],
            join_expr="A.user_id == B.user_id",
            select={"A.name": "user_name", "B.amount": "order_amount"},
            mode="left",
            left_where="status == 'active'",
            right_where="amount > 0",
            result_where="order_amount > 100",
            result_limit=50,
            result_offset=10,
        )
        assert req.mode == "left"
        assert req.result_limit == 50
        assert req.left_where == "status == 'active'"

    def test_wrong_table_count_rejected(self):
        with pytest.raises(ValidationError):
            JoinBridgeRequest(
                tables=["proj/users"],
                join_expr="A.id == B.id",
            )

    def test_three_tables_rejected(self):
        with pytest.raises(ValidationError):
            JoinBridgeRequest(
                tables=["a", "b", "c"],
                join_expr="A.id == B.id",
            )

    def test_invalid_mode_rejected(self):
        with pytest.raises(ValidationError):
            JoinBridgeRequest(
                tables=["a", "b"],
                join_expr="A.id == B.id",
                mode="cross",
            )

    def test_empty_join_expr_rejected(self):
        with pytest.raises(ValidationError):
            JoinBridgeRequest(
                tables=["a", "b"],
                join_expr="",
            )

    def test_limit_bounds(self):
        with pytest.raises(ValidationError):
            JoinBridgeRequest(
                tables=["a", "b"],
                join_expr="A.id == B.id",
                result_limit=0,
            )
        with pytest.raises(ValidationError):
            JoinBridgeRequest(
                tables=["a", "b"],
                join_expr="A.id == B.id",
                result_limit=1001,
            )


# ===========================================================================
# JoinReduceBridgeRequest
# ===========================================================================


class TestJoinReduceBridgeRequestValidation:
    def test_minimal(self):
        req = JoinReduceBridgeRequest(
            tables=["proj/users", "proj/orders"],
            join_expr="A.user_id == B.user_id",
            metric="sum",
            columns="amount",
        )
        assert req.metric == "sum"
        assert req.columns == "amount"
        assert req.group_by is None

    def test_with_group_by(self):
        req = JoinReduceBridgeRequest(
            tables=["proj/users", "proj/orders"],
            join_expr="A.user_id == B.user_id",
            metric="mean",
            columns=["amount", "cost"],
            group_by=["category"],
        )
        assert req.group_by == ["category"]
        assert req.columns == ["amount", "cost"]

    def test_missing_metric_rejected(self):
        with pytest.raises(ValidationError):
            JoinReduceBridgeRequest(
                tables=["a", "b"],
                join_expr="A.id == B.id",
                columns="amount",
            )

    def test_missing_columns_rejected(self):
        with pytest.raises(ValidationError):
            JoinReduceBridgeRequest(
                tables=["a", "b"],
                join_expr="A.id == B.id",
                metric="sum",
            )

    def test_wrong_table_count_rejected(self):
        with pytest.raises(ValidationError):
            JoinReduceBridgeRequest(
                tables=["only_one"],
                join_expr="A.id == B.id",
                metric="count",
                columns="x",
            )


# ===========================================================================
# Response models
# ===========================================================================


class TestReduceBridgeResponse:
    def test_scalar_result(self):
        resp = ReduceBridgeResponse(result=42.5)
        assert resp.result == 42.5

    def test_dict_result(self):
        resp = ReduceBridgeResponse(result={"score": 4.5, "cost": 0.003})
        assert resp.result["score"] == 4.5

    def test_none_result(self):
        resp = ReduceBridgeResponse(result=None)
        assert resp.result is None


class TestJoinBridgeResponse:
    def test_basic(self):
        resp = JoinBridgeResponse(
            rows=[{"name": "Alice", "amount": 100}],
            total_count=1,
        )
        assert len(resp.rows) == 1
        assert resp.total_count == 1

    def test_empty(self):
        resp = JoinBridgeResponse(rows=[], total_count=0)
        assert resp.rows == []


class TestJoinReduceBridgeResponse:
    def test_scalar_result(self):
        resp = JoinReduceBridgeResponse(result=60)
        assert resp.result == 60

    def test_dict_result(self):
        resp = JoinReduceBridgeResponse(result={"NYC": 40, "LA": 20})
        assert resp.result["NYC"] == 40

    def test_none_result(self):
        resp = JoinReduceBridgeResponse(result=None)
        assert resp.result is None
