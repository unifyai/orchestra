import json
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient

# Import log_data from the parent module
from ..test_log import log_data
from . import HEADERS, _create_project, _create_several_logs


@pytest.mark.anyio
async def test_get_logs_w_sorting(client: AsyncClient, use_jsonb_mode):
    """Test sorting by numeric field (temperature) in ascending/descending order."""
    project_name = f"eval-project-sorting-{'jsonb' if use_jsonb_mode else 'eav'}"
    _ = await _create_project(client, project_name)
    _ = await _create_several_logs(client, project_name)

    # descending creation time (default)
    response = await client.get(
        f"/v0/logs?project={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 7
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
    assert result["logs"][3]["entries"] == {
        "_/description": "freezing nitrogen",
        "_/temperature": -210.0,
        "_/state": "liquid->solid",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][4]["entries"] == {
        "_/description": "surface of the sun",
        "_/temperature": 6000.0,
        "_/state": "gas",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][5]["entries"] == {
        "_/description": "freezing water",
        "_/temperature": 0.0,
        "_/state": "liquid->solid",
        "_/safe": True,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][6]["entries"] == {
        "_/description": "boiling water",
        "_/temperature": 100.0,
        "_/state": "liquid->gas",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }

    # ascending temperature
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"sorting": json.dumps({"_/temperature": "ascending"})},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 7
    assert result["logs"][0]["entries"] == {
        "_/description": "freezing nitrogen",
        "_/temperature": -210.0,
        "_/state": "liquid->solid",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][1]["entries"] == {
        "_/description": "freezing water",
        "_/temperature": 0.0,
        "_/state": "liquid->solid",
        "_/safe": True,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][2]["entries"] == {
        "_/description": "boiling water",
        "_/temperature": 100.0,
        "_/state": "liquid->gas",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][3]["entries"] == {
        "_/description": "surface of the sun",
        "_/temperature": 6000.0,
        "_/state": "gas",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][4]["entries"] == {
        "_/_data": {"a": 8, "b": 10},
        "_/timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][5]["entries"] == {
        "_/description": "air",
        "_/metadata": [3, 8, 5],
        "_/_data": {"a": 6, "b": 12, "c": 8, "d": 11},
        "_/timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][6]["entries"] == {
        "_/description": "lava",
        "_/metadata": [1, 5, 6],
        "_/_data": {"a": 2, "b": 4},
        "_/timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
    }

    # descending temperature
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"sorting": json.dumps({"_/temperature": "descending"})},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 7
    assert result["logs"][0]["entries"] == {
        "_/description": "surface of the sun",
        "_/temperature": 6000.0,
        "_/state": "gas",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][1]["entries"] == {
        "_/description": "boiling water",
        "_/temperature": 100.0,
        "_/state": "liquid->gas",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][2]["entries"] == {
        "_/description": "freezing water",
        "_/temperature": 0.0,
        "_/state": "liquid->solid",
        "_/safe": True,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][3]["entries"] == {
        "_/description": "freezing nitrogen",
        "_/temperature": -210.0,
        "_/state": "liquid->solid",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][4]["entries"] == {
        "_/_data": {"a": 8, "b": 10},
        "_/timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][5]["entries"] == {
        "_/description": "air",
        "_/metadata": [3, 8, 5],
        "_/_data": {"a": 6, "b": 12, "c": 8, "d": 11},
        "_/timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][6]["entries"] == {
        "_/description": "lava",
        "_/metadata": [1, 5, 6],
        "_/_data": {"a": 2, "b": 4},
        "_/timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
    }


@pytest.mark.anyio
async def test_get_logs_w_timestamp_sorting(client: AsyncClient, use_jsonb_mode):
    """Test sorting by timestamp field with dynamically created timestamps."""
    project_name = f"eval-project-ts-sort-{'jsonb' if use_jsonb_mode else 'eav'}"
    _ = await _create_project(client, project_name)
    data = log_data["logs_for_various"]
    timestamps = list()
    for i in range(len(data)):
        ts = datetime.now(timezone.utc).isoformat()
        timestamps.append(ts)
        # Use copy to avoid mutating shared log_data
        entries = dict(data[i])
        entries["_/timestamp"] = ts
        response = await client.post(
            "/v0/logs",
            json={
                "project": project_name,
                "params": {"a/b/param1": f"test_{i}"},
                "entries": entries,
            },
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()

    # descending timestamp
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"sorting": json.dumps({"_/timestamp": "descending"})},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 7
    assert result["logs"][0]["entries"] == {
        "_/_data": {"a": 8, "b": 10},
        "_/timestamp": timestamps[-1],
    }
    assert result["logs"][1]["entries"] == {
        "_/description": "air",
        "_/metadata": [3, 8, 5],
        "_/_data": {"a": 6, "b": 12, "c": 8, "d": 11},
        "_/timestamp": timestamps[-2],
    }
    assert result["logs"][2]["entries"] == {
        "_/description": "lava",
        "_/metadata": [1, 5, 6],
        "_/_data": {"a": 2, "b": 4},
        "_/timestamp": timestamps[-3],
    }
    assert result["logs"][3]["entries"] == {
        "_/description": "freezing nitrogen",
        "_/temperature": -210.0,
        "_/state": "liquid->solid",
        "_/safe": False,
        "_/timestamp": timestamps[-4],
    }
    assert result["logs"][4]["entries"] == {
        "_/description": "surface of the sun",
        "_/temperature": 6000.0,
        "_/state": "gas",
        "_/safe": False,
        "_/timestamp": timestamps[-5],
    }
    assert result["logs"][5]["entries"] == {
        "_/description": "freezing water",
        "_/temperature": 0.0,
        "_/state": "liquid->solid",
        "_/safe": True,
        "_/timestamp": timestamps[-6],
    }
    assert result["logs"][6]["entries"] == {
        "_/description": "boiling water",
        "_/temperature": 100.0,
        "_/state": "liquid->gas",
        "_/safe": False,
        "_/timestamp": timestamps[-7],
    }


@pytest.mark.anyio
async def test_get_logs_w_date_sorting(client: AsyncClient, use_jsonb_mode):
    """Test sorting by date field with fixed dates."""
    project_name = f"eval-project-date-sort-{'jsonb' if use_jsonb_mode else 'eav'}"
    _ = await _create_project(client, project_name)
    data = log_data["logs_for_various"]
    dates = list()
    for i in range(len(data)):
        date = datetime(1993, 3, i + 1, tzinfo=timezone.utc).isoformat()
        dates.append(date)
        # Use copy to avoid mutating shared log_data
        entries = dict(data[i])
        entries["_/timestamp"] = date
        response = await client.post(
            "/v0/logs",
            json={
                "project": project_name,
                "params": {"a/b/param1": f"test_{i}"},
                "entries": entries,
            },
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()

    # descending timestamp
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"sorting": json.dumps({"_/timestamp": "descending"})},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 7
    assert result["logs"][0]["entries"] == {
        "_/_data": {"a": 8, "b": 10},
        "_/timestamp": dates[-1],
    }
    assert result["logs"][1]["entries"] == {
        "_/description": "air",
        "_/metadata": [3, 8, 5],
        "_/_data": {"a": 6, "b": 12, "c": 8, "d": 11},
        "_/timestamp": dates[-2],
    }
    assert result["logs"][2]["entries"] == {
        "_/description": "lava",
        "_/metadata": [1, 5, 6],
        "_/_data": {"a": 2, "b": 4},
        "_/timestamp": dates[-3],
    }
    assert result["logs"][3]["entries"] == {
        "_/description": "freezing nitrogen",
        "_/temperature": -210.0,
        "_/state": "liquid->solid",
        "_/safe": False,
        "_/timestamp": dates[-4],
    }
    assert result["logs"][4]["entries"] == {
        "_/description": "surface of the sun",
        "_/temperature": 6000.0,
        "_/state": "gas",
        "_/safe": False,
        "_/timestamp": dates[-5],
    }
    assert result["logs"][5]["entries"] == {
        "_/description": "freezing water",
        "_/temperature": 0.0,
        "_/state": "liquid->solid",
        "_/safe": True,
        "_/timestamp": dates[-6],
    }
    assert result["logs"][6]["entries"] == {
        "_/description": "boiling water",
        "_/temperature": 100.0,
        "_/state": "liquid->gas",
        "_/safe": False,
        "_/timestamp": dates[-7],
    }


@pytest.mark.anyio
async def test_get_logs_w_dynamic_expression_sorting(
    client: AsyncClient,
    use_jsonb_mode,
):
    """Test sorting by a dynamic expression (e.g., round(_/temperature, 1))."""
    project_name = f"expr-project-sort-{'jsonb' if use_jsonb_mode else 'eav'}"
    await _create_project(client, project_name)
    await _create_several_logs(client, project_name)

    expr = "round(_/temperature, 1)"
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"sorting": json.dumps({expr: "ascending"})},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    logs = response.json()["logs"]
    temps = [
        log["entries"]["_/temperature"]
        for log in logs
        if "_/temperature" in log["entries"]
    ]
    # temperature values should be sorted in ascending order
    assert temps == [
        -210.0,
        0.0,
        100.0,
        6000.0,
    ], f"Expected ascending temps, got {temps}"

    # Also test descending
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"sorting": json.dumps({expr: "descending"})},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    logs = response.json()["logs"]
    temps = [
        log["entries"]["_/temperature"]
        for log in logs
        if "_/temperature" in log["entries"]
    ]
    # temperature values should be sorted in descending order
    assert temps == [
        6000.0,
        100.0,
        0.0,
        -210.0,
    ], f"Expected descending temps, got {temps}"
