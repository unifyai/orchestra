import json

import pytest
from httpx import Request

from . import HEADERS

PROJECT = "perf-project"


@pytest.fixture(scope="module")
def _engine(_engine_session):
    import orchestra.web.lifetime as lifetime

    lifetime._engine = _engine_session
    yield _engine_session


### GET TESTS ###


@pytest.mark.anyio
@pytest.mark.performance
@pytest.mark.usefixtures("large_log_dataset")
async def test_basic_retrieval_performance(timed_client):
    """Test basic retrieval performance with large dataset."""
    params = {"project": PROJECT, "context": "ctx_big", "limit": 100}

    response = await timed_client.get(
        "/v0/logs",
        params=params,
        headers=HEADERS,
    )

    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.performance
@pytest.mark.usefixtures("large_log_dataset")
async def test_numeric_filter_performance(timed_client):
    """Test numeric filter performance with large dataset."""
    params = {
        "project": PROJECT,
        "context": "ctx_big",
        "filter_expr": "int_field > 1000",
        "limit": 100,
    }

    response = await timed_client.get(
        "/v0/logs",
        params=params,
        headers=HEADERS,
    )

    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.performance
@pytest.mark.usefixtures("large_log_dataset")
async def test_substring_match_performance(timed_client):
    """Test substring match performance with large dataset."""
    params = {
        "project": PROJECT,
        "context": "ctx_big",
        "filter_expr": "'ABC1' in str_field",
        "limit": 100,
    }

    response = await timed_client.get(
        "/v0/logs",
        params=params,
        headers=HEADERS,
    )

    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.performance
@pytest.mark.usefixtures("large_log_dataset")
async def test_field_filtering_performance(timed_client):
    """Test field filtering performance with large dataset."""
    params = {
        "project": PROJECT,
        "context": "ctx_big",
        "from_fields": "int_field&str_field",
        "exclude_ids": "1&2&3",
        "limit": 100,
    }

    response = await timed_client.get(
        "/v0/logs",
        params=params,
        headers=HEADERS,
    )

    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.performance
@pytest.mark.usefixtures("large_log_dataset")
async def test_distinct_grouping_performance(timed_client):
    """Test distinct grouping performance with large dataset."""
    params = {
        "project": PROJECT,
        "context": "ctx_big",
        "key": "int_field",
        "group_threshold": 10,
        "limit": 100,
    }

    response = await timed_client.get(
        "/v0/logs/groups",
        params=params,
        headers=HEADERS,
    )

    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.performance
@pytest.mark.usefixtures("large_log_dataset")
async def test_group_by_sorting_performance(timed_client):
    """Test group-by with sorting performance with large dataset."""
    group_sorting = {"field": "entries/int_field", "order": "desc"}

    params = {
        "project": PROJECT,
        "context": "ctx_big",
        "group_by": ["entries/int_field"],
        "group_sorting": json.dumps(group_sorting),
        "limit": 100,
    }

    response = await timed_client.get(
        "/v0/logs",
        params=params,
        headers=HEADERS,
    )

    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.performance
@pytest.mark.usefixtures("large_log_dataset")
async def test_sorting_performance(timed_client):
    params = {
        "project": PROJECT,
        "sorting": json.dumps({"entries/int_field": "desc"}),
        "limit": 200,
    }
    response = await timed_client.get(
        "/v0/logs",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.performance
@pytest.mark.usefixtures("large_log_dataset")
async def test_nested_grouping_performance(timed_client):
    params = {
        "project": PROJECT,
        "context": "ctx_big",
        "group_by": json.dumps(["entries/int_field", "entries/float_field"]),
        "group_depth": 0,
        "limit": 100,
    }
    response = await timed_client.get(
        "/v0/logs",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.performance
@pytest.mark.usefixtures("large_log_dataset")
async def test_metrics_group_performance(timed_client):
    params = {
        "project": PROJECT,
        "context": "ctx_big",
        "key": json.dumps(["entries/int_field", "entries/float_field"]),
    }
    response = await timed_client.get(
        "/v0/logs/metric/mean",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.performance
@pytest.mark.usefixtures("large_log_dataset")
async def test_multiple_metric_verbs(timed_client):
    """Test multiple metric verbs (min, max, sum) performance."""
    for verb in ("min", "max", "sum"):
        params = {
            "project": PROJECT,
            "context": "ctx_big",
            "key": json.dumps(["entries/int_field"]),
        }
        response = await timed_client.get(
            f"/v0/logs/metric/{verb}",
            params=params,
            headers=HEADERS,
        )
        assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.performance
@pytest.mark.usefixtures("large_log_dataset")
async def test_multi_key_sorting_and_pagination(timed_client):
    """Test compound sorting on two fields with pagination."""
    sorting = {"int_field": "ascending", "float_field": "descending"}
    params = {
        "project": PROJECT,
        "context": "ctx_big",
        "sorting": json.dumps(sorting),
        "limit": 50,
        "offset": 100,
    }
    response = await timed_client.get(
        "/v0/logs",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.performance
@pytest.mark.usefixtures("large_log_dataset")
async def test_boolean_filter_combo(timed_client):
    """Test boolean + categorical + numeric filter combination."""
    filter_expr = (
        "bool_field == True and " "category in ['alpha','beta'] and " "int_field < 5000"
    )
    params = {
        "project": PROJECT,
        "context": "ctx_big",
        "filter_expr": filter_expr,
        "limit": 100,
    }
    response = await timed_client.get(
        "/v0/logs",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.performance
@pytest.mark.usefixtures("large_log_dataset")
async def test_date_range_filter_performance(timed_client):
    params = {
        "project": PROJECT,
        "context": "ctx_big",
        "filter_expr": "ts_field >= '2023-01-01T00:00:00' and ts_field < '2023-01-05T00:00:00'",
        "limit": 100,
    }
    response = await timed_client.get(
        "/v0/logs",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.performance
@pytest.mark.usefixtures("large_log_dataset")
async def test_list_membership_filter_performance(timed_client):
    params = {
        "project": PROJECT,
        "context": "ctx_big",
        "filter_expr": "'api' in tags",
        "limit": 100,
    }
    response = await timed_client.get(
        "/v0/logs",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.performance
@pytest.mark.usefixtures("large_log_dataset")
async def test_dict_subscript_filter_performance(timed_client):
    params = {
        "project": PROJECT,
        "context": "ctx_big",
        "filter_expr": "dict_field['r'] > 50",
        "limit": 100,
    }
    response = await timed_client.get(
        "/v0/logs",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.performance
@pytest.mark.usefixtures("large_log_dataset")
async def test_get_fields_performance(timed_client):
    params = {"project": PROJECT, "context": "ctx_big"}
    response = await timed_client.get(
        "/v0/logs/fields",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.performance
@pytest.mark.usefixtures("large_log_dataset")
async def test_latest_timestamp_performance(timed_client):
    params = {"project": PROJECT, "context": "ctx_big"}
    response = await timed_client.get(
        "/v0/logs/latest_timestamp",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.performance
@pytest.mark.usefixtures("large_log_dataset")
async def test_complex_filter_performance(timed_client):
    """Test complex filter performance with large dataset."""
    filter_expr = (
        "(int_field > 1000 and int_field < 5000) and "
        "(str_field.startswith('ABC') or str_field.endswith('XYZ')) and "
        "bool_field == True"
    )
    params = {
        "project": PROJECT,
        "context": "ctx_big",
        "filter_expr": filter_expr,
        "limit": 100,
    }
    response = await timed_client.get(
        "/v0/logs",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.performance
@pytest.mark.usefixtures("large_log_dataset")
async def test_metric_sum_performance(timed_client):
    """Test metric sum performance."""
    params = {"project": PROJECT, "context": "ctx_big", "key": "entries/int_field"}
    response = await timed_client.get(
        "/v0/logs/metric/sum",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.performance
@pytest.mark.usefixtures("large_log_dataset")
async def test_metric_max_performance(timed_client):
    """Test metric max performance."""
    params = {"project": PROJECT, "context": "ctx_big", "key": "entries/float_field"}
    response = await timed_client.get(
        "/v0/logs/metric/max",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.performance
@pytest.mark.usefixtures("large_log_dataset")
async def test_edge_case_sorting_performance(timed_client):
    """Test edge case sorting performance."""
    params = {
        "project": PROJECT,
        "context": "ctx_big",
        "sorting": json.dumps({"int_field": "descending"}),
        # No limit specified - should return all logs
    }
    response = await timed_client.get(
        "/v0/logs",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200


### POST/PUT/DELETE TESTS ###


@pytest.mark.anyio
@pytest.mark.performance
@pytest.mark.usefixtures("large_log_dataset")
async def test_rename_string_field_performance(timed_client):
    """Test renaming string field performance."""
    payload = {
        "project": PROJECT,
        "context": "ctx_big",
        "old_field_name": "str_field",
        "new_field_name": "perf_str_field",
    }
    response = await timed_client.post(
        "/v0/logs/rename_field",
        json=payload,
        headers=HEADERS,
    )
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.performance
@pytest.mark.usefixtures("large_log_dataset")
async def test_derived_entry_performance(timed_client):
    # fetch 10 logs
    fetch_params = {"project": PROJECT, "limit": 10}
    fetch_resp = await timed_client.get(
        "/v0/logs",
        params=fetch_params,
        headers=HEADERS,
    )
    assert fetch_resp.status_code == 200

    # create derived entry doubling int_field
    payload = {
        "project": PROJECT,
        "context": "ctx_big",
        "key": "double_int_field",
        "equation": "{x:entries/int_field} * 2",
        "referenced_logs": {"x": {"filter_expr": "", "context": "ctx_big"}},
    }
    derive_resp = await timed_client.post(
        "/v0/logs/derived",
        json=payload,
        headers=HEADERS,
    )
    assert derive_resp.status_code == 200


@pytest.mark.anyio
@pytest.mark.performance
@pytest.mark.usefixtures("large_log_dataset")
async def test_join_logs_performance(timed_client):
    payload = {
        "project": PROJECT,
        "pair_of_args": [{"context": "ctx_big"}, {"context": "ctx_b"}],
        "join_expr": "A.category == B.category",
        "mode": "inner",
        "new_context": "joined_perf_context",
        "columns": ["A.int_field", "A.float_field", "B.category"],
    }
    response = await timed_client.post(
        "/v0/logs/join",
        json=payload,
        headers=HEADERS,
    )
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.performance
@pytest.mark.usefixtures("large_log_dataset")
async def test_rename_field_performance(timed_client):
    payload = {
        "project": PROJECT,
        "context": "ctx_big",
        "old_field_name": "int_field",
        "new_field_name": "perf_int_field",
    }
    response = await timed_client.post(
        "/v0/logs/rename_field",
        json=payload,
        headers=HEADERS,
    )
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.performance
@pytest.mark.usefixtures("large_log_dataset")
async def test_update_derived_performance(timed_client):
    # First create a derived entry
    create_payload = {
        "project": PROJECT,
        "key": "double_int_field",
        "equation": "{x:entries/int_field} * 2",
        "referenced_logs": {"x": {"filter_expr": ""}},
    }
    create_response = await timed_client.post(
        "/v0/logs/derived",
        json=create_payload,
        headers=HEADERS,
    )
    assert create_response.status_code == 200

    # Now update the derived entry
    update_payload = {
        "project": PROJECT,
        "target_derived_logs": {"from_fields": "double_int_field"},
        "key": "double_int_field",
        "equation": "{x:entries/int_field} * 3",
        "referenced_logs": {"x": {"filter_expr": ""}},
    }
    update_response = await timed_client.put(
        "/v0/logs/derived",
        json=update_payload,
        headers=HEADERS,
    )
    assert update_response.status_code == 200


@pytest.mark.anyio
@pytest.mark.performance
@pytest.mark.usefixtures("large_log_dataset")
async def test_bulk_delete_logs_performance(timed_client):
    """Test bulk deleting logs performance."""
    # First get some log IDs
    params = {"project": PROJECT, "context": "ctx_big", "limit": 5}
    response = await timed_client.get(
        "/v0/logs",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200
    log_ids = [log["id"] for log in response.json()["logs"]]

    # Delete the logs
    payload = {
        "project": PROJECT,
        "context": "ctx_big",
        "ids_and_fields": [(log_ids, None)],
    }
    request = Request(
        "DELETE",
        str(timed_client.base_url) + "/v0/logs",
        json=payload,
        headers=HEADERS,
    )
    response = await timed_client.send(request)
    assert response.status_code == 200
