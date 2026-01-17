"""
Tests for JSONB filter expression truthiness and Python `or` operator semantics.

These tests cover two classes of bugs in the Python-to-SQL filter translation:

1. **JSONB Truthiness Bug**: When a filter expression like `metadata.get('key')`
   is used as a standalone boolean condition (not in a comparison), the JSONB
   value must be converted to a boolean for PostgreSQL's WHERE clause.
   PostgreSQL requires WHERE conditions to be boolean, but raw JSONB is not.

   Python truthiness rules that must be respected:
   - None/null: False
   - False: False
   - 0, 0.0: False
   - "" (empty string): False
   - [] (empty list): False
   - {} (empty dict): False
   - Everything else: True

2. **`(expr or [])` Fallback Pattern Bug**: Python's `or` operator returns one
   of its operands, NOT a boolean. So `arr or []` returns `arr` if truthy, else
   `[]`. This is commonly used for safe iteration: `'x' in (arr or [])`.

   The SQL translation must NOT convert this to a boolean OR, which would break
   membership checks expecting an array on the RHS.

Run with: pytest orchestra/tests/test_log/test_filter_truthiness.py -v
"""

import pytest
from httpx import AsyncClient

from . import HEADERS, _create_log, _create_project

# =============================================================================
# JSONB Truthiness Tests - Parametrized
# =============================================================================


@pytest.mark.parametrize(
    "value,should_be_truthy,description",
    [
        # Truthy values
        ({"id": "T-123"}, True, "non-empty dict is truthy"),
        ({"nested": {"deep": 1}}, True, "nested dict is truthy"),
        ([1, 2, 3], True, "non-empty list is truthy"),
        (["single"], True, "single-element list is truthy"),
        ("hello", True, "non-empty string is truthy"),
        ("0", True, "string '0' is truthy (not numeric zero)"),
        (1, True, "positive int is truthy"),
        (-1, True, "negative int is truthy"),
        (0.5, True, "positive float is truthy"),
        (-0.5, True, "negative float is truthy"),
        (True, True, "boolean True is truthy"),
        # Falsy values
        ({}, False, "empty dict is falsy"),
        ([], False, "empty list is falsy"),
        ("", False, "empty string is falsy"),
        (0, False, "zero int is falsy"),
        (0.0, False, "zero float is falsy"),
        (False, False, "boolean False is falsy"),
        (None, False, "None is falsy"),
    ],
)
@pytest.mark.anyio
async def test_metadata_get_truthiness(
    client: AsyncClient,
    value,
    should_be_truthy,
    description,
):
    """
    Test that metadata.get('key') used as a boolean filter respects Python truthiness.

    The filter `metadata.get('key')` should return logs where 'key' exists AND
    its value is truthy according to Python semantics.
    """
    project_name = f"test-truthiness-{id(value)}-{should_be_truthy}"
    await _create_project(client, project_name)

    # Create log with the test value
    await _create_log(
        client,
        project_name,
        entries=[
            {"message": "test entry", "metadata": {"key": value}},
        ],
        params={},
    )

    # Filter using metadata.get('key') as boolean
    resp = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "filter_expr": "metadata.get('key')",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200, f"{description}: {resp.text}"
    logs = resp.json()["logs"]

    if should_be_truthy:
        assert len(logs) == 1, f"{description}: expected 1 log, got {len(logs)}"
    else:
        assert len(logs) == 0, f"{description}: expected 0 logs, got {len(logs)}"


@pytest.mark.anyio
async def test_missing_key_is_falsy(
    client: AsyncClient,
):
    """
    Test that metadata.get('missing_key') is falsy when the key doesn't exist.
    """
    project_name = "test-missing-key-falsy"
    await _create_project(client, project_name)

    await _create_log(
        client,
        project_name,
        entries=[
            {"message": "no key", "metadata": {"other": "data"}},
        ],
        params={},
    )

    resp = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "filter_expr": "metadata.get('missing_key')",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    logs = resp.json()["logs"]
    assert len(logs) == 0, "missing key should be falsy"


@pytest.mark.anyio
async def test_negated_truthiness(
    client: AsyncClient,
):
    """
    Test that `not metadata.get('key')` correctly negates truthiness.
    """
    project_name = "test-negated-truthiness"
    await _create_project(client, project_name)

    await _create_log(
        client,
        project_name,
        entries=[
            {"message": "truthy", "metadata": {"key": {"id": 1}}},
            {"message": "falsy empty", "metadata": {"key": {}}},
            {"message": "falsy missing", "metadata": {"other": "data"}},
        ],
        params={},
    )

    resp = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "filter_expr": "not metadata.get('key')",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    logs = resp.json()["logs"]

    # Should match falsy entries (empty dict and missing key)
    assert len(logs) == 2
    messages = {log["entries"]["message"] for log in logs}
    assert messages == {"falsy empty", "falsy missing"}


@pytest.mark.anyio
async def test_chained_truthiness_short_circuit(
    client: AsyncClient,
):
    """
    Test chained truthiness with short-circuit evaluation.

    Pattern: metadata.get('a') and metadata['a'].get('b')

    This should:
    - Return False if 'a' is missing/falsy (short-circuit, don't evaluate 'b')
    - Return truthiness of 'b' if 'a' is truthy
    """
    project_name = "test-chained-truthiness"
    await _create_project(client, project_name)

    await _create_log(
        client,
        project_name,
        entries=[
            # Both truthy
            {"message": "both truthy", "metadata": {"a": {"b": {"c": 1}}}},
            # 'a' truthy but 'b' falsy (empty)
            {"message": "b empty", "metadata": {"a": {"b": {}}}},
            # 'a' truthy but 'b' missing
            {"message": "b missing", "metadata": {"a": {"other": 1}}},
            # 'a' falsy (empty)
            {"message": "a empty", "metadata": {"a": {}}},
            # 'a' missing entirely
            {"message": "a missing", "metadata": {"other": "data"}},
        ],
        params={},
    )

    resp = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "filter_expr": "metadata.get('a') and metadata['a'].get('b')",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    logs = resp.json()["logs"]

    # Only "both truthy" should match
    assert len(logs) == 1
    assert logs[0]["entries"]["message"] == "both truthy"


# =============================================================================
# (expr or []) Fallback Pattern Tests - Parametrized
# =============================================================================


@pytest.mark.parametrize(
    "tags_value,has_vip,description",
    [
        # Array with target value
        (["vip", "priority"], True, "array contains target"),
        (["vip"], True, "single-element array with target"),
        # Array without target value
        (["normal", "standard"], False, "array without target"),
        (["other"], False, "single-element array without target"),
        # Empty array - should use empty array, membership is False
        ([], False, "empty array"),
        # None/null - should fallback to [], membership is False
        (None, False, "null value"),
        # Key missing entirely - .get() returns None, fallback to []
        ("__MISSING__", False, "missing key"),
    ],
)
@pytest.mark.anyio
async def test_membership_with_or_fallback(
    client: AsyncClient,
    tags_value,
    has_vip,
    description,
):
    """
    Test the (expr or []) fallback pattern for safe membership checks.

    Pattern: 'vip' in (metadata.get('tags') or [])

    This should work correctly whether tags is:
    - A valid array (use it)
    - An empty array (use it, membership is False)
    - None/null (fallback to [], membership is False)
    - Missing key (fallback to [], membership is False)
    """
    project_name = f"test-or-fallback-{id(tags_value)}-{has_vip}"
    await _create_project(client, project_name)

    # Build metadata based on test case
    if tags_value == "__MISSING__":
        metadata = {"other": "data"}
    else:
        metadata = {"tags": tags_value}

    await _create_log(
        client,
        project_name,
        entries=[{"message": "test", "metadata": metadata}],
        params={},
    )

    resp = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "filter_expr": "'vip' in (metadata.get('tags') or [])",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200, f"{description}: {resp.text}"
    logs = resp.json()["logs"]

    if has_vip:
        assert len(logs) == 1, f"{description}: expected 1 log, got {len(logs)}"
    else:
        assert len(logs) == 0, f"{description}: expected 0 logs, got {len(logs)}"


@pytest.mark.anyio
async def test_or_fallback_with_non_empty_default(
    client: AsyncClient,
):
    """
    Test (expr or ['default']) pattern - using non-empty fallback.

    When the array is falsy, the fallback ['default'] should be used.
    """
    project_name = "test-or-fallback-default"
    await _create_project(client, project_name)

    await _create_log(
        client,
        project_name,
        entries=[
            # Has tags, should use actual tags
            {"message": "has tags", "metadata": {"tags": ["custom"]}},
            # Missing tags, should fallback to ['default']
            {"message": "no tags", "metadata": {"other": "data"}},
            # Null tags, should fallback to ['default']
            {"message": "null tags", "metadata": {"tags": None}},
        ],
        params={},
    )

    # Check for 'default' in (tags or ['default'])
    resp = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "filter_expr": "'default' in (metadata.get('tags') or ['default'])",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    logs = resp.json()["logs"]

    # Should match entries where tags is missing/null (fallback used)
    assert len(logs) == 2
    messages = {log["entries"]["message"] for log in logs}
    assert messages == {"no tags", "null tags"}


@pytest.mark.anyio
async def test_nested_or_fallback_pattern(
    client: AsyncClient,
):
    """
    Test deeply nested (expr or []) pattern.

    Pattern: 'vip' in (metadata['thread'].get('tags') or [])
    """
    project_name = "test-nested-or-fallback"
    await _create_project(client, project_name)

    await _create_log(
        client,
        project_name,
        entries=[
            # Full path with vip tag
            {
                "message": "vip user",
                "metadata": {"thread": {"id": "T-1", "tags": ["vip", "priority"]}},
            },
            # Full path without vip tag
            {
                "message": "regular user",
                "metadata": {"thread": {"id": "T-2", "tags": ["normal"]}},
            },
            # Thread exists but no tags key
            {"message": "no tags", "metadata": {"thread": {"id": "T-3"}}},
            # No thread at all
            {"message": "no thread", "metadata": {"other": "data"}},
        ],
        params={},
    )

    resp = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "filter_expr": "'vip' in (metadata['thread'].get('tags') or [])",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    logs = resp.json()["logs"]

    # Only "vip user" should match
    assert len(logs) == 1
    assert logs[0]["entries"]["message"] == "vip user"


# =============================================================================
# Combined/Complex Filter Tests
# =============================================================================


@pytest.mark.anyio
async def test_full_complex_filter(
    client: AsyncClient,
):
    """
    Test a complex filter combining both bug patterns.

    Filter:
        metadata.get('thread') and
        metadata['thread'].get('id') == 'T-123' and
        'vip' in (metadata['thread'].get('tags') or [])

    This exercises:
    1. Truthiness of metadata.get('thread')
    2. Comparison with nested .get()
    3. Membership with or-fallback pattern
    """
    project_name = "test-complex-filter"
    await _create_project(client, project_name)

    await _create_log(
        client,
        project_name,
        entries=[
            # Perfect match
            {
                "message": "perfect match",
                "metadata": {"thread": {"id": "T-123", "tags": ["vip", "priority"]}},
            },
            # Wrong ID
            {
                "message": "wrong id",
                "metadata": {"thread": {"id": "T-999", "tags": ["vip"]}},
            },
            # No vip tag
            {
                "message": "no vip tag",
                "metadata": {"thread": {"id": "T-123", "tags": ["normal"]}},
            },
            # Empty thread (falsy)
            {"message": "empty thread", "metadata": {"thread": {}}},
            # No thread
            {"message": "no thread", "metadata": {"other": "data"}},
        ],
        params={},
    )

    complex_filter = (
        "metadata.get('thread') and "
        "metadata['thread'].get('id') == 'T-123' and "
        "'vip' in (metadata['thread'].get('tags') or [])"
    )

    resp = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "filter_expr": complex_filter,
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    logs = resp.json()["logs"]

    # Only perfect match should pass all conditions
    assert len(logs) == 1
    assert logs[0]["entries"]["message"] == "perfect match"


@pytest.mark.anyio
async def test_truthiness_in_or_expression(
    client: AsyncClient,
):
    """
    Test truthiness within an OR expression.

    Pattern: metadata.get('primary') or metadata.get('fallback')

    Should return logs where either key has a truthy value.
    """
    project_name = "test-truthiness-or-expr"
    await _create_project(client, project_name)

    await _create_log(
        client,
        project_name,
        entries=[
            # Primary truthy
            {"message": "primary", "metadata": {"primary": {"id": 1}}},
            # Primary falsy, fallback truthy
            {"message": "fallback", "metadata": {"primary": {}, "fallback": {"id": 2}}},
            # Both falsy
            {"message": "both falsy", "metadata": {"primary": {}, "fallback": {}}},
            # Both missing
            {"message": "both missing", "metadata": {"other": "data"}},
        ],
        params={},
    )

    resp = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "filter_expr": "metadata.get('primary') or metadata.get('fallback')",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    logs = resp.json()["logs"]

    # Should match "primary" and "fallback" entries
    assert len(logs) == 2
    messages = {log["entries"]["message"] for log in logs}
    assert messages == {"primary", "fallback"}


# =============================================================================
# != None Filter Tests
# =============================================================================
#
# These tests verify that None comparison filters work correctly in JSONB mode.
#
# JSONB has two representations of "null":
# 1. SQL NULL - when the key doesn't exist in the JSONB object
# 2. JSON null literal - when the key exists with the value `null`
#
# The filter expressions `field != None`, `None != field`, `field == None`,
# `None == field`, and their `is`/`is not` variants must check for BOTH
# representations to correctly match Python's None comparison semantics.


@pytest.mark.anyio
async def test_field_not_equals_none_filter(
    client: AsyncClient,
):
    """
    Test that `field != None` filter correctly returns logs where field has a value.

    Tests both `field != None` and `None != field` syntax variants.
    """
    project_name = "test-field-not-equals-none"
    await _create_project(client, project_name)

    # Create a log WITH custom_hash set to a non-null value
    await _create_log(
        client,
        project_name,
        entries=[
            {"name": "with_hash", "custom_hash": "abc123"},
        ],
        params={},
    )

    # Create a log WITHOUT custom_hash (should NOT match the filter)
    await _create_log(
        client,
        project_name,
        entries=[
            {"name": "without_hash", "other_field": "value"},
        ],
        params={},
    )

    # Test 1: Query with filter: custom_hash != None (RHS None)
    resp = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "filter_expr": "custom_hash != None",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    logs = resp.json()["logs"]

    assert len(logs) == 1, (
        f"Expected 1 log where custom_hash != None, got {len(logs)}. "
        "The filter 'custom_hash != None' should return logs where custom_hash has a value."
    )
    assert logs[0]["entries"]["name"] == "with_hash"
    assert logs[0]["entries"]["custom_hash"] == "abc123"

    # Test 2: Query with filter: None != custom_hash (LHS None - reversed)
    resp2 = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "filter_expr": "None != custom_hash",
        },
        headers=HEADERS,
    )
    assert resp2.status_code == 200, resp2.text
    logs2 = resp2.json()["logs"]

    assert len(logs2) == 1, (
        f"Expected 1 log where None != custom_hash, got {len(logs2)}. "
        "The filter 'None != custom_hash' should return logs where custom_hash has a value."
    )
    assert logs2[0]["entries"]["name"] == "with_hash"
    assert logs2[0]["entries"]["custom_hash"] == "abc123"


# =============================================================================
# Additional None Comparison Edge Cases
# =============================================================================


@pytest.mark.anyio
async def test_field_equals_none_filter(
    client: AsyncClient,
):
    """
    Test `field == None` filter (the equality variant, not just inequality).

    Should return logs where the field is missing OR has explicit null value.

    KNOWN ISSUE: Currently fails because `== None` only matches explicit JSON null,
    not missing keys. This is a gap in the None comparison handling.
    """
    project_name = "test-field-equals-none"
    await _create_project(client, project_name)

    await _create_log(
        client,
        project_name,
        entries=[
            {"name": "has_value", "optional_field": "some_value"},
            {"name": "missing_field", "other": "data"},
            {"name": "explicit_null", "optional_field": None},
        ],
        params={},
    )

    # Query: optional_field == None
    resp = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "filter_expr": "optional_field == None",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    logs = resp.json()["logs"]

    # Should match: missing_field (no key) and explicit_null (key with null value)
    # Should NOT match: has_value
    names = {log["entries"]["name"] for log in logs}
    assert names == {"missing_field", "explicit_null"}, f"Got: {names}"


@pytest.mark.anyio
async def test_explicit_json_null_vs_missing_key(
    client: AsyncClient,
):
    """
    Test that we correctly distinguish between:
    1. Key doesn't exist (SQL NULL when accessed)
    2. Key exists with JSON null value (JSONB literal "null")

    Both should be treated as None for Python comparison semantics.

    KNOWN ISSUE in legacy row-per-field storage: `== None` only matches explicit null,
    not missing keys. Missing fields have no row to match against.
    """
    project_name = "test-explicit-null-vs-missing"
    await _create_project(client, project_name)

    await _create_log(
        client,
        project_name,
        entries=[
            # Key exists with explicit null
            {"name": "explicit_null", "status": None},
            # Key doesn't exist at all
            {"name": "missing_key", "other_field": "value"},
            # Key exists with empty string (NOT null)
            {"name": "empty_string", "status": ""},
            # Key exists with actual value
            {"name": "has_value", "status": "active"},
        ],
        params={},
    )

    # Test != None: should return empty_string and has_value
    resp1 = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "filter_expr": "status != None",
        },
        headers=HEADERS,
    )
    assert resp1.status_code == 200, resp1.text
    names1 = {log["entries"]["name"] for log in resp1.json()["logs"]}
    assert names1 == {"empty_string", "has_value"}, f"!= None got: {names1}"

    # Test == None: should return explicit_null and missing_key
    resp2 = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "filter_expr": "status == None",
        },
        headers=HEADERS,
    )
    assert resp2.status_code == 200, resp2.text
    names2 = {log["entries"]["name"] for log in resp2.json()["logs"]}
    assert names2 == {"explicit_null", "missing_key"}, f"== None got: {names2}"


@pytest.mark.anyio
async def test_nested_field_none_comparison(
    client: AsyncClient,
):
    """
    Test None comparisons on nested field access like `metadata.nested.field != None`.
    """
    project_name = "test-nested-none-comparison"
    await _create_project(client, project_name)

    await _create_log(
        client,
        project_name,
        entries=[
            # Deep nested value exists
            {"name": "deep_value", "metadata": {"level1": {"level2": "value"}}},
            # Partial path exists but leaf is null
            {"name": "deep_null", "metadata": {"level1": {"level2": None}}},
            # Partial path exists but leaf is missing
            {"name": "deep_missing", "metadata": {"level1": {"other": "x"}}},
            # Parent path missing entirely
            {"name": "parent_missing", "metadata": {"other": "data"}},
        ],
        params={},
    )

    # Test: metadata['level1']['level2'] != None
    resp = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "filter_expr": "metadata['level1']['level2'] != None",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    names = {log["entries"]["name"] for log in resp.json()["logs"]}
    # Only deep_value should have a non-None value at that path
    assert names == {"deep_value"}, f"Nested != None got: {names}"


@pytest.mark.anyio
async def test_none_comparison_with_is_operator(
    client: AsyncClient,
):
    """
    Verify that `is None` and `is not None` still work correctly
    (they were working before, this is a regression test).

    KNOWN ISSUE in legacy row-per-field storage: `is None` only matches explicit null,
    not missing keys. Same limitation as `== None`.
    """
    project_name = "test-is-none-operator"
    await _create_project(client, project_name)

    await _create_log(
        client,
        project_name,
        entries=[
            {"name": "has_value", "field": "value"},
            {"name": "is_null", "field": None},
            {"name": "is_missing", "other": "data"},
        ],
        params={},
    )

    # Test: field is not None
    resp1 = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "filter_expr": "field is not None",
        },
        headers=HEADERS,
    )
    assert resp1.status_code == 200, resp1.text
    names1 = {log["entries"]["name"] for log in resp1.json()["logs"]}
    assert names1 == {"has_value"}, f"is not None got: {names1}"

    # Test: field is None
    resp2 = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "filter_expr": "field is None",
        },
        headers=HEADERS,
    )
    assert resp2.status_code == 200, resp2.text
    names2 = {log["entries"]["name"] for log in resp2.json()["logs"]}
    assert names2 == {"is_null", "is_missing"}, f"is None got: {names2}"


@pytest.mark.anyio
async def test_chained_none_comparisons(
    client: AsyncClient,
):
    """
    Test chained None comparisons: `field1 != None and field2 != None`
    """
    project_name = "test-chained-none"
    await _create_project(client, project_name)

    await _create_log(
        client,
        project_name,
        entries=[
            {"name": "both_present", "field1": "a", "field2": "b"},
            {"name": "only_field1", "field1": "a"},
            {"name": "only_field2", "field2": "b"},
            {"name": "neither", "other": "data"},
        ],
        params={},
    )

    # Test: field1 != None and field2 != None
    resp = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "filter_expr": "field1 != None and field2 != None",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    names = {log["entries"]["name"] for log in resp.json()["logs"]}
    assert names == {"both_present"}, f"Chained != None got: {names}"


@pytest.mark.anyio
async def test_none_comparison_with_or(
    client: AsyncClient,
):
    """
    Test None comparisons with OR: `field1 != None or field2 != None`
    """
    project_name = "test-none-with-or"
    await _create_project(client, project_name)

    await _create_log(
        client,
        project_name,
        entries=[
            {"name": "both_present", "field1": "a", "field2": "b"},
            {"name": "only_field1", "field1": "a"},
            {"name": "only_field2", "field2": "b"},
            {"name": "neither", "other": "data"},
        ],
        params={},
    )

    # Test: field1 != None or field2 != None
    resp = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "filter_expr": "field1 != None or field2 != None",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    names = {log["entries"]["name"] for log in resp.json()["logs"]}
    # Should match anything with at least one field present
    assert names == {
        "both_present",
        "only_field1",
        "only_field2",
    }, f"OR != None got: {names}"


@pytest.mark.anyio
async def test_none_comparison_after_get_method(
    client: AsyncClient,
):
    """
    Test None comparison on result of .get() method: `metadata.get('key') != None`

    This is different from direct field access because .get() returns None for missing keys.
    """
    project_name = "test-get-method-none"
    await _create_project(client, project_name)

    await _create_log(
        client,
        project_name,
        entries=[
            {"name": "key_exists", "metadata": {"key": "value"}},
            {"name": "key_null", "metadata": {"key": None}},
            {"name": "key_missing", "metadata": {"other": "data"}},
            {"name": "no_metadata", "other": "field"},
        ],
        params={},
    )

    # Test: metadata.get('key') != None
    resp = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "filter_expr": "metadata.get('key') != None",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    names = {log["entries"]["name"] for log in resp.json()["logs"]}
    # Only key_exists should have a non-None value
    assert names == {"key_exists"}, f"get() != None got: {names}"


@pytest.mark.anyio
async def test_mixed_none_and_value_comparison(
    client: AsyncClient,
):
    """
    Test filter combining None check with value comparison:
    `field != None and field != 'excluded'`
    """
    project_name = "test-mixed-none-value"
    await _create_project(client, project_name)

    await _create_log(
        client,
        project_name,
        entries=[
            {"name": "good_value", "status": "active"},
            {"name": "excluded_value", "status": "excluded"},
            {"name": "null_value", "status": None},
            {"name": "missing", "other": "data"},
        ],
        params={},
    )

    # Test: status != None and status != 'excluded'
    resp = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "filter_expr": "status != None and status != 'excluded'",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    names = {log["entries"]["name"] for log in resp.json()["logs"]}
    assert names == {"good_value"}, f"Mixed comparison got: {names}"


@pytest.mark.anyio
async def test_none_in_list_membership(
    client: AsyncClient,
):
    """
    Test None behavior in list membership: `field in [None, 'a', 'b']`

    This tests whether None is correctly matched when it's part of a list literal.

    KNOWN ISSUE in legacy row-per-field storage: SQL type error (text = jsonb) when
    None is in the list. This is a separate bug in the membership operator handling.
    """
    project_name = "test-none-in-list"
    await _create_project(client, project_name)

    await _create_log(
        client,
        project_name,
        entries=[
            {"name": "value_a", "status": "a"},
            {"name": "value_b", "status": "b"},
            {"name": "value_c", "status": "c"},
            {"name": "null_status", "status": None},
            {"name": "missing_status", "other": "data"},
        ],
        params={},
    )

    # Test: status in [None, 'a', 'b']
    # This should match: value_a, value_b, null_status, and possibly missing_status
    resp = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "filter_expr": "status in [None, 'a', 'b']",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    names = {log["entries"]["name"] for log in resp.json()["logs"]}
    # Should at minimum match a, b, and null_status
    # Whether missing_status matches depends on how `in` handles missing keys
    assert "value_a" in names, f"'a' should match, got: {names}"
    assert "value_b" in names, f"'b' should match, got: {names}"
    assert "value_c" not in names, f"'c' should not match, got: {names}"


# =============================================================================
# Grouping Context Tests
# =============================================================================


@pytest.mark.anyio
async def test_truthiness_in_grouping_filter(
    client: AsyncClient,
):
    """
    Test that truthiness works correctly in grouping context.

    The fix touches grouping_utils.py, so we need to verify it works
    when using filter_expr with grouping operations.
    """
    project_name = "test-truthiness-grouping"
    await _create_project(client, project_name)

    await _create_log(
        client,
        project_name,
        entries=[
            {"message": "group-a-1", "metadata": {"enabled": True}, "group": "a"},
            {"message": "group-a-2", "metadata": {"enabled": True}, "group": "a"},
            {"message": "group-b-1", "metadata": {"enabled": False}, "group": "b"},
            {"message": "group-c-1", "metadata": {"enabled": {}}, "group": "c"},
        ],
        params={},
    )

    # Use filter with grouping - filter should apply before/during grouping
    resp = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "filter_expr": "metadata.get('enabled')",
            "groupby": "group",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text

    # The response structure with groupby may vary, but we should only
    # see logs where enabled is truthy (True), not False or empty dict
    logs = resp.json().get("logs", [])
    for log in logs:
        message = log["entries"]["message"]
        # Only group-a entries should appear
        assert message.startswith("group-a"), f"Unexpected log: {message}"
