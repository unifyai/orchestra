import base64
import json
import os
from datetime import datetime, timezone

import cv2
import pytest
from httpx import AsyncClient

from . import (
    HEADERS,
    HEADERS_2,
    _create_log,
    _create_project,
    _create_several_logs,
    _get_log,
    _update_logs,
)


@pytest.mark.anyio
async def test_get_log(client: AsyncClient, use_jsonb_mode):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)
    log_response = await _create_log(client, project_name)
    log_id = log_response.json()["log_event_ids"][0]

    # fetch the log
    response = await _get_log(client, project_name, log_id)

    assert response.status_code == 200, response.json()
    assert "logs" in response.json()
    assert len(response.json()["logs"]) == 1
    assert isinstance(response.json()["logs"][0]["ts"], str)
    assert isinstance(response.json()["logs"][0]["entries"]["a/b/c/input"], str)


@pytest.mark.anyio
async def test_get_logs(client: AsyncClient, use_jsonb_mode):
    project_name = "eval-project"
    # create the same project with another user to ensure the correct one
    # is fetched
    _ = await _create_project(client, project_name, user=2)
    _ = await _create_project(client, project_name, user=1)
    _ = await _create_log(client, project_name, user=1)

    # fetch entries for the project
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    assert isinstance(response.json(), dict)
    assert isinstance(response.json()["logs"], list)
    assert isinstance(response.json()["logs"][0]["ts"], str)
    assert isinstance(
        response.json()["logs"][0]["entries"]["a/b/c/boolean_input"],
        bool,
    )
    assert isinstance(
        response.json()["logs"][0]["entries"]["a/b/c/numeric_input"],
        float,
    )

    # assert the field ordering is correct
    assert (
        json.dumps([list(lg["entries"].keys()) for lg in response.json()["logs"]])
        == '[["a/b/c/input", "a/b/c/boolean_input", "a/b/c/numeric_input"]]'
    )

    # fetch entries for the empty project
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS_2,
    )

    assert response.status_code == 200, response.json()
    assert isinstance(response.json()["logs"], list)
    assert len(response.json()["logs"]) == 0


@pytest.mark.anyio
async def test_get_logs_project_not_found(client: AsyncClient, use_jsonb_mode):
    project_name = "non_existent_project"

    # This should return 404 as the project does not exist
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )

    assert response.status_code == 404, response.json()
    assert response.json() == {
        "detail": f"Project {project_name} not found.",
    }


@pytest.mark.anyio
async def test_get_entries(client: AsyncClient, use_jsonb_mode):
    project_name = "eval-project"
    # create the same project with another user to ensure the correct one
    # is fetched
    _ = await _create_project(client, project_name)
    _ = await _create_log(client, project_name)

    # fetch all entries for the project with context filter
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
        params={"column_context": "a/b"},
    )

    assert response.status_code == 200, response.json()
    assert isinstance(response.json(), dict)
    assert isinstance(response.json()["logs"], list)
    assert isinstance(response.json()["logs"][0]["ts"], str)
    assert isinstance(
        response.json()["logs"][0]["entries"]["c/boolean_input"],
        bool,
    )
    assert isinstance(
        response.json()["logs"][0]["entries"]["c/numeric_input"],
        float,
    )


@pytest.mark.anyio
async def test_get_logs_from_ids(client: AsyncClient, use_jsonb_mode):
    project_name = "eval-project"
    # create the same project with another user to ensure the correct one
    # is fetched
    _ = await _create_project(client, project_name)
    _ = await _create_several_logs(client, project_name)

    # fetch entries for the project
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        params={"return_ids_only": True},
        headers=HEADERS,
    )
    ids = response.json()
    from_ids = ids[0:4]

    # fetch entries for the project
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        params={"from_ids": "&".join([str(i) for i in from_ids])},
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    response = response.json()
    logs = response["logs"]
    assert len(logs) == 4
    assert len(logs) == response["count"]
    assert [log["id"] for log in logs] == [i for i in ids if i in from_ids]


@pytest.mark.anyio
async def test_get_logs_excluding_ids(client: AsyncClient, use_jsonb_mode):
    project_name = "eval-project"
    # create the same project with another user to ensure the correct one
    # is fetched
    _ = await _create_project(client, project_name)
    _ = await _create_several_logs(client, project_name)

    # fetch entries for the project
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        params={"return_ids_only": True},
        headers=HEADERS,
    )
    ids = response.json()
    exclude_ids = ids[0:4]

    # fetch entries for the project
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        params={"exclude_ids": "&".join([str(i) for i in exclude_ids])},
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    response = response.json()
    logs = response["logs"]
    assert len(logs) == 3
    assert len(logs) == response["count"]
    assert [log["id"] for log in logs] == [i for i in ids if i not in exclude_ids]


@pytest.mark.anyio
async def test_get_logs_from_fields(client: AsyncClient, use_jsonb_mode):
    """Test getting logs from specific fields."""
    project_name = "eval-project"
    # create the same project with another user to ensure the correct one
    # is fetched
    _ = await _create_project(client, project_name)
    _ = await _create_several_logs(client, project_name)

    # fetch entries for the project
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        params={"from_fields": "&".join(["_/temperature", "_/state", "_/metadata"])},
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    response = response.json()
    logs = response["logs"]
    assert len(logs) == 6
    assert len(logs) == response["count"]
    assert logs[0]["entries"] == {"_/metadata": [3, 8, 5]}
    assert logs[1]["entries"] == {"_/metadata": [1, 5, 6]}
    assert logs[2]["entries"] == {"_/temperature": -210.0, "_/state": "liquid->solid"}
    assert logs[3]["entries"] == {"_/temperature": 6000.0, "_/state": "gas"}
    assert logs[4]["entries"] == {"_/temperature": 0.0, "_/state": "liquid->solid"}
    assert logs[5]["entries"] == {"_/temperature": 100.0, "_/state": "liquid->gas"}


@pytest.mark.anyio
async def test_get_logs_excluding_fields(client: AsyncClient, use_jsonb_mode):
    """Test excluding fields from logs."""
    project_name = "eval-project"
    # create the same project with another user to ensure the correct one
    # is fetched
    _ = await _create_project(client, project_name)
    _ = await _create_several_logs(client, project_name)

    # fetch entries for the project
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        params={
            "exclude_fields": "&".join(
                [
                    "_/temperature",
                    "_/state",
                    "_/_data",
                    "_/timestamp",
                ],
            ),
        },
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    response = response.json()
    logs = response["logs"]
    assert len(logs) == 6
    assert len(logs) == response["count"]
    assert logs[0]["entries"] == {"_/description": "air", "_/metadata": [3, 8, 5]}
    assert logs[1]["entries"] == {"_/description": "lava", "_/metadata": [1, 5, 6]}
    assert logs[2]["entries"] == {"_/description": "freezing nitrogen", "_/safe": False}
    assert logs[3]["entries"] == {
        "_/description": "surface of the sun",
        "_/safe": False,
    }
    assert logs[4]["entries"] == {"_/description": "freezing water", "_/safe": True}
    assert logs[5]["entries"] == {"_/description": "boiling water", "_/safe": False}


@pytest.mark.anyio
async def test_get_logs_w_column_context(client: AsyncClient, use_jsonb_mode):
    """Test getting logs with column context."""
    project_name = "eval-project"
    # create project and log
    _ = await _create_project(client, project_name, user=1)
    _ = await _create_log(client, project_name, user=1)

    # get full context log
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    response = response.json()
    del response["logs"][0]["ts"]
    assert response == {
        "logs": [
            {
                "id": 1,
                "entries": {
                    "a/b/c/input": "Some input data",
                    "a/b/c/boolean_input": True,
                    "a/b/c/numeric_input": 4.5,
                },
                "derived_entries": {},
                "versions": {},
                "clipped_fields": [],
            },
        ],
        "count": 1,
    }

    # get log with "a" context
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        params={"column_context": "a"},
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    response = response.json()
    del response["logs"][0]["ts"]
    assert response == {
        "logs": [
            {
                "id": 1,
                "entries": {
                    "b/c/input": "Some input data",
                    "b/c/boolean_input": True,
                    "b/c/numeric_input": 4.5,
                },
                "derived_entries": {},
                "versions": {},
                "clipped_fields": [],
            },
        ],
        "count": 1,
    }

    # get log with "a/b" context
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        params={"column_context": "a/b"},
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    response = response.json()
    del response["logs"][0]["ts"]
    assert response == {
        "logs": [
            {
                "id": 1,
                "entries": {
                    "c/input": "Some input data",
                    "c/boolean_input": True,
                    "c/numeric_input": 4.5,
                },
                "derived_entries": {},
                "versions": {},
                "clipped_fields": [],
            },
        ],
        "count": 1,
    }

    # get log with "a/b/c" context
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        params={"column_context": "a/b/c"},
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    response = response.json()
    del response["logs"][0]["ts"]
    assert response == {
        "logs": [
            {
                "id": 1,
                "entries": {
                    "input": "Some input data",
                    "boolean_input": True,
                    "numeric_input": 4.5,
                },
                "derived_entries": {},
                "versions": {},
                "clipped_fields": [],
            },
        ],
        "count": 1,
    }


@pytest.mark.anyio
async def test_get_logs_latest_timestamp(client: AsyncClient, use_jsonb_mode):
    """Test getting latest timestamp."""
    # create logs
    project_name = "eval-project"
    _ = await _create_project(client, project_name, user=1)
    t0 = datetime.now(timezone.utc)
    _ = await _create_several_logs(client, project_name, user=1)

    # assert the latest timestamp t1 is more recent than t0
    response = await client.get(
        f"/v0/logs/latest_timestamp?project_name={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    response = response.json()
    assert isinstance(response, str)
    t1 = datetime.fromisoformat(response).replace(tzinfo=timezone.utc)
    assert t1 > t0

    # add new entries
    entries = {
        "new_entry": "Updated value",
        "explicit_types": {"new_entry": {"type": "str"}},
    }
    response = await _update_logs(client, [1, 2], entries)
    assert response.status_code == 200, response.json()

    # assert the latest timestamp t2 is more recent than t1
    response = await client.get(
        f"/v0/logs/latest_timestamp?project_name={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    response = response.json()
    assert isinstance(response, str)
    t2 = datetime.fromisoformat(response).replace(tzinfo=timezone.utc)
    assert t2 > t1


@pytest.mark.anyio
async def test_get_log_ids(client: AsyncClient, use_jsonb_mode):
    project_name = "eval-project"
    # create the same project with another user to ensure the correct one
    # is fetched
    _ = await _create_project(client, project_name, user=2)
    _ = await _create_project(client, project_name, user=1)
    _ = await _create_several_logs(client, project_name, user=1)

    # fetch entries for the project
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        params={"return_ids_only": True},
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    response = response.json()
    assert isinstance(response, list)
    assert len(response) == 7
    correct = list(range(1, 8))
    correct.reverse()
    assert response == correct


@pytest.mark.anyio
async def test_get_logs_field_ordering(client: AsyncClient, use_jsonb_mode):
    project_name = "field-order-test"
    _ = await _create_project(client, project_name)

    # Create first log with fields in one order
    response = await client.post(
        "/v0/logs",
        json={
            "project_name": project_name,
            "entries": {
                "field1": "first",
                "field2": "second",
                "field3": "third",
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create second log with fields in different order
    response = await client.post(
        "/v0/logs",
        json={
            "project_name": project_name,
            "entries": {
                "field2": "second again",
                "field3": "third again",
                "field1": "first again",
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Get logs and verify field ordering
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200

    logs = response.json()["logs"]
    assert len(logs) == 2

    # Verify field ordering in first log
    first_log_fields = list(logs[0]["entries"].keys())
    assert first_log_fields == ["field1", "field2", "field3"]

    # Verify same field ordering in second log
    second_log_fields = list(logs[1]["entries"].keys())
    assert second_log_fields == ["field1", "field2", "field3"]


@pytest.mark.anyio
async def test_get_logs_with_value_limit(client: AsyncClient, use_jsonb_mode):
    """Test value_limit parameter for truncating large values."""
    project_name = "value-limit-test"
    _ = await _create_project(client, project_name)

    # Create test data with various value types and lengths
    test_data = {
        "entries": {
            "numeric_int": 12345,
            "numeric_float": 123.45,
            "short_string": "Hello",
            "long_string": "A" * 200,
            "nested_dict": {"key1": "value1", "key2": "value2"},
            "nested_list": [1, 2, 3, 4, 5],
            "nested_tuple": ("a", "b", "c"),
            "boolean_value": True,
        },
    }

    # Add image data
    img_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
        "sample_datasets/img.png",
    )
    success, buffer = cv2.imencode(".png", cv2.imread(img_path))
    assert success
    test_data["entries"]["image_field"] = base64.b64encode(buffer).decode("utf-8")

    # Create log with test data
    response = await client.post(
        "/v0/logs",
        json={"project_name": project_name, "entries": test_data["entries"]},
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Test with value_limit=10
    response = await client.get(
        f"/v0/logs?project_name={project_name}&value_limit=10",
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    log_entries = result["logs"][0]["entries"]

    # Test numeric values remain unchanged
    assert log_entries["numeric_int"] == 12345
    assert log_entries["numeric_float"] == 123.45
    assert log_entries["boolean_value"] == True

    # Test string truncation
    assert log_entries["short_string"] == "Hello"  # Should not be truncated
    assert log_entries["long_string"] == "AAAAAAAAAA..."  # Should be truncated

    # Test nested structure handling
    assert len(log_entries["nested_dict"]) <= 13  # '{"key1":"va...'
    assert "..." in log_entries["nested_dict"]
    assert len(log_entries["nested_list"]) <= 13
    assert "..." in log_entries["nested_list"]
    assert len(log_entries["nested_tuple"]) <= 13
    assert "..." in log_entries["nested_tuple"]

    # Test clipping indicator
    assert "clipped_fields" in result["logs"][0]
    clipped_fields = result["logs"][0]["clipped_fields"]
    assert "long_string" in clipped_fields
    assert "nested_dict" in clipped_fields
    assert "nested_list" in clipped_fields
    assert "nested_tuple" in clipped_fields
    assert "image_field" in clipped_fields
    assert "short_string" not in clipped_fields
    assert "numeric_int" not in clipped_fields
    assert "numeric_float" not in clipped_fields
    assert "boolean_value" not in clipped_fields

    # Test with no value_limit (backward compatibility)
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    log_entries = result["logs"][0]["entries"]

    # Verify all values are returned in full
    assert log_entries["numeric_int"] == 12345
    assert log_entries["numeric_float"] == 123.45
    assert log_entries["short_string"] == "Hello"
    assert log_entries["long_string"] == "A" * 200
    assert log_entries["nested_dict"] == {"key1": "value1", "key2": "value2"}
    assert log_entries["nested_list"] == [1, 2, 3, 4, 5]
    assert log_entries["nested_tuple"] == ["a", "b", "c"]  # JSON converts tuple to list
    assert log_entries["boolean_value"] == True

    # Verify no clipping indicators when value_limit is not set
    assert "clipped_fields" in result["logs"][0]
    assert len(result["logs"][0]["clipped_fields"]) == 0

    # Test with zero value_limit
    response = await client.get(
        f"/v0/logs?project_name={project_name}&value_limit=0",
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    log_entries = result["logs"][0]["entries"]

    # Verify numeric values are unchanged but strings are empty or truncated
    assert log_entries["numeric_int"] == 12345
    assert log_entries["numeric_float"] == 123.45
    assert log_entries["boolean_value"] == True
    assert log_entries["short_string"] == "..."
    assert log_entries["long_string"] == "..."
    assert log_entries["nested_dict"] == "..."
    assert log_entries["nested_list"] == "..."
    assert log_entries["nested_tuple"] == "..."
    assert log_entries["image_field"] == ""


@pytest.mark.anyio
async def test_get_empty_logs(client: AsyncClient, use_jsonb_mode):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)

    # fetch entries for the project
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    assert isinstance(response.json()["logs"], list)  # List of logs is returned
    assert len(response.json()["logs"]) == 0  # Logs are empty


@pytest.mark.anyio
async def test_get_logs_w_pagination(client: AsyncClient, use_jsonb_mode):
    project_name = "test-pagination-project"
    _ = await _create_project(client, project_name)
    _ = await _create_several_logs(client, project_name)

    # limit = 3
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        params={"limit": 3},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 3

    assert result["logs"][0]["entries"] == {
        "_/_data": {"a": 8, "b": 10},
        "_/timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][1]["entries"] == {
        "_/description": "air",
        "_/metadata": [3, 8, 5],
        "_/_data": {"a": 6, "b": 12, "c": 8, "d": 11},
        "_/timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][2]["entries"] == {
        "_/description": "lava",
        "_/metadata": [1, 5, 6],
        "_/_data": {"a": 2, "b": 4},
        "_/timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
    }

    # limit = 3 and offset = 2
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        params={"limit": 3, "offset": 2},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 3

    assert result["logs"][0]["entries"] == {
        "_/description": "lava",
        "_/metadata": [1, 5, 6],
        "_/_data": {"a": 2, "b": 4},
        "_/timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][1]["entries"] == {
        "_/description": "freezing nitrogen",
        "_/temperature": -210.0,
        "_/state": "liquid->solid",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][2]["entries"] == {
        "_/description": "surface of the sun",
        "_/temperature": 6000.0,
        "_/state": "gas",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }

    # offset = 5
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        params={"offset": 5},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 2

    assert result["logs"][0]["entries"] == {
        "_/description": "freezing water",
        "_/temperature": 0.0,
        "_/state": "liquid->solid",
        "_/safe": True,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][1]["entries"] == {
        "_/description": "boiling water",
        "_/temperature": 100.0,
        "_/state": "liquid->gas",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }


@pytest.mark.anyio
async def test_get_logs_nested_dict_ordering(client: AsyncClient, use_jsonb_mode):
    """Test nested dict key ordering is preserved in both EAV and JSONB modes.

    JSONB mode uses a separate key_order column to store the original insertion order
    since PostgreSQL JSONB fundamentally alphabetizes keys for performance optimization.
    """
    project_name = "nested-dict-order-test"
    _ = await _create_project(client, project_name)

    # Create a log with deeply nested dictionaries in specific orders
    nested_data = {
        "level1": {
            "c": {
                "inner3": 3,
                "inner2": 2,
                "inner1": 1,
                "nested": {
                    "z": "last",
                    "y": "middle",
                    "x": "first",
                },
            },
            "b": {
                "foo": "bar",
                "baz": "qux",
                "empty_dict": {},
                "list_of_dicts": [
                    {"d3": 3, "d2": 2, "d1": 1},
                    {"z": "z", "y": "y", "x": "x"},
                ],
            },
            "a": "value",
        },
        "edge_cases": {
            "empty": {},
            "mixed_types": {
                "num": 42,
                "str": "text",
                "bool": True,
                "null": None,
                "list": [1, 2, 3],
                "nested_empty": {"empty": {}},
            },
        },
        "simple": "field",
    }

    response = await client.post(
        "/v0/logs",
        json={
            "project_name": project_name,
            "entries": nested_data,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Retrieve and verify the log
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200

    log = response.json()["logs"][0]["entries"]

    # Verify top level ordering
    assert list(log.keys()) == ["level1", "edge_cases", "simple"]

    # Verify level1 ordering
    level1 = log["level1"]
    assert list(level1.keys()) == ["c", "b", "a"]

    # Verify inner dictionary ordering
    inner_c = level1["c"]
    assert list(inner_c.keys()) == ["inner3", "inner2", "inner1", "nested"]
    assert list(inner_c["nested"].keys()) == ["z", "y", "x"]

    inner_b = level1["b"]
    assert list(inner_b.keys()) == ["foo", "baz", "empty_dict", "list_of_dicts"]
    assert inner_b["empty_dict"] == {}

    # Verify ordering in list of dictionaries
    list_of_dicts = inner_b["list_of_dicts"]
    assert len(list_of_dicts) == 2
    assert list(list_of_dicts[0].keys()) == ["d3", "d2", "d1"]
    assert list(list_of_dicts[1].keys()) == ["z", "y", "x"]

    # Verify edge cases
    edge_cases = log["edge_cases"]
    assert list(edge_cases.keys()) == ["empty", "mixed_types"]
    assert edge_cases["empty"] == {}

    mixed_types = edge_cases["mixed_types"]
    assert list(mixed_types.keys()) == [
        "num",
        "str",
        "bool",
        "null",
        "list",
        "nested_empty",
    ]
    assert mixed_types["nested_empty"] == {"empty": {}}


@pytest.mark.anyio
async def test_get_logs_randomized_pagination_and_reproducibility(
    client: AsyncClient,
    use_jsonb_mode,
):
    project = "randomize-test"
    await _create_project(client, project)
    await _create_several_logs(client, project)

    # Page 1 with randomize
    resp1 = await client.get(
        "/v0/logs",
        params={"project_name": project, "randomize": True, "limit": 3},
        headers=HEADERS,
    )
    assert resp1.status_code == 200
    ids_page1 = [log["id"] for log in resp1.json()["logs"]]

    # Repeated call returns same IDs
    resp2 = await client.get(
        "/v0/logs",
        params={"project_name": project, "randomize": True, "limit": 3},
        headers=HEADERS,
    )
    assert [log["id"] for log in resp2.json()["logs"]] == ids_page1

    # Page 2 has no overlap
    resp3 = await client.get(
        "/v0/logs",
        params={"project_name": project, "randomize": True, "limit": 3, "offset": 3},
        headers=HEADERS,
    )
    ids_page2 = [log["id"] for log in resp3.json()["logs"]]
    assert set(ids_page1).isdisjoint(ids_page2)
