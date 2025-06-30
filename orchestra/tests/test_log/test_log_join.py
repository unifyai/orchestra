import pytest
from httpx import AsyncClient

from . import HEADERS, _create_log, _create_project


@pytest.mark.anyio
async def test_inner_join_logs(client: AsyncClient):
    """Test inner join based on a common user_id."""
    project_name = "test_project_join_inner"
    await _create_project(client, project_name, user=1)

    context_a = "context_A"
    context_b = "context_B"
    joined_context = "joined_inner"

    await _create_log(
        client,
        project_name,
        context=context_a,
        entries={
            "some_field": "A_log",
            "user_id": 1,
            "another_field": "A_log",
            "new_field": 10,
        },
    )

    await _create_log(
        client,
        project_name,
        context=context_b,
        entries={
            "some_field": "B_log",
            "another_field": "B_log",
            "user_id": 1,
            "another_new_field": 20,
        },
    )

    join_payload = {
        "project": project_name,
        "pair_of_args": [{"context": context_a}, {"context": context_b}],
        "join_expr": "A.user_id == B.user_id",
        "mode": "inner",
        "new_context": joined_context,
        "columns": ["A.user_id", "A.some_field", "B.another_field"],
    }
    response = await client.post("/v0/logs/join", json=join_payload, headers=HEADERS)
    assert response.status_code == 200

    response = await client.get(
        f"/v0/logs?project={project_name}&context={joined_context}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json().get("logs", [])
    assert isinstance(logs, list) and len(logs) == 1

    entries = logs[0].get("entries", {})
    assert entries.get("A_user_id") == 1
    assert entries.get("A_some_field") == "A_log"
    assert entries.get("B_another_field") == "B_log"


@pytest.mark.anyio
async def test_no_match_join_logs(client: AsyncClient):
    """Test inner join where no logs match the join condition."""
    project_name = "test_project_join_no_match"
    await _create_project(client, project_name, user=1)

    context_a = "context_A_no_match"
    context_b = "context_B_no_match"
    joined_context = "joined_no_match"

    await _create_log(
        client,
        project_name,
        context=context_a,
        entries={"user_id": 1, "some_field": "A_log"},
    )

    await _create_log(
        client,
        project_name,
        context=context_b,
        entries={"user_id": 2, "another_field": "B_log"},
    )

    join_payload = {
        "project": project_name,
        "pair_of_args": [{"context": context_a}, {"context": context_b}],
        "join_expr": "A.user_id == B.user_id",
        "mode": "inner",
        "new_context": joined_context,
        "columns": ["A.user_id", "A.some_field", "B.another_field"],
    }
    response = await client.post("/v0/logs/join", json=join_payload, headers=HEADERS)
    assert response.status_code == 200

    response = await client.get(
        f"/v0/logs?project={project_name}&context={joined_context}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json().get("logs", [])
    assert isinstance(logs, list) and len(logs) == 0


@pytest.mark.anyio
async def test_left_join_logs(client: AsyncClient):
    """Test left join ensuring all logs from the left context are present."""
    project_name = "test_project_join_left"
    await _create_project(client, project_name, user=1)
    context_a = "context_A_left"
    context_b = "context_B_left"
    joined_context = "joined_left"

    await _create_log(
        client,
        project_name,
        context=context_a,
        entries={"user_id": 1, "field_A": "value_A1"},
    )
    await _create_log(
        client,
        project_name,
        context=context_a,
        entries={"user_id": 2, "field_A": "value_A2"},
    )

    await _create_log(
        client,
        project_name,
        context=context_b,
        entries={"user_id": 1, "field_B": "value_B1"},
    )

    join_payload = {
        "project": project_name,
        "pair_of_args": [{"context": context_a}, {"context": context_b}],
        "join_expr": "A.user_id == B.user_id",
        "mode": "left",
        "new_context": joined_context,
        "columns": ["A.user_id", "A.field_A", "B.field_B"],
    }
    response = await client.post("/v0/logs/join", json=join_payload, headers=HEADERS)
    assert response.status_code == 200

    response = await client.get(
        f"/v0/logs?project={project_name}&context={joined_context}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json().get("logs", [])
    assert isinstance(logs, list) and len(logs) == 2

    log_map = {
        log.get("entries", {}).get("A_user_id"): log.get("entries", {}) for log in logs
    }
    assert 1 in log_map and 2 in log_map

    entries_1 = log_map[1]
    assert entries_1.get("A_field_A") == "value_A1"
    assert entries_1.get("B_field_B") == "value_B1"

    entries_2 = log_map[2]
    assert entries_2.get("A_field_A") == "value_A2"
    assert "B_field_B" not in entries_2 or entries_2.get("B_field_B") is None


@pytest.mark.anyio
async def test_right_join_logs(client: AsyncClient):
    """Test right join ensuring all logs from the right context are present."""
    project_name = "test_project_join_right"
    await _create_project(client, project_name, user=1)
    context_a = "context_A_right"
    context_b = "context_B_right"
    joined_context = "joined_right"

    await _create_log(
        client,
        project_name,
        context=context_a,
        entries={"user_id": 1, "field_A": "value_A1"},
    )

    await _create_log(
        client,
        project_name,
        context=context_b,
        entries={"user_id": 1, "field_B": "value_B1"},
    )
    await _create_log(
        client,
        project_name,
        context=context_b,
        entries={"user_id": 3, "field_B": "value_B3"},
    )

    join_payload = {
        "project": project_name,
        "pair_of_args": [{"context": context_a}, {"context": context_b}],
        "join_expr": "A.user_id == B.user_id",
        "mode": "right",
        "new_context": joined_context,
        "columns": ["B.user_id", "B.field_B", "A.field_A"],
    }
    response = await client.post("/v0/logs/join", json=join_payload, headers=HEADERS)
    assert response.status_code == 200

    response = await client.get(
        f"/v0/logs?project={project_name}&context={joined_context}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json().get("logs", [])
    assert isinstance(logs, list) and len(logs) == 2

    log_map = {
        log.get("entries", {}).get("B_user_id"): log.get("entries", {}) for log in logs
    }
    assert 1 in log_map and 3 in log_map

    entries_1 = log_map[1]
    assert entries_1.get("B_field_B") == "value_B1"
    assert entries_1.get("A_field_A") == "value_A1"

    entries_3 = log_map[3]
    assert entries_3.get("B_field_B") == "value_B3"
    assert "A_field_A" not in entries_3 or entries_3.get("A_field_A") is None


@pytest.mark.anyio
async def test_outer_join_logs(client: AsyncClient):
    """Test outer join ensuring all logs from both contexts are present."""
    project_name = "test_project_join_outer"
    await _create_project(client, project_name, user=1)
    context_a = "context_A_outer"
    context_b = "context_B_outer"
    joined_context = "joined_outer"

    await _create_log(
        client,
        project_name,
        context=context_a,
        entries={"user_id": 1, "field_A": "value_A1"},
    )
    await _create_log(
        client,
        project_name,
        context=context_a,
        entries={"user_id": 2, "field_A": "value_A2"},
    )

    await _create_log(
        client,
        project_name,
        context=context_b,
        entries={"user_id": 1, "field_B": "value_B1"},
    )
    await _create_log(
        client,
        project_name,
        context=context_b,
        entries={"user_id": 3, "field_B": "value_B3"},
    )
    await _create_log(
        client,
        project_name,
        context=context_b,
        entries={"user_id": 4, "field_B": "value_B4"},
    )

    join_payload = {
        "project": project_name,
        "pair_of_args": [{"context": context_a}, {"context": context_b}],
        "join_expr": "A.user_id == B.user_id",
        "mode": "outer",
        "new_context": joined_context,
        "columns": ["A.user_id", "A.field_A", "B.user_id", "B.field_B"],
    }
    response = await client.post("/v0/logs/join", json=join_payload, headers=HEADERS)
    assert response.status_code == 200

    response = await client.get(
        f"/v0/logs?project={project_name}&context={joined_context}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json().get("logs", [])
    # Expected rows: user_id=1 (matched), user_id=2 (from A only), user_id=3, user_id=4 (from B only)
    assert isinstance(logs, list) and len(logs) == 4

    # Check the content characteristics based on expected join behavior
    a_user_ids = {
        log.get("entries", {}).get("A_user_id")
        for log in logs
        if log.get("entries", {}).get("A_user_id") is not None
    }
    b_user_ids = {
        log.get("entries", {}).get("B_user_id")
        for log in logs
        if log.get("entries", {}).get("B_user_id") is not None
    }

    assert a_user_ids == {1, 2}
    assert b_user_ids == {1, 3, 4}

    # Verify specific rows (example check for the matched row and one unmatched from each side)
    found_matched = False
    found_a_only = False
    found_b_only_3 = False
    found_b_only_4 = False

    for log in logs:
        entries = log.get("entries", {})
        a_id = entries.get("A_user_id")
        b_id = entries.get("B_user_id")

        if a_id == 1 and b_id == 1:
            assert entries.get("A_field_A") == "value_A1"
            assert entries.get("B_field_B") == "value_B1"
            found_matched = True
        elif a_id == 2 and b_id is None:
            assert entries.get("A_field_A") == "value_A2"
            assert entries.get("B_field_B") is None
            found_a_only = True
        elif a_id is None and b_id == 3:
            assert entries.get("A_field_A") is None
            assert entries.get("B_field_B") == "value_B3"
            found_b_only_3 = True
        elif a_id is None and b_id == 4:
            assert entries.get("A_field_A") is None
            assert entries.get("B_field_B") == "value_B4"
            found_b_only_4 = True

    assert found_matched and found_a_only and found_b_only_3 and found_b_only_4


@pytest.mark.anyio
async def test_complex_join_expression(client: AsyncClient):
    """Test inner join with a complex expression involving multiple fields."""
    project_name = "test_project_join_complex"
    await _create_project(client, project_name, user=1)
    context_a = "context_A_complex"
    context_b = "context_B_complex"
    joined_context = "joined_complex"

    await _create_log(
        client,
        project_name,
        context=context_a,
        entries={"user_id": 5, "new_field": [1, 2, 3], "field_A": "complex_A1"},
    )
    await _create_log(
        client,
        project_name,
        context=context_a,
        entries={
            "user_id": 6,
            "new_field": [22, 33, 44, 55, 66],
            "field_A": "complex_A2",
        },
    )

    await _create_log(
        client,
        project_name,
        context=context_b,
        entries={"user_id": 5, "field_B": "complex_B1"},
    )
    await _create_log(
        client,
        project_name,
        context=context_b,
        entries={"user_id": 6, "field_B": "complex_B2"},
    )

    # Join when A.user_id equals B.user_id AND meA.new_field > 15
    join_payload = {
        "project": project_name,
        "pair_of_args": [{"context": context_a}, {"context": context_b}],
        "join_expr": "A.user_id == B.user_id and mean(A.new_field) > 3",
        "mode": "inner",
        "new_context": joined_context,
        "columns": ["A.user_id", "A.new_field", "A.field_A", "B.field_B"],
    }
    response = await client.post("/v0/logs/join", json=join_payload, headers=HEADERS)
    assert response.status_code == 200, response.text

    response = await client.get(
        f"/v0/logs?project={project_name}&context={joined_context}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    logs = response.json().get("logs", [])
    assert isinstance(logs, list) and len(logs) == 1

    entries = logs[0].get("entries", {})
    assert entries.get("A_user_id") == 6
    assert entries.get("A_new_field") == [22, 33, 44, 55, 66]
    assert entries.get("A_field_A") == "complex_A2"
    assert entries.get("B_field_B") == "complex_B2"


@pytest.mark.anyio
async def test_two_column_equality_join(client: AsyncClient):
    """Test inner join using a composite key (two columns)."""
    project_name = "proj_two_cols"
    await _create_project(client, project_name, user=1)
    context_a = "A"
    context_b = "B"
    joined_context = "J"

    await _create_log(
        client,
        project_name,
        context=context_a,
        entries={"x": 1, "y": 2, "val": "left"},
    )
    await _create_log(
        client,
        project_name,
        context=context_b,
        entries={"x": 1, "y": 2, "val": "right"},
    )
    await _create_log(
        client,
        project_name,
        context=context_b,
        entries={"x": 1, "y": 3, "val": "wrong"},
    )  # Should not match

    payload = {
        "project": project_name,
        "pair_of_args": [{"context": context_a}, {"context": context_b}],
        "join_expr": "A.x == B.x and A.y == B.y",
        "mode": "inner",
        "new_context": joined_context,
        "columns": ["A.val", "B.val"],
    }
    response = await client.post("/v0/logs/join", json=payload, headers=HEADERS)
    assert response.status_code == 200

    response = await client.get(
        f"/v0/logs?project={project_name}&context={joined_context}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json().get("logs", [])
    assert len(logs) == 1

    entries = logs[0].get("entries", {})
    assert entries.get("A_val") == "left"
    assert entries.get("B_val") == "right"


@pytest.mark.anyio
async def test_join_without_columns_arg(client: AsyncClient):
    """Test join returns all columns correctly prefixed when 'columns' is omitted."""
    project_name = "proj_no_cols"
    await _create_project(client, project_name, user=1)
    context_a = "A"
    context_b = "B"
    joined_context = "J"

    await _create_log(
        client,
        project_name,
        context=context_a,
        entries={"k": 1, "shared": "foo"},
        params={},
    )
    await _create_log(
        client,
        project_name,
        context=context_b,
        entries={"m": 9, "shared": "bar"},
        params={},
    )

    payload = {
        "project": project_name,
        "pair_of_args": [{"context": context_a}, {"context": context_b}],
        "join_expr": "A.k == 1 and B.m == 9",  # Ensures one row match
        "mode": "inner",
        "new_context": joined_context,
        # No "columns" key specified
    }
    response = await client.post("/v0/logs/join", json=payload, headers=HEADERS)
    assert response.status_code == 200, response.text

    response = await client.get(
        f"/v0/logs?project={project_name}&context={joined_context}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    logs = response.json().get("logs", [])
    assert len(logs) == 1

    entries = logs[0].get("entries", {})
    # Expect all original columns, prefixed
    expected_entries = {"A_k": 1, "A_shared": "foo", "B_m": 9, "B_shared": "bar"}
    assert entries == expected_entries


@pytest.mark.anyio
async def test_duplicate_field_names(client: AsyncClient):
    """Test join handles columns with the same name in both contexts by prefixing."""
    project_name = "proj_dup_fields"
    await _create_project(client, project_name, user=1)
    context_a = "A"
    context_b = "B"
    joined_context = "J"

    await _create_log(client, project_name, context=context_a, entries={"score": 0.7})
    await _create_log(client, project_name, context=context_b, entries={"score": 0.9})

    payload = {
        "project": project_name,
        "pair_of_args": [{"context": context_a}, {"context": context_b}],
        "join_expr": "True",  # Cross-join to ensure one result row
        "mode": "inner",
        "new_context": joined_context,
        "columns": ["A.score", "B.score"],  # Explicitly request both
    }
    response = await client.post("/v0/logs/join", json=payload, headers=HEADERS)
    assert response.status_code == 200, response.text

    response = await client.get(
        f"/v0/logs?project={project_name}&context={joined_context}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json().get("logs", [])
    assert len(logs) == 1

    entries = logs[0]["entries"]
    assert entries.get("A_score") == 0.7
    assert entries.get("B_score") == 0.9


@pytest.mark.anyio
async def test_invalid_column_name_returns_400(client: AsyncClient):
    """Test join rejects request with non-existent column name in 'columns' list."""
    project_name = "proj_bad_col"
    await _create_project(client, project_name, user=1)
    context_a = "A"
    context_b = "B"
    joined_context = "J"  # Context won't actually be created

    await _create_log(client, project_name, context=context_a, entries={"k": 1})
    await _create_log(client, project_name, context=context_b, entries={"k": 1})

    payload = {
        "project": project_name,
        "pair_of_args": [{"context": context_a}, {"context": context_b}],
        "join_expr": "A.k == B.k",
        "mode": "inner",
        "new_context": joined_context,
        "columns": ["A.k", "B.no_such_column"],  # "B.no_such_column" does not exist
    }
    response = await client.post("/v0/logs/join", json=payload, headers=HEADERS)
    assert response.status_code == 400
