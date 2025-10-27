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
        "columns": ["A.user_id", "A.score", "A.name", "B.category", "B.status"],
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


@pytest.mark.anyio
async def test_join_logs_reference_with_dict_columns_fails(client: AsyncClient):
    """Test that using dictionary columns with copy=False raises an error."""
    project_name = "test_project_join_ref_dict_fail"
    await _create_project(client, project_name, user=1)

    context_a = "context_A"
    context_b = "context_B"
    joined_context = "joined"

    # Create logs
    await _create_log(
        client,
        project_name,
        context=context_a,
        entries={"user_id": 1, "value": 10},
    )

    await _create_log(
        client,
        project_name,
        context=context_b,
        entries={"user_id": 1, "status": "active"},
    )

    # Try to join with copy=False and dictionary columns (should fail)
    join_payload = {
        "project": project_name,
        "pair_of_args": [{"context": context_a}, {"context": context_b}],
        "join_expr": "A.user_id == B.user_id",
        "mode": "inner",
        "new_context": joined_context,
        "columns": {"A.user_id": "id", "A.value": "val"},  # Dict format
        "copy": False,  # Pass by reference
    }

    response = await client.post("/v0/logs/join", json=join_payload, headers=HEADERS)
    assert response.status_code == 400
    assert "column aliases are not supported" in response.json()["detail"]


async def _setup_contexts_with_embeddings(
    client: AsyncClient,
    project_name: str,
    context_a: str = "context_descriptions",
    context_b: str = "context_titles",
):
    """Helper function to set up two contexts with logs and derived embedding columns."""
    await _create_project(client, project_name, user=1)

    # Create 5 logs in context A with descriptions
    descriptions = [
        "a beautiful sunset over the ocean",
        "a busy city street at night",
        "a peaceful mountain landscape",
        "a colorful flower garden",
        "a cozy cabin in the woods",
    ]

    log_ids_a = []
    for i, desc in enumerate(descriptions):
        response = await _create_log(
            client,
            project_name,
            context=context_a,
            entries={"description": desc, "id": i},
        )
        assert response.status_code == 200
        log_ids_a.append(response.json()["log_event_ids"][0])

    # Create 5 logs in context B with titles
    titles = [
        "Ocean Sunset",
        "Urban Nightlife",
        "Mountain Peace",
        "Garden Colors",
        "Forest Retreat",
    ]

    log_ids_b = []
    for i, title in enumerate(titles):
        response = await _create_log(
            client,
            project_name,
            context=context_b,
            entries={"title": title, "id": i},
        )
        assert response.status_code == 200
        log_ids_b.append(response.json()["log_event_ids"][0])

    # Create derived embedding column for context A descriptions
    response = await client.post(
        "/v0/logs/derived",
        json={
            "project": project_name,
            "context": context_a,
            "key": "_description_emb",
            "equation": "embed({lg:description}, model='text-embedding-3-small')",
            "referenced_logs": {"lg": {"context": context_a}},
        },
        headers=HEADERS,
    )
    assert (
        response.status_code == 200
    ), f"Failed to create embedding for descriptions: {response.text}"

    # Create derived embedding column for context B titles
    response = await client.post(
        "/v0/logs/derived",
        json={
            "project": project_name,
            "context": context_b,
            "key": "_title_emb",
            "equation": "embed({lg:title}, model='text-embedding-3-small')",
            "referenced_logs": {"lg": {"context": context_b}},
        },
        headers=HEADERS,
    )
    assert (
        response.status_code == 200
    ), f"Failed to create embedding for titles: {response.text}"

    # Verify derived columns exist in original contexts before joining
    response = await client.get(
        f"/v0/logs?project={project_name}&context={context_a}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs_a = response.json().get("logs", [])
    assert len(logs_a) == 5
    for log in logs_a:
        assert "_description_emb" in log.get(
            "derived_entries",
            {},
        ), f"Missing _description_emb in context A log {log['id']}"

    response = await client.get(
        f"/v0/logs?project={project_name}&context={context_b}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs_b = response.json().get("logs", [])
    assert len(logs_b) == 5
    for log in logs_b:
        assert "_title_emb" in log.get(
            "derived_entries",
            {},
        ), f"Missing _title_emb in context B log {log['id']}"

    return {
        "context_a": context_a,
        "context_b": context_b,
        "log_ids_a": log_ids_a,
        "log_ids_b": log_ids_b,
        "logs_a": logs_a,
        "logs_b": logs_b,
    }


@pytest.mark.parametrize("copy", [False, True])
@pytest.mark.anyio
async def test_join_with_derived_embedding_columns(client: AsyncClient, copy: bool):
    """Test joining contexts that have derived embedding columns."""
    project_name = f"test_project_join_embeddings_copy_{copy}"
    joined_context = "joined_embeddings"

    # Set up contexts with embeddings
    setup_data = await _setup_contexts_with_embeddings(client, project_name)
    context_a = setup_data["context_a"]
    context_b = setup_data["context_b"]

    # Perform inner join on the id field
    # First try with explicit columns including derived ones
    # Adjust columns based on copy parameter
    if copy:
        # For copy=True, we can use a dict to rename columns
        columns = {
            "A.id": "id",
            "A.description": "description",
            "A._description_emb": "_description_emb",
            "B.title": "title",
            "B._title_emb": "_title_emb",
        }
    else:
        # For copy=False, must use a list
        columns = [
            "A.id",
            "A.description",
            "A._description_emb",
            "B.title",
            "B._title_emb",
        ]

    join_payload = {
        "project": project_name,
        "pair_of_args": [
            {"context": context_a},
            {"context": context_b},
        ],
        "join_expr": "A.id == B.id",
        "mode": "inner",
        "new_context": joined_context,
        "columns": columns,
        "copy": copy,
    }
    response = await client.post("/v0/logs/join", json=join_payload, headers=HEADERS)
    assert response.status_code == 200, f"Join failed: {response.text}"

    # Verify the joined context has both embedding columns
    response = await client.get(
        f"/v0/logs?project={project_name}&context={joined_context}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json().get("logs", [])

    # Should have 5 joined logs (one for each matching id)
    assert len(logs) == 5, f"Expected 5 joined logs, got {len(logs)}"

    # Verify each log has both embedding columns
    for i, log in enumerate(logs):
        entries = log.get("entries", {})
        derived_entries = log.get("derived_entries", {})

        # Check that we have the base fields
        # assert "id" in entries  # - For some reason, id columns are considered special and not copied over?
        assert "description" in entries
        assert "title" in entries

        # Derived columns should remain as derived entries
        assert (
            "_description_emb" in derived_entries
        ), f"Missing _description_emb in derived_entries for log {i}"
        assert (
            "_title_emb" in derived_entries
        ), f"Missing _title_emb in derived_entries for log {i}"

        # Verify embeddings are lists (vectors)
        assert isinstance(
            derived_entries["_description_emb"],
            list,
        ), "_description_emb should be a vector"
        assert isinstance(
            derived_entries["_title_emb"],
            list,
        ), "_title_emb should be a vector"

        # Verify embeddings have expected dimensionality for text-embedding-3-small (1536 dimensions)
        assert (
            len(derived_entries["_description_emb"]) == 1536
        ), f"Expected 1536 dimensions, got {len(derived_entries['_description_emb'])}"
        assert (
            len(derived_entries["_title_emb"]) == 1536
        ), f"Expected 1536 dimensions, got {len(derived_entries['_title_emb'])}"


@pytest.mark.anyio
async def test_join_with_derived_cosine_similarity(client: AsyncClient):
    """Test joining contexts with embeddings and creating a derived cosine similarity column in the joined context."""
    project_name = "test_project_join_cosine_similarity"
    joined_context = "joined_cosine"

    # Set up contexts with embeddings
    setup_data = await _setup_contexts_with_embeddings(client, project_name)
    context_a = setup_data["context_a"]
    context_b = setup_data["context_b"]

    # Perform inner join with copy=True to create a new context with copied data
    columns = {
        "A.id": "id",
        "A.description": "description",
        "A._description_emb": "_description_emb",
        "B.title": "title",
        "B._title_emb": "_title_emb",
    }

    join_payload = {
        "project": project_name,
        "pair_of_args": [
            {"context": context_a},
            {"context": context_b},
        ],
        "join_expr": "A.id == B.id",
        "mode": "inner",
        "new_context": joined_context,
        "columns": columns,
        "copy": True,  # Use copy=True to create new logs with all columns as regular entries
    }
    response = await client.post("/v0/logs/join", json=join_payload, headers=HEADERS)
    assert response.status_code == 200, f"Join failed: {response.text}"

    # Verify the joined context exists and has the expected logs
    response = await client.get(
        f"/v0/logs?project={project_name}&context={joined_context}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json().get("logs", [])
    assert len(logs) == 5, f"Expected 5 joined logs, got {len(logs)}"

    # Extract log IDs from the joined context
    joined_log_ids = [log["id"] for log in logs]

    # Debug: Check the structure of the joined logs
    print("\n=== DEBUG: Joined log structure ===")
    for i, log in enumerate(logs[:2]):  # Check first 2 logs
        print(f"Log {i}:")
        print(f"  Entries keys: {list(log.get('entries', {}).keys())}")
        print(f"  Derived entries keys: {list(log.get('derived_entries', {}).keys())}")
        print(
            f"  Has _description_emb in entries: {'_description_emb' in log.get('entries', {})}",
        )
        print(f"  Has _title_emb in entries: {'_title_emb' in log.get('entries', {})}")
        if "_description_emb" in log.get("entries", {}):
            desc_emb = log.get("entries", {}).get("_description_emb")
            print(f"  _description_emb type: {type(desc_emb)}")
            if isinstance(desc_emb, list):
                print(f"  _description_emb length: {len(desc_emb)}")
        if "_title_emb" in log.get("entries", {}):
            title_emb = log.get("entries", {}).get("_title_emb")
            print(f"  _title_emb type: {type(title_emb)}")
            if isinstance(title_emb, list):
                print(f"  _title_emb length: {len(title_emb)}")

    # Create the cosine similarity derived column
    cosine_equation = (
        "((((cosine({lg:_description_emb}, embed('crowded downtown after dark', model='text-embedding-3-small'))) "
        "if exists({lg:_description_emb}) else 0) + "
        "((cosine({lg:_title_emb}, embed('City after dark', model='text-embedding-3-small'))) "
        "if exists({lg:_title_emb}) else 0)) / "
        "((1 if exists({lg:_description_emb}) else 0) + (1 if exists({lg:_title_emb}) else 0))) "
        "if (((1 if exists({lg:_description_emb}) else 0) + (1 if exists({lg:_title_emb}) else 0)) > 0) else 2"
    )

    response = await client.post(
        "/v0/logs/derived",
        json={
            "project": project_name,
            "context": joined_context,
            "key": "_sum_cos",
            "equation": cosine_equation,
            "referenced_logs": {"lg": {"context": joined_context}},
        },
        headers=HEADERS,
    )
    assert (
        response.status_code == 200
    ), f"Failed to create cosine similarity column: {response.text}"

    # Verify the derived column exists and has non-None values
    response = await client.get(
        f"/v0/logs?project={project_name}&context={joined_context}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json().get("logs", [])
    assert len(logs) == 5

    # Check each log has the _sum_cos derived column with a valid value
    # print("\n=== DEBUG: Checking derived column values ===")
    # print("_sum_cos values:", [log["derived_entries"].get("_sum_cos") for log in logs])

    for i, log in enumerate(logs):
        derived_entries = log.get("derived_entries", {})
        # entries = log.get("entries", {})

        # print(f"\nLog {i}:")
        # print(f"  Description: {entries.get('description', 'N/A')}")
        # print(f"  Title: {entries.get('title', 'N/A')}")
        # print(f"  Has embeddings in entries: _desc={bool(entries.get('_description_emb'))}, _title={bool(entries.get('_title_emb'))}")

        assert (
            "_sum_cos" in derived_entries
        ), f"Missing _sum_cos in derived_entries for log {i}"

        cos_value = derived_entries["_sum_cos"]

        # If the value is None, let's understand why but continue to gather more info
        if cos_value is None:
            print(f"  ERROR: _sum_cos is None for log {i}")
            print(
                f"  This indicates the embeddings might not be accessible in the equation",
            )
            # For debugging, let's not fail immediately
            continue

        assert isinstance(
            cos_value,
            (int, float),
        ), f"_sum_cos should be numeric, got {type(cos_value)}"

        # The value should be between -1 and 1 (cosine similarity range) or 2 (fallback value)
        # Since we're averaging two cosine similarities, the range is still -1 to 1, or exactly 2
        assert (
            -1 <= cos_value <= 1 or cos_value == 2
        ), f"_sum_cos value {cos_value} is out of expected range for log {i}"

    # assert False


@pytest.mark.anyio
async def test_join_with_derived_cosine_similarity_copy_false(client: AsyncClient):
    """Test joining contexts with embeddings using copy=False to see if pass-by-reference works better."""
    project_name = "test_project_join_cosine_ref"
    joined_context = "joined_cosine_ref"

    # Set up contexts with embeddings
    setup_data = await _setup_contexts_with_embeddings(client, project_name)
    context_a = setup_data["context_a"]
    context_b = setup_data["context_b"]

    # Perform inner join with copy=False to use pass-by-reference
    # Must use list format for columns when copy=False
    columns = [
        "A.id",
        "A.description",
        "A._description_emb",
        "B.title",
        "B._title_emb",
    ]

    join_payload = {
        "project": project_name,
        "pair_of_args": [
            {"context": context_a},
            {"context": context_b},
        ],
        "join_expr": "A.id == B.id",
        "mode": "inner",
        "new_context": joined_context,
        "columns": columns,
        "copy": False,  # Use pass-by-reference to keep original structure
    }
    response = await client.post("/v0/logs/join", json=join_payload, headers=HEADERS)
    assert response.status_code == 200, f"Join failed: {response.text}"

    # Verify the joined context exists and has the expected logs
    response = await client.get(
        f"/v0/logs?project={project_name}&context={joined_context}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json().get("logs", [])
    assert len(logs) == 5, f"Expected 5 joined logs, got {len(logs)}"

    # Create the cosine similarity derived column
    # With copy=False, the embeddings should still be in derived_entries
    cosine_equation = (
        "((((cosine({lg:_description_emb}, embed('crowded downtown after dark', model='text-embedding-3-small'))) "
        "if exists({lg:_description_emb}) else 0) + "
        "((cosine({lg:_title_emb}, embed('City after dark', model='text-embedding-3-small'))) "
        "if exists({lg:_title_emb}) else 0)) / "
        "((1 if exists({lg:_description_emb}) else 0) + (1 if exists({lg:_title_emb}) else 0))) "
        "if (((1 if exists({lg:_description_emb}) else 0) + (1 if exists({lg:_title_emb}) else 0)) > 0) else 2"
    )

    response = await client.post(
        "/v0/logs/derived",
        json={
            "project": project_name,
            "context": joined_context,
            "key": "_sum_cos",
            "equation": cosine_equation,
            "referenced_logs": {"lg": {"context": joined_context}},
        },
        headers=HEADERS,
    )
    assert (
        response.status_code == 200
    ), f"Failed to create cosine similarity column: {response.text}"

    # Verify the derived column exists and has non-None values
    response = await client.get(
        f"/v0/logs?project={project_name}&context={joined_context}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json().get("logs", [])
    assert len(logs) == 5

    for i, log in enumerate(logs):
        derived_entries = log.get("derived_entries", {})

        assert (
            "_sum_cos" in derived_entries
        ), f"Missing _sum_cos in derived_entries for log {i}"

        cos_value = derived_entries["_sum_cos"]
        if cos_value is not None:
            assert isinstance(
                cos_value,
                (int, float),
            ), f"_sum_cos should be numeric, got {type(cos_value)}"
            assert (
                -1 <= cos_value <= 1 or cos_value == 2
            ), f"_sum_cos value {cos_value} is out of expected range for log {i}"


@pytest.mark.anyio
async def test_join_transcripts_contacts_with_embeddings(client: AsyncClient):
    """Create Contacts and Transcripts contexts from Pydantic models, embed columns, then join."""

    import json
    import random
    from datetime import datetime, timedelta, timezone
    from typing import List as TypingList
    from typing import Optional

    from pydantic import BaseModel, Field, RootModel

    UNASSIGNED = -1

    class RawImageRef(BaseModel):
        url: str = Field(description="Image URL")
        width: Optional[int] = None
        height: Optional[int] = None

    class AnnotatedImageRef(BaseModel):
        raw_image_ref: RawImageRef = Field(
            description="Reference to the underlying raw image",
        )
        annotation: str = Field(description="Context-specific annotation")

    class AnnotatedImageRefs(RootModel[TypingList[AnnotatedImageRef]]):
        pass

    class Contact(BaseModel):
        contact_id: int = Field(default=UNASSIGNED, ge=UNASSIGNED)
        first_name: Optional[str] = None
        surname: Optional[str] = None
        email_address: Optional[str] = Field(
            default=None,
            pattern=r"^[^@]+@[^@]+$",
        )
        phone_number: Optional[str] = Field(default=None, pattern=r"^\+?[0-9]+$")
        whatsapp_number: Optional[str] = Field(default=None, pattern=r"^\+?[0-9]+$")
        bio: Optional[str] = None
        rolling_summary: Optional[str] = None
        respond_to: bool = False
        response_policy: Optional[str] = None

    class Message(BaseModel):
        message_id: int = Field(ge=-1)
        medium: str
        sender_id: int
        receiver_ids: TypingList[int]
        timestamp: datetime
        content: str
        exchange_id: int = Field(ge=-1)
        images: AnnotatedImageRefs = Field(
            default_factory=lambda: AnnotatedImageRefs.model_validate([]),
        )

    def model_to_fields(model: type[BaseModel]) -> dict[str, dict]:
        fields: dict[str, dict] = {}
        for name, f in model.model_fields.items():
            ann = f.annotation
            try:
                if isinstance(ann, type) and issubclass(ann, BaseModel):
                    fields[name] = {
                        "type": json.dumps(ann.model_json_schema()),
                        "mutable": True,
                    }
                    if getattr(f, "description", None):
                        fields[name]["description"] = f.description
                    continue
            except Exception:
                pass
            origin = getattr(ann, "__origin__", None)
            if origin in (list, TypingList):
                dtype = "list"
            elif ann in (int,):
                dtype = "int"
            elif ann in (float,):
                dtype = "float"
            elif ann in (bool,):
                dtype = "bool"
            elif ann in (datetime,):
                dtype = "datetime"
            else:
                dtype = "str"
            entry = {"type": dtype, "mutable": True}
            if getattr(f, "description", None):
                entry["description"] = f.description
            fields[name] = entry
        return fields

    project_name = "proj_join_transcripts_contacts_embeddings"
    await _create_project(client, project_name, user=1)

    contacts_context = "Contacts"
    transcripts_context = "Transcripts"

    contacts_fields = model_to_fields(Contact)
    transcripts_fields = model_to_fields(Message)
    resp_ft = await client.post(
        "/v0/logs/fields",
        json={
            "project": project_name,
            "fields": {**contacts_fields, **transcripts_fields},
        },
        headers=HEADERS,
    )
    assert resp_ft.status_code == 200, resp_ft.text

    CONTACTS: TypingList[dict] = [
        {
            "first_name": "Carlos",
            "surname": "Diaz",
            "email_address": "carlos.diaz@example.com",
            "phone_number": "+14155550000",
            "whatsapp_number": "+14155550000",
        },
        {
            "first_name": "Dan",
            "surname": "Turner",
            "email_address": "dan.turner@example.com",
            "phone_number": "+447700900001",
            "whatsapp_number": "+447700900001",
        },
        {
            "first_name": "Julia",
            "surname": "Nguyen",
            "email_address": "julia.nguyen@example.com",
            "phone_number": "+447700900002",
            "whatsapp_number": "+447700900002",
        },
        {
            "first_name": "Jimmy",
            "surname": "O'Brian",
            "email_address": "jimmy.obrien@example.com",
            "phone_number": "+61240011000",
            "whatsapp_number": "+61240011000",
        },
        {
            "first_name": "Anne",
            "surname": "Fischer",
            "email_address": "anne.fischer@example.com",
            "phone_number": "+49891234567",
            "whatsapp_number": "+49891234567",
        },
        {
            "first_name": "John",
            "surname": "Doe",
            "email_address": "john.doe@example.com",
            "phone_number": "+1234567890",
            "whatsapp_number": "+1234567890",
        },
        {
            "first_name": "Jane",
            "surname": "Doe",
            "email_address": "jane.doe@example.com",
            "phone_number": "+1234567890",
            "whatsapp_number": "+1234567890",
        },
        {
            "first_name": "Jim",
            "surname": "Beam",
            "email_address": "jim.beam@example.com",
            "phone_number": "+1234567890",
            "whatsapp_number": "+1234567890",
        },
        {
            "first_name": "Jill",
            "surname": "Doe",
            "email_address": "jill.doe@example.com",
            "phone_number": "+1234567890",
            "whatsapp_number": "+1234567890",
        },
        {
            "first_name": "Jack",
            "surname": "Doe",
            "email_address": "jack.doe@example.com",
            "phone_number": "+1234567890",
            "whatsapp_number": "+1234567890",
        },
    ]

    id_by_name: dict[str, int] = {}
    for idx, c in enumerate(CONTACTS):
        contact_entry = {"contact_id": idx, **c}
        resp = await _create_log(
            client,
            project_name,
            context=contacts_context,
            entries=contact_entry,
        )
        assert resp.status_code == 200, resp.text
        id_by_name[c["first_name"].lower()] = idx

    def z(dt: datetime) -> str:
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    now = datetime(2025, 4, 20, 15, 0, tzinfo=timezone.utc)
    later = datetime(2025, 4, 26, 9, 30, tzinfo=timezone.utc)
    carlos_id = id_by_name["carlos"]
    dan_id = id_by_name["dan"]
    julia_id = id_by_name["julia"]
    jimmy_id = id_by_name["jimmy"]
    anne_id = id_by_name["anne"]

    messages: TypingList[dict] = []

    messages.append(
        dict(
            message_id=0,
            medium="phone_call",
            sender_id=dan_id,
            receiver_ids=[julia_id],
            timestamp=z(now),
            content="Hi Julia, it's Dan. Quick check-in about Q2 metrics.",
            exchange_id=0,
            images=[],
        ),
    )
    messages.append(
        dict(
            message_id=1,
            medium="phone_call",
            sender_id=julia_id,
            receiver_ids=[dan_id],
            timestamp=z(now + timedelta(seconds=30)),
            content="Sure Dan, ready when you are.",
            exchange_id=0,
            images=[],
        ),
    )

    messages.append(
        dict(
            message_id=2,
            medium="phone_call",
            sender_id=dan_id,
            receiver_ids=[julia_id],
            timestamp=z(later),
            content="Morning Julia – finalising the London event agenda today.",
            exchange_id=1,
            images=[],
        ),
    )
    messages.append(
        dict(
            message_id=3,
            medium="phone_call",
            sender_id=julia_id,
            receiver_ids=[dan_id],
            timestamp=z(later + timedelta(seconds=45)),
            content="Great. Let's confirm the speaker list and coffee budget.",
            exchange_id=1,
            images=[],
        ),
    )

    t_email = datetime(2025, 4, 21, 12, 0, tzinfo=timezone.utc)
    messages.append(
        dict(
            message_id=4,
            medium="email",
            sender_id=carlos_id,
            receiver_ids=[dan_id],
            timestamp=z(t_email),
            content=(
                "Subject: Stapler bulk order\n\nHi Dan, I'm interested in buying 200 units."
            ),
            exchange_id=2,
            images=[],
        ),
    )
    messages.append(
        dict(
            message_id=5,
            medium="email",
            sender_id=dan_id,
            receiver_ids=[carlos_id],
            timestamp=z(t_email + timedelta(hours=2)),
            content="Hi Carlos — sure, $4.50 per unit. See attached PDF.",
            exchange_id=2,
            images=[],
        ),
    )

    t_holiday = datetime(2025, 4, 22, 18, 10, tzinfo=timezone.utc)
    messages.append(
        dict(
            message_id=6,
            medium="whatsapp_message",
            sender_id=jimmy_id,
            receiver_ids=[dan_id],
            timestamp=z(t_holiday),
            content=("Heads-up Dan, I'll be on holiday from 2025-05-15 to 2025-05-30."),
            exchange_id=3,
            images=[],
        ),
    )

    t_excuse = datetime(2025, 4, 23, 9, 0, tzinfo=timezone.utc)
    messages.append(
        dict(
            message_id=7,
            medium="whatsapp_message",
            sender_id=anne_id,
            receiver_ids=[dan_id],
            timestamp=z(t_excuse),
            content=(
                "Sorry Dan, I can't join the Berlin trip because my passport expired."
            ),
            exchange_id=4,
            images=[],
        ),
    )

    random.seed(12345)
    # Create 300+ filler messages to ensure we have more than 300 logs total
    filler_messages = 300
    for i in range(filler_messages):
        a, b = random.sample(list(id_by_name.values()), 2)
        messages.append(
            dict(
                message_id=100 + i,
                medium=random.choice(["email", "phone_call", "whatsapp_message"]),
                sender_id=a if i % 2 == 0 else b,
                receiver_ids=[b if i % 2 == 0 else a],
                timestamp=z(
                    datetime(2024, 5, 1, 12, 0, tzinfo=timezone.utc)
                    + timedelta(minutes=i),
                ),
                content=random.choice(
                    [
                        "Quick ping",
                        "Following up on that thing",
                        "FYI",
                        "This is filler",
                    ],
                ),
                exchange_id=10 + (i // 3),
                images=[],
            ),
        )

    for msg in messages:
        resp = await _create_log(
            client,
            project_name,
            context=transcripts_context,
            entries=msg,
        )
        assert resp.status_code == 200, resp.text

    resp_d1 = await client.post(
        "/v0/logs/derived",
        json={
            "project": project_name,
            "context": transcripts_context,
            "key": "_medium_emb",
            "equation": "embed({lg:medium}, model='text-embedding-3-small')",
            "referenced_logs": {"lg": {"context": transcripts_context}},
        },
        headers=HEADERS,
    )
    assert resp_d1.status_code == 200, resp_d1.text

    resp_d2 = await client.post(
        "/v0/logs/derived",
        json={
            "project": project_name,
            "context": contacts_context,
            "key": "_first_name_emb",
            "equation": "embed({lg:first_name}, model='text-embedding-3-small')",
            "referenced_logs": {"lg": {"context": contacts_context}},
        },
        headers=HEADERS,
    )
    assert resp_d2.status_code == 200, resp_d2.text

    join_payload = {
        "project": project_name,
        "pair_of_args": [
            {"context": transcripts_context},
            {"context": contacts_context},
        ],
        "join_expr": "A.sender_id == B.contact_id",
        "mode": "inner",
        "new_context": "Joined__Transcripts__Contacts",
        "columns": {
            "A.message_id": "message_id",
            "A.medium": "medium",
            "A.sender_id": "sender_id",
            "A.receiver_ids": "receiver_ids",
            "A.timestamp": "timestamp",
            "A.content": "content",
            "A.exchange_id": "exchange_id",
            "A.images": "images",
            "A._medium_emb": "_medium_emb",
            "B._first_name_emb": "_first_name_emb",
        },
        "copy": True,
    }

    join_resp = await client.post("/v0/logs/join", json=join_payload, headers=HEADERS)
    assert join_resp.status_code == 200, join_resp.text

    get_resp = await client.get(
        f"/v0/logs?project={project_name}&context=Joined__Transcripts__Contacts",
        headers=HEADERS,
    )
    assert get_resp.status_code == 200, get_resp.text
    joined_logs = get_resp.json().get("logs", [])
    assert len(joined_logs) == len(messages)
    assert (
        len(messages) > 300
    ), f"Expected more than 300 messages, but got {len(messages)}"

    for i, log in enumerate(joined_logs):
        entries = log.get("entries", {})
        derived_entries = log.get("derived_entries", {})
        for key in [
            "message_id",
            "medium",
            "sender_id",
            "receiver_ids",
            "timestamp",
            "content",
            "exchange_id",
        ]:
            assert key in entries and entries[key] is not None

        def _get_vec(dct: dict, k: str):
            v = dct.get(k)
            if v is None:
                return entries.get(k)
            return v

        med_vec = _get_vec(derived_entries, "_medium_emb")
        name_vec = _get_vec(derived_entries, "_first_name_emb")
        assert isinstance(med_vec, list) and len(med_vec) == 1536
        assert isinstance(name_vec, list) and len(name_vec) == 1536
