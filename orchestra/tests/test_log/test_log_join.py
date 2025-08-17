import pytest
from httpx import AsyncClient

from . import HEADERS, _create_log, _create_project, _update_logs


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
        "columns": {
            "A.user_id": "uid",
            "A.some_field": "field_from_A",
            "B.another_field": "field_from_B",
        },
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
    assert entries.get("uid") == 1
    assert entries.get("field_from_A") == "A_log"
    assert entries.get("field_from_B") == "B_log"


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
        "columns": {
            "A.user_id": "uid",
            "A.some_field": "field_A",
            "B.another_field": "field_B",
        },
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
        "columns": {
            "A.user_id": "user",
            "A.field_A": "field_from_A",
            "B.field_B": "field_from_B",
        },
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
        log.get("entries", {}).get("user"): log.get("entries", {}) for log in logs
    }
    assert 1 in log_map and 2 in log_map

    entries_1 = log_map[1]
    assert entries_1.get("field_from_A") == "value_A1"
    assert entries_1.get("field_from_B") == "value_B1"

    entries_2 = log_map[2]
    assert entries_2.get("field_from_A") == "value_A2"
    assert "field_from_B" not in entries_2 or entries_2.get("field_from_B") is None


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
        "columns": {
            "B.user_id": "user",
            "A.field_A": "field_from_A",
            "B.field_B": "field_from_B",
        },
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
        log.get("entries", {}).get("user"): log.get("entries", {}) for log in logs
    }
    assert 1 in log_map and 3 in log_map

    entries_1 = log_map[1]
    assert entries_1.get("field_from_B") == "value_B1"
    assert entries_1.get("field_from_A") == "value_A1"

    entries_3 = log_map[3]
    assert entries_3.get("field_from_B") == "value_B3"
    assert "field_from_A" not in entries_3 or entries_3.get("field_from_A") is None


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
        "columns": {
            "A.user_id": "a_uid",
            "A.field_A": "a_val",
            "B.user_id": "b_uid",
            "B.field_B": "b_val",
        },
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
        log.get("entries", {}).get("a_uid")
        for log in logs
        if log.get("entries", {}).get("a_uid") is not None
    }
    b_user_ids = {
        log.get("entries", {}).get("b_uid")
        for log in logs
        if log.get("entries", {}).get("b_uid") is not None
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
        a_id = entries.get("a_uid")
        b_id = entries.get("b_uid")

        if a_id == 1 and b_id == 1:
            assert entries.get("a_val") == "value_A1"
            assert entries.get("b_val") == "value_B1"
            found_matched = True
        elif a_id == 2 and b_id is None:
            assert entries.get("a_val") == "value_A2"
            assert entries.get("b_val") is None
            found_a_only = True
        elif a_id is None and b_id == 3:
            assert entries.get("a_val") is None
            assert entries.get("b_val") == "value_B3"
            found_b_only_3 = True
        elif a_id is None and b_id == 4:
            assert entries.get("a_val") is None
            assert entries.get("b_val") == "value_B4"
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
        "columns": {
            "A.user_id": "uid",
            "A.new_field": "values",
            "A.field_A": "fieldA",
            "B.field_B": "fieldB",
        },
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
    assert entries.get("uid") == 6
    assert entries.get("values") == [22, 33, 44, 55, 66]
    assert entries.get("fieldA") == "complex_A2"
    assert entries.get("fieldB") == "complex_B2"


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
        "columns": {"A.val": "left_val", "B.val": "right_val"},
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
    assert entries.get("left_val") == "left"
    assert entries.get("right_val") == "right"


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
        "columns": {
            "A.score": "score_A",
            "B.score": "score_B",
        },  # Explicitly request and alias both
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
    assert entries.get("score_A") == 0.7
    assert entries.get("score_B") == 0.9


@pytest.mark.anyio
async def test_invalid_column_name_returns_400(client: AsyncClient):
    """Test join rejects request with non-existent column name in 'columns' dict."""
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
        "columns": {
            "A.k": "k_val",
            "B.no_such_column": "no_val",
        },  # "B.no_such_column" does not exist
    }
    response = await client.post("/v0/logs/join", json=payload, headers=HEADERS)
    assert response.status_code == 400


@pytest.mark.anyio
async def test_join_logs_pass_by_reference(client: AsyncClient):
    """Test join logs with copy=False to verify pass-by-reference behavior."""
    project_name = "test_project_join_reference"
    await _create_project(client, project_name, user=1)

    context_a = "context_A_ref"
    context_b = "context_B_ref"
    joined_context = "joined_ref"

    # Create initial logs
    response_a = await _create_log(
        client,
        project_name,
        context=context_a,
        entries={"user_id": 1, "score": 100, "name": "Alice"},
    )
    assert response_a.status_code == 200

    response_b = await _create_log(
        client,
        project_name,
        context=context_b,
        entries={"user_id": 1, "category": "premium", "status": "active"},
    )
    assert response_b.status_code == 200

    # Perform join with copy=False (pass by reference)
    join_payload = {
        "project": project_name,
        "pair_of_args": [{"context": context_a}, {"context": context_b}],
        "join_expr": "A.user_id == B.user_id",
        "mode": "inner",
        "new_context": joined_context,
        "copy": False,  # Pass by reference
    }
    response = await client.post("/v0/logs/join", json=join_payload, headers=HEADERS)
    assert response.status_code == 200

    # Verify the joined result
    response = await client.get(
        f"/v0/logs?project={project_name}&context={joined_context}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json().get("logs", [])
    assert len(logs) == 1

    entries = logs[0].get("entries", {})
    assert entries.get("user_id") == 1
    assert entries.get("score") == 100
    assert entries.get("category") == "premium"

    # Update the original log in context A
    # Get the log ID from context A first
    response = await client.get(
        f"/v0/logs?project={project_name}&context={context_a}",
        headers=HEADERS,
    )
    log_a = response.json().get("logs", [])[0]
    log_a_id = log_a.get("id")

    # Update the score in the original log
    response = await _update_logs(
        client,
        [log_a_id],
        {"score": 200},
        overwrite=True,
    )
    assert response.status_code == 200

    # Check if the joined context reflects the update (with pass-by-reference)
    response = await client.get(
        f"/v0/logs?project={project_name}&context={joined_context}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json().get("logs", [])
    assert len(logs) == 1

    # With pass-by-reference (copy=False), the joined context should reflect the update
    # because it references the original logs rather than creating copies.
    # Note: When using pass-by-reference, the original keys are preserved (no aliases)
    entries = logs[0].get("entries", {})
    assert entries.get("user_id") == 1  # user_id should remain the same
    assert (
        entries.get("score") == 200
    )  # score should be updated to 200 (original key, not alias)
    assert entries.get("category") == "premium"  # category should remain the same


@pytest.mark.anyio
async def test_join_logs_with_copy(client: AsyncClient):
    """Test join logs with copy=True to verify copy behavior (default)."""
    project_name = "test_project_join_copy"
    await _create_project(client, project_name, user=1)

    context_a = "context_A_copy"
    context_b = "context_B_copy"
    joined_context = "joined_copy"

    # Create initial logs
    response_a = await _create_log(
        client,
        project_name,
        context=context_a,
        entries={"user_id": 1, "value": 10},
    )
    assert response_a.status_code == 200

    response_b = await _create_log(
        client,
        project_name,
        context=context_b,
        entries={"user_id": 1, "multiplier": 2},
    )
    assert response_b.status_code == 200

    # Perform join with copy=True (default behavior)
    join_payload = {
        "project": project_name,
        "pair_of_args": [{"context": context_a}, {"context": context_b}],
        "join_expr": "A.user_id == B.user_id",
        "mode": "inner",
        "new_context": joined_context,
        "columns": {
            "A.user_id": "uuid",
            "A.value": "original_value",
            "B.multiplier": "mult",
        },
        "copy": True,  # Explicitly set to True (though it's the default)
    }
    response = await client.post("/v0/logs/join", json=join_payload, headers=HEADERS)
    assert response.status_code == 200

    # Verify the joined result
    response = await client.get(
        f"/v0/logs?project={project_name}&context={joined_context}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json().get("logs", [])
    assert len(logs) == 1

    entries = logs[0].get("entries", {})
    assert entries.get("uuid") == 1
    assert entries.get("original_value") == 10
    assert entries.get("mult") == 2

    # Update the original log in context A
    # Get the log ID from context A first
    response = await client.get(
        f"/v0/logs?project={project_name}&context={context_a}",
        headers=HEADERS,
    )
    log_a = response.json().get("logs", [])[0]
    log_a_id = log_a.get("id")

    # Update the value in the original log
    response = await _update_logs(
        client,
        [log_a_id],
        {"value": 20},
        overwrite=True,
    )
    assert response.status_code == 200

    # Check if the joined context reflects the update (with copy=True)
    response = await client.get(
        f"/v0/logs?project={project_name}&context={joined_context}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json().get("logs", [])
    assert len(logs) == 1

    # With copy=True, the joined logs should NOT reflect the update
    # because they are copies, not references
    entries = logs[0].get("entries", {})
    assert entries.get("uuid") == 1
    assert entries.get("original_value") == 10  # Should still be 10, not 20
    assert entries.get("mult") == 2
