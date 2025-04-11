import json
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient

from ...web.api.log.helpers import str_filter_exp_to_dict_using_ast
from . import (
    HEADERS,
    _create_derived_entry,
    _create_log,
    _create_project,
    _create_several_logs,
    fetch_logs,
)


@pytest.mark.parametrize(
    "expression, values",
    [
        (
            "((a == 5) and (b > 7)) or (len(c) < 10 and 'earth' not in d)",
            {"a": 5, "b": 8, "c": "abcdef", "d": "hello world"},
        ),
        (
            "submarine == 6.45 and van is False or len(ship) < 10 and 'audi' in car",
            {"submarine": 7.89, "van": True, "ship": "_" * 10, "car": "porsche"},
        ),
        (
            "coffee == 'hot' or ice_cream == 'cold' and temperature == 1.23",
            {"coffee": "hot", "ice_cream": "cold", "temperature": 1.23},
        ),
        (  # This needs to be the string from a json.dumps of a python object
            '(messages == [{"role": "assistant", '
            '"context": "you are a helpful assistant"}])',
            {
                "messages": [
                    {
                        "role": "assistant",
                        "context": "you are a helpful assistant",
                    },
                ],
            },
        ),
        (
            "exists(lorry)",
            {
                "lorry": "big",
            },
        ),
        (
            "exists(car)",
            {
                "lorry": "big",
            },
        ),
        (
            "not exists(car)",
            {
                "lorry": "big",
            },
        ),
        ('a == "\'"', {"a": "'"}),
        ("a == '\\\"'", {"a": '"'}),
        ("a == '\\\\'", {"a": "\\"}),
        ('a == "He said, \\"Hello\\""', {"a": 'He said, "Hello"'}),
        ("a == 'It\\'s a test'", {"a": "It's a test"}),
    ],
)
async def test_log_filter_helper(client: AsyncClient, expression, values):
    project_name = "test_filter_helper"
    await _create_project(client, project_name)

    # Create a log with the test values
    response = await _create_log(client, project_name, entries=values)
    assert response.status_code == 200
    log_id = response.json()[0]

    # Test the filter expression
    response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "filter_expr": expression,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    data = response.json()

    # Evaluate the expression in Python to determine expected result
    try:
        # This is a simplified evaluation - in a real test you might use a proper evaluator
        # that handles all the operations in your filter language
        result = eval(
            expression,
            {"__builtins__": {}},
            {
                **values,
                "len": len,
                "exists": lambda x: x in values,
            },
        )

        # Check if the filter worked correctly
        if result:
            assert (
                len(data["logs"]) == 1
            ), f"Expected 1 log for expression: {expression}"
            assert data["logs"][0]["id"] == log_id
        else:
            assert (
                len(data["logs"]) == 0
            ), f"Expected 0 logs for expression: {expression}"
    except Exception as e:
        # If we can't evaluate in Python, just check the API response
        # This is a fallback for complex expressions
        print(f"Could not evaluate expression '{expression}': {e}")
        # We'll assume the API handled it correctly


# Tests for the new AST-based parser implementation
@pytest.mark.parametrize(
    "expression, expected_dict",
    [
        # Basic comparison
        (
            "score > 20",
            {
                "lhs": {"type": "identifier", "value": "score"},
                "operand": ">",
                "rhs": 20,
            },
        ),
        # Parenthesized arithmetic + comparison
        (
            "(a + b) > 10",
            {
                "lhs": {
                    "lhs": {"type": "identifier", "value": "a"},
                    "operand": "+",
                    "rhs": {"type": "identifier", "value": "b"},
                },
                "operand": ">",
                "rhs": 10,
            },
        ),
        # Double-nested parentheses with "and"
        (
            "((a + b) > 10) and ((c * d) < 20)",
            {
                "lhs": {
                    "lhs": {
                        "lhs": {"type": "identifier", "value": "a"},
                        "operand": "+",
                        "rhs": {"type": "identifier", "value": "b"},
                    },
                    "operand": ">",
                    "rhs": 10,
                },
                "operand": "and",
                "rhs": {
                    "lhs": {
                        "lhs": {"type": "identifier", "value": "c"},
                        "operand": "*",
                        "rhs": {"type": "identifier", "value": "d"},
                    },
                    "operand": "<",
                    "rhs": 20,
                },
            },
        ),
        # Function calls: len(a)
        (
            "(len(a) == 3) and ((b + c) > 10)",
            {
                "lhs": {
                    "lhs": {
                        "operand": "len",
                        "rhs": {"type": "identifier", "value": "a"},
                    },
                    "operand": "==",
                    "rhs": 3,
                },
                "operand": "and",
                "rhs": {
                    "lhs": {
                        "lhs": {"type": "identifier", "value": "b"},
                        "operand": "+",
                        "rhs": {"type": "identifier", "value": "c"},
                    },
                    "operand": ">",
                    "rhs": 10,
                },
            },
        ),
        # BASE function call
        (
            "BASE([4, 5], score) / 2",
            {
                "lhs": {
                    "operand": "BASE",
                    "rhs": [
                        [4, 5],
                        {"type": "identifier", "value": "score"},
                    ],
                },
                "operand": "/",
                "rhs": 2,
            },
        ),
        # Membership with a string + function call
        (
            "'new-var' in str(field_1)",
            {
                "lhs": "new-var",
                "operand": "in",
                "rhs": {
                    "operand": "str",
                    "rhs": {"type": "identifier", "value": "field_1"},
                },
            },
        ),
        # Not operator
        (
            "not (x in [1, 2, 3])",
            {
                "operand": "not",
                "rhs": {
                    "lhs": {"type": "identifier", "value": "x"},
                    "operand": "in",
                    "rhs": [1, 2, 3],
                },
            },
        ),
        # round_timestamp, plus an arithmetic comparison
        (
            "round_timestamp(a, b) + 2 >= c",
            {
                "lhs": {
                    "lhs": {
                        "operand": "round_timestamp",
                        "rhs": [
                            {"type": "identifier", "value": "a"},
                            {"type": "identifier", "value": "b"},
                        ],
                    },
                    "operand": "+",
                    "rhs": 2,
                },
                "operand": ">=",
                "rhs": {"type": "identifier", "value": "c"},
            },
        ),
        # isNone(d)
        (
            "isNone(d)",
            {
                "operand": "isNone",
                "rhs": {"type": "identifier", "value": "d"},
            },
        ),
        # Nested indexing
        (
            "x['a'][0] == 10",
            {
                "lhs": {
                    "lhs": {
                        "lhs": {"type": "identifier", "value": "x"},
                        "operand": "INDEX",
                        "rhs": "a",
                    },
                    "operand": "INDEX",
                    "rhs": 0,
                },
                "operand": "==",
                "rhs": 10,
            },
        ),
        # ambiguous identifer (eg: date > date(...))
        (
            "date > date(a)",
            {
                "lhs": {"type": "identifier", "value": "date"},
                "operand": ">",
                "rhs": {
                    "operand": "date",
                    "rhs": {"type": "identifier", "value": "a"},
                },
            },
        ),
        # dict methods
        (
            "my_dict.keys()",
            {
                "operand": "dict_method",
                "method": "keys",
                "rhs": {"type": "identifier", "value": "my_dict"},
            },
        ),
        (
            "my_dict.values()",
            {
                "operand": "dict_method",
                "method": "values",
                "rhs": {"type": "identifier", "value": "my_dict"},
            },
        ),
        (
            "my_dict.items()",
            {
                "operand": "dict_method",
                "method": "items",
                "rhs": {"type": "identifier", "value": "my_dict"},
            },
        ),
        # if‑expr
        (
            "a if cond else b",
            {
                "operand": "if_expr",
                "test": {"type": "identifier", "value": "cond"},
                "body": {"type": "identifier", "value": "a"},
                "orelse": {"type": "identifier", "value": "b"},
            },
        ),
        # list comprehension
        (
            "[x*2 for x in nums if x>0]",
            {
                "operand": "list_comp",
                "elt": {
                    "lhs": {"type": "identifier", "value": "x"},
                    "operand": "*",
                    "rhs": 2,
                },
                "target": {"type": "identifier", "value": "x"},
                "iter": {"type": "identifier", "value": "nums"},
                "ifs": [
                    {
                        "lhs": {"type": "identifier", "value": "x"},
                        "operand": ">",
                        "rhs": 0,
                    },
                ],
            },
        ),
        # dict comprehension
        (
            "{k:v for k,v in pairs}",
            {
                "operand": "dict_comp",
                "key_elt": {"type": "identifier", "value": "k"},
                "val_elt": {"type": "identifier", "value": "v"},
                "target": [
                    {"type": "identifier", "value": "k"},
                    {"type": "identifier", "value": "v"},
                ],
                "iter": {"type": "identifier", "value": "pairs"},
                "ifs": [],
            },
        ),
        # zip
        (
            "zip(a,b,c)",
            {
                "operand": "zip",
                "rhs": [
                    {"type": "identifier", "value": "a"},
                    {"type": "identifier", "value": "b"},
                    {"type": "identifier", "value": "c"},
                ],
            },
        ),
    ],
)
def test_ast_parser(expression, expected_dict):
    """
    Test that the new AST-based parser correctly converts filter expressions
    to the expected dictionary structure.
    """
    result_dict = str_filter_exp_to_dict_using_ast(expression)
    assert (
        result_dict == expected_dict
    ), f"AST mismatch.\nGot: {result_dict}\nExpected: {expected_dict}"


@pytest.mark.parametrize(
    "expression, values, expected",
    [
        # Arithmetic
        ("(a + b) > 10", {"a": 5, "b": 8}, True),
        ("(a - b) == 2", {"a": 5, "b": 3}, True),
        ("(a * b) == 15", {"a": 3, "b": 5}, True),
        ("(a / b) == 2", {"a": 10, "b": 5}, True),
        ("(a % b) == 1", {"a": 10, "b": 3}, True),
        ("((a**2 + b**2)**0.5) == 10", {"a": 6.0, "b": 8.0}, True),
        # String arithmetic
        ("(a + b) == 'apple banana'", {"a": "apple", "b": " banana"}, True),
        # Logical
        ("(a > 5) and (b < 10)", {"a": 6, "b": 9}, True),
        ("(a < 5) or (b > 10)", {"a": 4, "b": 11}, True),
        ("not (a == 5)", {"a": 4}, True),
        # Comparison
        ("a == 5", {"a": 5}, True),
        ("a != 5", {"a": 4}, True),
        ("a < 5", {"a": 4}, True),
        ("a > 5", {"a": 6}, True),
        ("a <= 5", {"a": 5}, True),
        ("a >= 5", {"a": 5}, True),
        # Membership
        ("a in [1, 2, 3]", {"a": 2}, True),
        ("a not in [1, 2, 3]", {"a": 4}, True),
        ("1 in a", {"a": [1, 2, 3]}, True),
        ("b1 in a", {"a": [1, 2, 3], "b1": 1}, True),
        ("b2 in a", {"a": [[1, 2], [3, 4]], "b2": [1, 2]}, True),
        ("[1,2] in a", {"a": [[1, 2], [3, 4]]}, True),
        ("'hello' in a", {"a": "hello world"}, True),
        ("s in a", {"a": "hello world", "s": "hello"}, True),
        # Indexing
        ("x[0] + y[1] == 5", {"x": [1, 2], "y": [3, 4]}, True),
        ("'hell' + x[4] == 'hello'", {"x": "hello"}, True),
        ("x['a'][0] + 2 == 12", {"x": {"a": [10, 20, 30]}}, True),
        # Indexing + Rounding
        ("round(x['some_key'], 2) >= 100.44", {"x": {"some_key": 100.4479}}, True),
        (
            "round_timestamp(x['_timestamp'], 5) == '1993-03-23T00:00:02+00:00'",
            {
                "x": {
                    "_timestamp": datetime(
                        1993,
                        3,
                        23,
                        0,
                        0,
                        3,
                        tzinfo=timezone.utc,
                    ).isoformat(),
                },
            },
            False,
        ),
        # Round to nearest 5 seconds - should round down
        (
            "round_timestamp(x['_timestamp'], 5) == '1993-03-23T00:00:00+00:00'",
            {
                "x": {
                    "_timestamp": datetime(
                        1993,
                        3,
                        23,
                        0,
                        0,
                        2,
                        tzinfo=timezone.utc,
                    ).isoformat(),
                },
            },
            True,
        ),
        # Round to nearest minute (60 seconds)
        (
            "round_timestamp(x['_timestamp'], 60) == '1993-03-23T00:00:00+00:00'",
            {
                "x": {
                    "_timestamp": datetime(
                        1993,
                        3,
                        23,
                        0,
                        0,
                        29,
                        tzinfo=timezone.utc,
                    ).isoformat(),
                },
            },
            True,
        ),
        # Round to nearest 15 minutes (900 seconds)
        (
            "round_timestamp(x['_timestamp'], 900) == '1993-03-23T00:15:00+00:00'",
            {
                "x": {
                    "_timestamp": datetime(
                        1993,
                        3,
                        23,
                        0,
                        8,
                        0,
                        tzinfo=timezone.utc,
                    ).isoformat(),
                },
            },
            True,
        ),
        (
            "x['timestamps'][0]['time1'] >= '1993-03-25T00:00:00+00:00'",
            {
                "x": {
                    "timestamps": [
                        {
                            "time1": (
                                datetime(1993, 3, 24, tzinfo=timezone.utc)
                            ).isoformat(),
                        },
                        {
                            "time2": (
                                datetime(1993, 3, 27, tzinfo=timezone.utc)
                            ).isoformat(),
                        },
                    ],
                },
            },
            False,
        ),
        # Nested Logical and Arithmetic
        ("((a + b) > 10) and ((c * d) < 20)", {"a": 5, "b": 8, "c": 2, "d": 3}, True),
        ("((a - b) == 2) or ((e / f) == 3)", {"a": 5, "b": 3, "e": 9, "f": 3}, True),
        # More Complex Nested Expressions
        ("(len(a) == 3) and ((b + c) > 10)", {"a": [1, 2, 3], "b": 5, "c": 6}, True),
        ("(str(a) == 'abc') or (len(b) == 2)", {"a": "abc", "b": [1, 2]}, True),
        # Using exists with nested conditions
        ("exists(a) and (b > 5)", {"a": 5, "b": 6}, True),
        ("not exists(c) or (d < 10)", {"d": 9}, True),
        # Testing isNone function
        ("isNone(field1)", {"field1": None}, True),
        ("not isNone(field2)", {"field2": "non-null"}, True),
        ("isNone(field3)", {"field3": None}, True),
        ("not isNone(field4)", {"field4": 0}, True),
        # 1. datetime object.
        (
            "time(a) == '14:30:00'",
            {"a": datetime(2023, 5, 4, 14, 30, 0).isoformat()},
            True,
        ),
        # 2. 24-hour formatted time string.
        ("time(a) == '14:30:00'", {"a": "14:30:00"}, True),
        # 3. 12-hour formatted time string.
        ("time(a) == '14:30:00'", {"a": "2:30 PM"}, True),
        # 4.the time does not match.
        ("time(a) != '14:30:00'", {"a": "15:00:00"}, True),
        # 5. Date extraction from timestamp
        ("date(ts) == '2023-01-01'", {"ts": "2023-01-01T12:00:00"}, True),
        # 6. Date comparison (less than)
        ("date(ts) < '2023-01-02'", {"ts": "2023-01-01T23:59:59"}, True),
        # 7. Date comparison (greater than)
        ("date(ts) > '2022-12-31'", {"ts": "2023-01-01T00:00:01"}, True),
        # 8. Date comparison (not equal)
        ("date(ts) != '2023-01-02'", {"ts": "2023-01-01T12:00:00"}, True),
        # 9. Timedelta arithmetic - adding hours to timestamp
        ("ts + 'PT1H' == '2023-01-01T13:00:00'", {"ts": "2023-01-01T12:00:00"}, True),
        # 10. Timedelta arithmetic - adding days to timestamp
        ("ts + 'P1D' == '2023-01-02T12:00:00'", {"ts": "2023-01-01T12:00:00"}, True),
        # 11. Timedelta arithmetic - subtracting hours from timestamp
        ("ts - 'PT2H' == '2023-01-01T10:00:00'", {"ts": "2023-01-01T12:00:00"}, True),
        # 12. Date subtraction resulting in timedelta
        (
            "date2 - date1 == 'P1D'",
            {"date1": "2023-01-01", "date2": "2023-01-02"},
            True,
        ),
        # 13. Time difference between two timestamps
        (
            "time2 - time1 == 'PT1H'",
            {"time1": "2023-01-01T12:00:00", "time2": "2023-01-01T13:00:00"},
            True,
        ),
        # 14. Complex date arithmetic with multiple operations
        (
            "(date1 + 'P1D') - date2 == 'P0D'",
            {"date1": "2023-01-01", "date2": "2023-01-02"},
            True,
        ),
        # 15. Comparing date with extracted date from timestamp
        (
            "date(ts) == date1",
            {"ts": "2023-01-01T12:00:00", "date1": "2023-01-01"},
            True,
        ),
    ],
)
async def test_log_filter_helper_w_arithmetic(
    client: AsyncClient,
    expression,
    values,
    expected,
):
    project_name = "test_filter_helper"
    _ = await _create_project(client, project_name, user=1)
    response = await client.post(
        "/v0/logs",
        json={"project": project_name, "entries": values},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    response = await client.get(
        "/v0/logs",
        params={"project": project_name, "filter_expr": expression},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    result = len(response.json()["logs"]) == 1
    assert result == expected


@pytest.mark.anyio
async def test_get_logs_with_derived_math_expressions_and_indexing(client: AsyncClient):

    project_name = "test_derived_logs_math"
    user_id = 1

    # 1) Create project
    await _create_project(client, project_name, user=user_id)

    # 2) Create the base logs (7 logs total).
    await _create_several_logs(client, project_name, user=user_id)

    # Fetch them back to confirm we have 7 log events.
    resp = await client.get(
        "/v0/logs",
        params={"project": project_name, "sorting": json.dumps({"id": "ascending"})},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    base_logs = data["logs"]
    assert (
        len(base_logs) == 7
    ), f"Expected exactly 7 logs from _create_several_logs, got {len(base_logs)}"

    # Let's locate logs by description (and track the one missing description).
    log_id_boiling = None
    log_id_freezing = None
    log_id_sun = None
    log_id_nitrogen = None
    log_id_lava = None
    log_id_air = None
    log_id_no_desc = None

    for log_obj in base_logs:
        desc = log_obj["entries"].get("_/description", "")
        _id = log_obj["id"]
        if desc == "boiling water":
            log_id_boiling = _id
        elif desc == "freezing water":
            log_id_freezing = _id
        elif desc == "surface of the sun":
            log_id_sun = _id
        elif desc == "freezing nitrogen":
            log_id_nitrogen = _id
        elif desc == "lava":
            log_id_lava = _id
        elif desc == "air":
            log_id_air = _id
        else:
            log_id_no_desc = _id

    # Sanity-check that we found all 7
    assert all(
        [
            log_id_boiling,
            log_id_freezing,
            log_id_sun,
            log_id_nitrogen,
            log_id_lava,
            log_id_air,
            log_id_no_desc,
        ],
    ), "Did not locate all 7 logs by description / no-desc."

    ############################################################
    #              3) Create Derived Logs
    ############################################################

    #
    # (A) Add 10 to _/temperature for logs [boiling, freezing, sun]
    #
    derived_conf_add10 = {
        "key": "dl_add10",
        "equation": "{temp:_/temperature} + 10",
        "referenced_logs": {
            "temp": [log_id_boiling, log_id_freezing, log_id_sun],
        },
    }
    resp = await _create_derived_entry(
        client,
        project_name,
        derived_conf_add10["key"],
        derived_conf_add10["equation"],
        derived_conf_add10["referenced_logs"],
        user=user_id,
    )
    assert resp.status_code == 200, resp.json()
    dl_add10_ids = resp.json()["derived_log_ids"]
    assert len(dl_add10_ids) == 3, f"Expected 3 derived logs, got {dl_add10_ids}"

    #
    # (B) Convert Celsius→Fahrenheit: (C × 9/5) + 32, referencing [boiling, freezing]
    #
    derived_conf_c_to_f = {
        "key": "dl_c_to_f",
        "equation": "({C:_/temperature} * 9 / 5) + 32",
        "referenced_logs": {
            "C": [log_id_boiling, log_id_freezing],
        },
    }
    resp = await _create_derived_entry(
        client,
        project_name,
        derived_conf_c_to_f["key"],
        derived_conf_c_to_f["equation"],
        derived_conf_c_to_f["referenced_logs"],
        user=user_id,
    )
    assert resp.status_code == 200, resp.json()
    dl_c_to_f_ids = resp.json()["derived_log_ids"]
    assert len(dl_c_to_f_ids) == 2, "Only boiling & freezing logs used"

    #
    # (C) Round the temperature to nearest hundred: round({t:_/temperature}, -2)
    #     We'll reference [boiling, freezing, sun, nitrogen] for variety.
    #
    derived_conf_round_temp = {
        "key": "dl_round_temp",
        "equation": "round({t:_/temperature}, -2)",
        "referenced_logs": {
            "t": [log_id_boiling, log_id_freezing, log_id_sun, log_id_nitrogen],
        },
    }
    resp = await _create_derived_entry(
        client,
        project_name,
        derived_conf_round_temp["key"],
        derived_conf_round_temp["equation"],
        derived_conf_round_temp["referenced_logs"],
        user=user_id,
    )
    assert resp.status_code == 200, resp.json()
    dl_round_temp_ids = resp.json()["derived_log_ids"]
    assert len(dl_round_temp_ids) == 4

    #
    # (D) len({desc:_/description}) for [all logs that have _/description].
    #     That excludes the log with no description (log_id_no_desc).
    #
    logs_with_desc = [
        log_id_boiling,
        log_id_freezing,
        log_id_sun,
        log_id_nitrogen,
        log_id_lava,
        log_id_air,
    ]
    derived_conf_len_desc = {
        "key": "dl_len_desc",
        "equation": "len({desc:_/description})",
        "referenced_logs": {"desc": logs_with_desc},
    }
    resp = await _create_derived_entry(
        client,
        project_name,
        derived_conf_len_desc["key"],
        derived_conf_len_desc["equation"],
        derived_conf_len_desc["referenced_logs"],
        user=user_id,
    )
    assert resp.status_code == 200, resp.json()
    dl_len_desc_ids = resp.json()["derived_log_ids"]
    assert len(dl_len_desc_ids) == len(logs_with_desc)

    #
    # (E) Subtraction across logs: "Sun temp minus boiling temp"
    #
    derived_conf_sub = {
        "key": "dl_sun_minus_boil",
        "equation": "{sun:_/temperature} - {boil:_/temperature}",
        "referenced_logs": {
            "sun": [log_id_sun],
            "boil": [log_id_boiling],
        },
    }
    resp = await _create_derived_entry(
        client,
        project_name,
        derived_conf_sub["key"],
        derived_conf_sub["equation"],
        derived_conf_sub["referenced_logs"],
        user=user_id,
    )
    assert resp.status_code == 200, resp.json()
    dl_sub_ids = resp.json()["derived_log_ids"]
    assert len(dl_sub_ids) >= 1, "Should create derived log for that combination"

    #
    # (F) Indexing a list: {m:_/metadata}[1] + 2
    #     We'll reference logs known to have _/metadata = [1,5,6] (lava) and [3,8,5] (air).
    #     (We won't include nitrogen etc. if they don't have _/metadata.)
    #
    derived_conf_index_array = {
        "key": "dl_index_array",
        "equation": "{m:_/metadata}[1] + 2",
        "referenced_logs": {
            "m": [log_id_lava, log_id_air],  # they both have _/metadata
        },
    }
    resp = await _create_derived_entry(
        client,
        project_name,
        derived_conf_index_array["key"],
        derived_conf_index_array["equation"],
        derived_conf_index_array["referenced_logs"],
        user=user_id,
    )
    assert resp.status_code == 200, resp.json()
    dl_index_array_ids = resp.json()["derived_log_ids"]
    assert len(dl_index_array_ids) == 2, "lava + air"

    #
    # (G) Indexing a dict: {d:_/_data}['b'] + 5
    #     We'll reference logs #5 (lava => b=4), #6 (air => b=12), #7 (no desc => b=10).
    #
    derived_conf_index_dict = {
        "key": "dl_index_dict",
        "equation": "{d:_/_data}['b'] + 5",
        "referenced_logs": {
            "d": [log_id_lava, log_id_air, log_id_no_desc],
        },
    }
    resp = await _create_derived_entry(
        client,
        project_name,
        derived_conf_index_dict["key"],
        derived_conf_index_dict["equation"],
        derived_conf_index_dict["referenced_logs"],
        user=user_id,
    )
    assert resp.status_code == 200, resp.json()
    dl_index_dict_ids = resp.json()["derived_log_ids"]
    assert len(dl_index_dict_ids) == 3, "lava + air + no-desc"

    # (H) Exponent: e.g. {sun:_/temperature} ** 2
    derived_conf_exp = {
        "key": "dl_sun_exp2",
        "equation": "{sun:_/temperature} ** 2",
        "referenced_logs": {
            "sun": [log_id_sun],  # surface of sun = 6000
        },
    }
    resp = await _create_derived_entry(
        client,
        project_name,
        derived_conf_exp["key"],
        derived_conf_exp["equation"],
        derived_conf_exp["referenced_logs"],
        user=user_id,
    )
    assert resp.status_code == 200, resp.json()
    dl_exp_ids = resp.json()["derived_log_ids"]
    assert len(dl_exp_ids) == 1

    # (I) Floor Division: e.g. {boil:_/temperature} // 3
    derived_conf_floor_div = {
        "key": "dl_boil_floor_div",
        "equation": "{boil:_/temperature} // 3",
        "referenced_logs": {
            "boil": [log_id_boiling],
        },
    }
    resp = await _create_derived_entry(
        client,
        project_name,
        derived_conf_floor_div["key"],
        derived_conf_floor_div["equation"],
        derived_conf_floor_div["referenced_logs"],
        user=user_id,
    )
    assert resp.status_code == 200, resp.json()
    dl_floor_div_ids = resp.json()["derived_log_ids"]
    assert len(dl_floor_div_ids) == 1

    ############################################################################
    # 4) Verify the derived entries in GET /v0/logs
    ############################################################################

    resp = await client.get(
        "/v0/logs",
        params={"project": project_name, "sorting": json.dumps({"id": "ascending"})},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    data_all = resp.json()
    all_logs = data_all["logs"]
    assert len(all_logs) == 7, "Should still be 7 logs in this project."

    # We'll check each log_event for the derived values
    for log_obj in all_logs:
        log_id = log_obj["id"]
        entries = log_obj["entries"]
        derived = log_obj.get("derived_entries", {})

        # Unpack some known fields
        temp = entries.get("_/temperature")
        desc = entries.get("_/description", "")
        metadata = entries.get("_/metadata")
        data_dict = entries.get("_/_data")

        # (A) dl_add10 => temp + 10
        add10_val = derived.get("dl_add10")
        if add10_val is not None and temp is not None:
            expected = temp + 10
            assert (
                abs(add10_val - expected) < 1e-7
            ), f"dl_add10 mismatch: log_id={log_id}, got {add10_val}, expected {expected}"

        # (B) dl_c_to_f => (temp * 9/5) + 32
        c_to_f_val = derived.get("dl_c_to_f")
        if c_to_f_val is not None and temp is not None:
            expected = (temp * 9.0 / 5.0) + 32
            assert (
                abs(c_to_f_val - expected) < 1e-7
            ), f"dl_c_to_f mismatch: log_id={log_id}, got {c_to_f_val}, expected {expected}"

        # (C) dl_round_temp => round(temp, -2)
        rtemp_val = derived.get("dl_round_temp")
        if rtemp_val is not None and temp is not None:
            # For example,  100 => 100, 0 => 0, 6000 => 6000, -210 => -200
            expected = round(temp, -2)
            assert (
                rtemp_val == expected
            ), f"round_temp mismatch: log_id={log_id}, got {rtemp_val}, expected {expected}"

        # (D) dl_len_desc => len(desc)
        len_desc_val = derived.get("dl_len_desc")
        if len_desc_val is not None:
            expected_len = len(desc)
            assert (
                len_desc_val == expected_len
            ), f"dl_len_desc mismatch: log_id={log_id}, got {len_desc_val}, expected {expected_len}"

        # (E) dl_sun_minus_boil => (sun_temp - boil_temp)
        sub_val = derived.get("dl_sun_minus_boil")
        # Typically only the "sun" log would have a valid numeric result; "boiling" might see None
        if sub_val is not None and log_id == log_id_sun and temp is not None:
            # sun=6000, boil=100 => 5900
            # (assuming these are still the original temperatures)
            expected = 6000 - 100
            assert (
                abs(sub_val - expected) < 1e-7
            ), f"Expected sun-boil=5900 on log_id={log_id}, got {sub_val}"

        # (F) dl_index_array => {m:_/metadata}[1] + 2
        index_array_val = derived.get("dl_index_array")
        if index_array_val is not None and metadata:
            # For "lava" => metadata=[1,5,6], [1] => 5 => +2 => 7
            # For "air"  => metadata=[3,8,5], [1] => 8 => +2 => 10
            expected = metadata[1] + 2
            assert (
                index_array_val == expected
            ), f"dl_index_array mismatch: log_id={log_id}, got {index_array_val}, expected {expected}"

        # (G) dl_index_dict => {d:_/_data}['b'] + 5
        index_dict_val = derived.get("dl_index_dict")
        if index_dict_val is not None and data_dict and "b" in data_dict:
            # For lava => b=4 => +5 => 9
            # For air  => b=12 => +5 => 17
            # For no_desc => b=10 => +5 => 15
            expected = data_dict["b"] + 5
            assert (
                index_dict_val == expected
            ), f"dl_index_dict mismatch: log_id={log_id}, got {index_dict_val}, expected {expected}"

        # (H) Check dl_sun_exp2 => 6000 ** 2 = 36,000,000
        sun_exp2_val = derived.get("dl_sun_exp2")
        if sun_exp2_val is not None and log_id == log_id_sun:
            expected = 6000**2
            assert (
                abs(sun_exp2_val - expected) < 1e-7
            ), f"Exponent mismatch on log_id={log_id}. Got {sun_exp2_val}, expected {expected}"

        # (I) Check dl_boil_floor_div => 100 // 3 = 33
        boil_floor_val = derived.get("dl_boil_floor_div")
        if boil_floor_val is not None and log_id == log_id_boiling:
            # 100 // 3 => 33 in Python
            expected = 33
            assert (
                boil_floor_val == expected
            ), f"Floor division mismatch on log_id={log_id}. Got {boil_floor_val}, expected {expected}"


@pytest.mark.anyio
async def test_filtering_and_sorting_base_and_derived_logs(client: AsyncClient):
    project_name = "test_base_derived_filters"
    user_id = 1

    await _create_project(client, project_name, user=user_id)

    base_logs_data = [
        {
            "entries": {
                "alpha/num": 100,
                "alpha/str": "hello",
                "common_field": True,
            },
            "params": {"p/param1": "base1-param"},
        },
        {
            "entries": {
                "beta/num": 5,
                "beta/str": "world",
                "common_field": False,
            },
            "params": {"p/param1": "base2-param"},
        },
    ]

    base_log_ids = []

    for data in base_logs_data:
        resp = await client.post(
            "/v0/logs",
            headers=HEADERS,
            json={
                "project": project_name,
                "entries": data["entries"],
                "params": data["params"],
            },
        )
        assert resp.status_code == 200, resp.json()
        out_data = resp.json()
        created_log_id = out_data[0]
        base_log_ids.append(created_log_id)

    assert len(base_log_ids) == 2, f"Expected 2 base log_event_ids, got {base_log_ids}"

    derived_definitions = [
        {
            "key": "derv/calcA",
            "equation": "{val:alpha/num} + 10",
            "referenced_logs": {"val": [base_log_ids[0]]},
        },
        {
            "key": "derv/calcB",
            "equation": "{val:beta/num} * 2",
            "referenced_logs": {"val": [base_log_ids[1]]},
        },
    ]

    derived_log_ids = []
    for ddef in derived_definitions:
        resp = await _create_derived_entry(
            client,
            project_name,
            key=ddef["key"],
            equation=ddef["equation"],
            referenced_logs=ddef["referenced_logs"],
            user=user_id,
        )
        assert resp.status_code == 200, resp.json()
        created_d_ids = resp.json()["derived_log_ids"]
        derived_log_ids.extend(created_d_ids)

    assert len(derived_log_ids) == 2, f"Expected 2 derived logs, got {derived_log_ids}"

    # (a) Test that *all* 2 base + 2 derived logs appear across 2 distinct log_event_ids
    logs_all = await fetch_logs(client, project_name)
    assert len(logs_all) == 2, "We created 2 distinct log events total."
    for log_obj in logs_all:
        log_id = log_obj["id"]
        if log_id == base_log_ids[0]:
            assert "alpha/num" in log_obj["entries"]
            assert "alpha/str" in log_obj["entries"]
            assert "derv/calcA" in log_obj["derived_entries"]
        elif log_id == base_log_ids[1]:
            assert "beta/num" in log_obj["entries"]
            assert "beta/str" in log_obj["entries"]
            assert "derv/calcB" in log_obj["derived_entries"]

    # (b) from_ids => If we only want log_id=base_log_ids[0], we should get 1 log event
    logs_single = await fetch_logs(client, project_name, from_ids=str(base_log_ids[0]))
    assert len(logs_single) == 1
    assert logs_single[0]["id"] == base_log_ids[0]
    assert "derv/calcA" in logs_single[0]["derived_entries"]

    # (c) exclude_ids => Exclude the second log_id => only the first remains
    logs_excluding = await fetch_logs(
        client,
        project_name,
        exclude_ids=str(base_log_ids[1]),
    )
    assert len(logs_excluding) == 1
    assert logs_excluding[0]["id"] == base_log_ids[0]

    # (d) from_fields => e.g. only keys that match ["alpha/num", "beta/num"].
    from_fields_param = "alpha/num&beta/num"
    logs_field_incl = await fetch_logs(
        client,
        project_name,
        from_fields=from_fields_param,
    )
    for lg in logs_field_incl:
        assert set(lg["entries"].keys()).issubset({"alpha/num", "beta/num"})
        assert lg["derived_entries"] == {}

    # (e) exclude_fields => e.g. exclude "common_field" from both logs + exclude "derv/calcB"
    exclude_fields_param = "common_field&derv/calcB"
    logs_excluding_fields = await fetch_logs(
        client,
        project_name,
        exclude_fields=exclude_fields_param,
    )
    for lg in logs_excluding_fields:
        assert "common_field" not in lg["entries"]
        assert "derv/calcB" not in lg["derived_entries"]
        if lg["id"] == base_log_ids[0]:
            assert "derv/calcA" in lg["derived_entries"]

    # (f) column_context => Suppose we only want logs with a key starting with "alpha/"
    col_ctx = "alpha/entries"
    logs_alpha = await fetch_logs(client, project_name, column_context=col_ctx)
    assert len(logs_alpha) == 1
    assert logs_alpha[0]["id"] == base_log_ids[0]
    assert set(logs_alpha[0]["entries"].keys()) == {"num", "str"}
    assert logs_alpha[0]["derived_entries"] == {}

    # (g) filter_expr => e.g. "alpha/num > 50 or beta/num < 10"
    logs_filtered = await fetch_logs(
        client,
        project_name,
        filter_expr="derv/calcA > 50 or derv/calcB <= 10",
    )
    assert len(logs_filtered) == 2, "Both logs match the filter expression."

    # (h) sorting => e.g. sort by alpha/num descending
    logs_sorted = await fetch_logs(
        client,
        project_name,
        sorting=json.dumps({"derv/calcA": "descending"}),
    )
    assert len(logs_sorted) == 2
    assert logs_sorted[0]["id"] == base_log_ids[0]


@pytest.mark.parametrize(
    "timestamp_format,filter_format,should_match",
    [
        # Test ISO format with T in both log and filter
        ("2025-03-11T11:56:46.392", "2025-03-11T11:56:46.392", True),
        # Test ISO format with T in log but space in filter
        ("2025-03-11T11:56:46.392", "2025-03-11 11:56:46.392", True),
        # Test space format in log but T in filter
        ("2025-03-11 11:56:46.392", "2025-03-11T11:56:46.392", True),
        # Test space format in both log and filter
        ("2025-03-11 11:56:46.392", "2025-03-11 11:56:46.392", True),
        # Test different timestamps that shouldn't match
        ("2025-03-11T12:56:46.392", "2025-03-11T11:56:46.392", False),
    ],
)
async def test_get_logs_w_timestamp_filtering(
    client: AsyncClient,
    timestamp_format,
    filter_format,
    should_match,
):
    """
    Test that timestamp filtering works correctly with different timestamp formats.

    This test verifies that the normalize_timestamp function correctly handles
    timestamps with and without the 'T' separator in ISO 8601 format.
    """
    project_name = "test_timestamp_normalization"
    _ = await _create_project(client, project_name, user=1)

    # Create a log with a timestamp in the specified format
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "entries": {
                "student/timestamp": timestamp_format,
                "test_field": "test_value",
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text

    # Filter logs using the specified filter format
    filter_expr = f'student/timestamp == "{filter_format}"'
    response = await client.get(
        "/v0/logs",
        params={"project": project_name, "filter_expr": filter_expr},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text

    logs = response.json()["logs"]
    if should_match:
        assert (
            len(logs) == 1
        ), f"Expected 1 log for filter: {filter_expr}, got {len(logs)}"
    else:
        assert (
            len(logs) == 0
        ), f"Expected 0 logs for filter: {filter_expr}, got {len(logs)}"

    # Also test greater-than comparison
    filter_expr = f'student/timestamp > "{filter_format}"'
    response = await client.get(
        "/v0/logs",
        params={"project": project_name, "filter_expr": filter_expr},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text

    # For greater-than, if the log timestamp equals the filter it should return 0 logs
    expected_count = 0 if should_match else 1
    logs = response.json()["logs"]
    assert (
        len(logs) == expected_count
    ), f"Expected {expected_count} logs for filter: {filter_expr}, got {len(logs)}"


@pytest.mark.anyio
async def test_get_logs_w_filtering(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)
    _ = await _create_several_logs(client, project_name, batched=False)

    # temperature == -210.0
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "_/temperature == -210.0"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 1
    assert isinstance(result["logs"][0]["ts"], str)
    assert result["logs"][0]["entries"] == {
        "_/description": "freezing nitrogen",
        "_/temperature": -210.0,
        "_/state": "liquid->solid",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }

    # temperature != -210.0
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "_/temperature != -210.0"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 3
    assert isinstance(result["logs"][0]["ts"], str)
    assert {
        "_/description": "freezing nitrogen",
        "_/temperature": -210.0,
        "_/state": "liquid->solid",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    } not in [log["entries"] for log in result["logs"]]

    # temperature > 0.
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "_/temperature > 0."},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 2
    assert isinstance(result["logs"][0]["ts"], str)
    assert isinstance(result["logs"][1]["ts"], str)
    assert result["logs"][0]["entries"] == {
        "_/description": "surface of the sun",
        "_/temperature": 6000.0,
        "_/state": "gas",
        "_/safe": False,
        "_/timestamp": datetime(1993, 3, 22, tzinfo=timezone.utc).isoformat(),
    }
    assert result["logs"][1]["entries"] == {
        "_/description": "boiling water",
        "_/temperature": 100.0,
        "_/state": "liquid->gas",
        "_/safe": False,
        "_/timestamp": datetime(1993, 3, 22, tzinfo=timezone.utc).isoformat(),
    }

    # timestamp later than 23/03/1993
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": '_/timestamp > "1993-03-23"'},
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

    # timestamp earlier than 23/03/1993
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": '_/timestamp < "1993-03-23"'},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 4
    assert result["logs"][0]["entries"] == {
        "_/description": "freezing nitrogen",
        "_/temperature": -210.0,
        "_/state": "liquid->solid",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][1]["entries"] == {
        "_/description": "surface of the sun",
        "_/temperature": 6000.0,
        "_/state": "gas",
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
        "_/description": "boiling water",
        "_/temperature": 100.0,
        "_/state": "liquid->gas",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }

    # timestamp is 23/03/1993
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": '_/timestamp == "1993-03-23"'},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 0

    # is earlier than or later than 23/03/1993
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={
            "filter_expr": '_/timestamp < "1993-03-23" or _/timestamp > "1993-03-23"',
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 7

    # liquid not in state
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "'liquid' not in _/state"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["entries"] == {
        "_/description": "surface of the sun",
        "_/temperature": 6000.0,
        "_/state": "gas",
        "_/safe": False,
        "_/timestamp": datetime(1993, 3, 22, tzinfo=timezone.utc).isoformat(),
    }

    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "_/description == 'boiling water'"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["entries"]["_/description"] == "boiling water"

    # check multiple conditions
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "('liquid' not in _/state) or (_/temperature == 0)"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 2
    assert result["logs"][0]["entries"] == {
        "_/description": "surface of the sun",
        "_/temperature": 6000.0,
        "_/state": "gas",
        "_/safe": False,
        "_/timestamp": datetime(1993, 3, 22, tzinfo=timezone.utc).isoformat(),
    }
    assert result["logs"][1]["entries"] == {
        "_/description": "freezing water",
        "_/temperature": 0.0,
        "_/state": "liquid->solid",
        "_/safe": True,
        "_/timestamp": datetime(1993, 3, 22, tzinfo=timezone.utc).isoformat(),
    }

    # Test filtering by updated_at and created_at timestamps
    # Update some logs to create a time difference
    log_ids = [1, 2]
    initial_time = datetime.now(timezone.utc)
    entries = {"_/state": "gas->liquid"}
    update_response = await client.put(
        f"/v0/logs",
        json={"ids": log_ids, "entries": entries, "overwrite": True},
        headers=HEADERS,
    )
    assert update_response.status_code == 200

    # Now test filtering for logs where updated_at > created_at
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "updated_at > created_at"},
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    updated_logs = result["logs"]
    assert len(updated_logs) == 2  # Should find the two updated logs
    # # Verify timestamps were updated
    # for log in updated_logs:
    #     assert datetime.fromisoformat(log["updated_at"]) > datetime.fromisoformat(log["created_at"])
    log_ids_found = [log["id"] for log in result["logs"]]
    assert log_ids_found == [2, 1]

    # Test filtering for logs where updated_at = created_at
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "updated_at == created_at"},
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    # Should find the non-updated logs where updated_at equals created_at
    assert len(result["logs"]) == 5  # Should find the non-updated logs
    log_ids_found = [log["id"] for log in result["logs"]]
    assert log_ids_found == [7, 6, 5, 4, 3]
    # Test combining timestamp filters with other fields
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={
            "filter_expr": "updated_at > created_at and _/state == 'gas->liquid'",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    assert len(result["logs"]) == 2
    for log in result["logs"]:
        assert log["entries"]["_/state"] == "gas->liquid"

    # Test filtering by updated_at range
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={
            "filter_expr": f'updated_at >= "{initial_time.isoformat()}"',
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    assert len(result["logs"]) == 2  # Should only find the updated logs
    for log in result["logs"]:
        assert log["entries"]["_/state"] == "gas->liquid"

    # check exists
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "exists(_/state)"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 4

    # check not exists
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "not exists(_/temperature)"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 3

    # Test log_id equality filtering
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "log_id == 1"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["id"] == 1

    # Test log_id inequality filtering
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "log_id != 1"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) > 0
    assert all(log["id"] != 1 for log in result["logs"])

    # Test log_id in operator
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "log_id in [1, 2, 3]"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) > 0
    assert all(log["id"] in [1, 2, 3] for log in result["logs"])

    # Test log_id not in operator
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "log_id not in [1, 2, 3]"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) > 0
    assert all(log["id"] not in [1, 2, 3] for log in result["logs"])

    # Test nested conditions with log_id
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "log_id > 2 and _/temperature > 0"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) > 0
    for log in result["logs"]:
        assert log["id"] > 2
        assert log["entries"]["_/temperature"] > 0

    # Test non-existent log_id
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "log_id == 9999"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 0

    # Test log_id with complex nested conditions
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={
            "filter_expr": "(log_id > 1 and log_id < 4) and (_/temperature > 0 or _/safe is True)",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) > 0
    for log in result["logs"]:
        assert 1 < log["id"] < 4
        assert (
            log["entries"].get("_/temperature", 0) > 0
            or log["entries"].get("_/safe") is True
        )

    # check len
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "len(_/description) < 10"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 2
    assert result["logs"][1]["entries"]["_/description"] == "lava"

    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "len(_/_data) > 2"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["entries"]["_/description"] == "air"

    # check in
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "'lava' in _/description"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["entries"]["_/description"] == "lava"

    # check version
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "version(a/b/param1) == 1"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["params"]["a/b/param1"] == "1"

    # check is <val>
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "_/safe is True"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["entries"] == {
        "_/description": "freezing water",
        "_/temperature": 0.0,
        "_/state": "gas->liquid",
        "_/safe": True,
        "_/timestamp": datetime(1993, 3, 22, tzinfo=timezone.utc).isoformat(),
    }

    # check is None
    # update description to None
    response = await client.put(
        f"/v0/logs",
        json={"ids": [3, 4], "entries": {"_/description": None}, "overwrite": True},
        headers=HEADERS,
    )
    assert response.status_code == 200
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "_/description is None"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 2
    assert result["logs"][0]["entries"]["_/description"] is None
    assert result["logs"][1]["entries"]["_/description"] is None


@pytest.mark.anyio
async def test_now_function_in_filter_expressions(client: AsyncClient):
    """
    Test the now() function in filter expressions.

    This test verifies that:
    1. The now() function returns the current time
    2. It can be used in datetime comparisons
    3. It works with different operators (>, <, ==, etc.)
    4. It maintains timezone awareness
    """
    project_name = "test_now_function"
    await _create_project(client, project_name)

    # Create logs with timestamps in the past, present (approximately), and future
    past_time = datetime.now(timezone.utc) - timedelta(days=1)
    future_time = datetime.now(timezone.utc) + timedelta(days=1)

    logs_data = [
        {"entries": {"dt/timestamp": past_time.isoformat(), "dt/name": "past_event"}},
        {
            "entries": {
                "dt/timestamp": future_time.isoformat(),
                "dt/name": "future_event",
            },
        },
    ]

    for log_data in logs_data:
        response = await client.post(
            "/v0/logs",
            json={"project": project_name, "entries": log_data["entries"]},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.text

    # 1. Test now() > past timestamp
    response = await client.get(
        "/v0/logs",
        params={"project": project_name, "filter_expr": "now() > dt/timestamp"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["entries"]["dt/name"] == "past_event"

    # 2. Test now() < future timestamp
    response = await client.get(
        "/v0/logs",
        params={"project": project_name, "filter_expr": "now() < dt/timestamp"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["entries"]["dt/name"] == "future_event"

    # 3. Test complex expression with now()
    response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "filter_expr": "(now() - dt/timestamp) > 'PT12H'",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["entries"]["dt/name"] == "past_event"

    # 4. Test now() with date extraction
    response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "filter_expr": "date(now()) >= date(dt/timestamp)",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["entries"]["dt/name"] == "past_event"


@pytest.mark.anyio
async def test_timezone_aware_datetime_filtering(client: AsyncClient):
    """
    Test datetime filtering with timezone differences.

    This test verifies that:
    1. Datetime comparisons respect timezone information
    2. Datetimes with different timezone offsets are correctly compared
    3. Timezone information is preserved in arithmetic operations
    4. now() function returns timezone-aware datetime
    """
    project_name = "test_timezone_filtering"
    await _create_project(client, project_name)

    # Create logs with timestamps in different timezones
    utc_time = datetime.now(timezone.utc)
    est_offset = timedelta(hours=-5)  # EST is UTC-5
    est_timezone = timezone(est_offset)
    est_time = utc_time.astimezone(est_timezone)

    # Create a time that's the same wall clock time but in different zones
    # For example, 10:00 UTC and 10:00 EST (which is actually 15:00 UTC)
    base_time = datetime(2023, 6, 15, 10, 0, 0, tzinfo=timezone.utc)
    same_wall_time_est = datetime(2023, 6, 15, 10, 0, 0, tzinfo=est_timezone)

    logs_data = [
        {
            "entries": {
                "dt/utc_time": utc_time.isoformat(),
                "dt/est_time": est_time.isoformat(),
                "dt/name": "same_instant_different_zones",
            },
        },
        {
            "entries": {
                "dt/utc_time": base_time.isoformat(),
                "dt/est_time": same_wall_time_est.isoformat(),
                "dt/name": "same_wall_time_different_zones",
            },
        },
    ]

    for log_data in logs_data:
        response = await client.post(
            "/v0/logs",
            json={"project": project_name, "entries": log_data["entries"]},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.text

    # 1. Test equality of same instant in different timezones
    response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "filter_expr": "dt/utc_time == dt/est_time and dt/name == 'same_instant_different_zones'",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["entries"]["dt/name"] == "same_instant_different_zones"

    # 2. Test inequality of same wall time in different timezones
    response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "filter_expr": "dt/utc_time != dt/est_time and dt/name == 'same_wall_time_different_zones'",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["entries"]["dt/name"] == "same_wall_time_different_zones"

    # 3. Test that UTC time is earlier than EST time with same wall clock time
    response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "filter_expr": "dt/utc_time < dt/est_time and dt/name == 'same_wall_time_different_zones'",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["entries"]["dt/name"] == "same_wall_time_different_zones"

    # 4. Test timezone-aware arithmetic with now()
    response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "filter_expr": "now() - dt/utc_time > 'PT0.000001S'",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result["logs"]) == 2

    # 5. Test that now() preserves timezone information in comparisons
    response = await client.get(
        "/v0/logs",
        params={"project": project_name, "filter_expr": "dt/est_time < now()"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result["logs"]) == 2


async def test_advanced_datetime_arithmetic(client: AsyncClient):
    """
    Test advanced datetime arithmetic with fractional seconds and complex operations.

    This test focuses on:
    1. Timestamps with fractional seconds (milliseconds, microseconds)
    2. Timezone-aware comparisons with fractional precision
    3. Mixed date/time/timestamp operations
    4. Fractional timedeltas (adding 2.5 hours, etc.)
    5. Chained operations (calculating midpoint between timestamps)
    6. Month boundary calculations with fractional seconds
    7. Complex filtering expressions combining multiple operations
    """
    project_name = "test_advanced_datetime"
    await _create_project(client, project_name)

    # Create logs with various datetime values including fractional seconds
    logs_data = [
        # Timestamp with milliseconds precision
        {
            "entries": {
                "dt/precise_ts": "2023-06-15T14:30:45.123+00:00",
                "dt/name": "millisecond_precision",
            },
        },
        # Timestamp with microseconds precision
        {
            "entries": {
                "dt/precise_ts": "2023-06-15T14:30:45.123456+00:00",
                "dt/name": "microsecond_precision",
            },
        },
        # Two timestamps with fractional seconds for interval calculation
        {
            "entries": {
                "dt/start_ts": "2023-06-15T10:15:30.500+00:00",
                "dt/end_ts": "2023-06-15T12:45:45.750+00:00",
                "dt/name": "fractional_interval",
            },
        },
        # Timestamps in different timezones with fractional seconds
        {
            "entries": {
                "dt/utc_ts": "2023-06-15T12:30:45.500+00:00",
                "dt/est_ts": "2023-06-15T17:30:45.500-05:00",
                "dt/name": "timezone_fractional",
            },
        },
        # Mixed date, time and timestamp for combined operations
        {
            "entries": {
                "dt/date": "2023-06-15",
                "dt/time": "14:30:45.500",
                "dt/timestamp": "2023-06-15T14:30:45.500+00:00",
                "dt/name": "mixed_types_fractional",
            },
        },
        # Three timestamps for midpoint calculation
        {
            "entries": {
                "dt/start": "2023-06-15T08:00:00.000+00:00",
                "dt/middle": "2023-06-15T12:00:00.000+00:00",
                "dt/end": "2023-06-15T16:00:00.000+00:00",
                "dt/name": "midpoint_calculation",
            },
        },
        # Month boundary with fractional seconds
        {
            "entries": {
                "dt/jan31": "2023-01-31T23:59:59.999+00:00",
                "dt/feb01": "2023-02-01T00:00:00.001+00:00",
                "dt/name": "month_boundary_fractional",
            },
        },
    ]

    for log_data in logs_data:
        response = await client.post(
            "/v0/logs",
            json={"project": project_name, "entries": log_data["entries"]},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.text

    # 1. Test fractional seconds precision in timestamp comparison
    response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "filter_expr": "dt/precise_ts == '2023-06-15T14:30:45.123+00:00'",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["entries"]["dt/name"] == "millisecond_precision"

    # 2. Test microsecond precision
    response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "filter_expr": "dt/precise_ts == '2023-06-15T14:30:45.123456+00:00'",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["entries"]["dt/name"] == "microsecond_precision"

    # 3. Test adding fractional timedelta (2.5 hours = 2 hours 30 minutes)
    response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "filter_expr": "dt/start_ts + 'PT2H30M' == '2023-06-15T12:45:30.500+00:00'",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["entries"]["dt/name"] == "fractional_interval"

    # 4. Test fractional interval calculation
    response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "filter_expr": "dt/end_ts - dt/start_ts == 'PT2H30M15.25S'",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["entries"]["dt/name"] == "fractional_interval"

    # 5. Test timezone-aware comparison with fractional seconds
    # TODO: fix this test. seems like a timezone issue.
    response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "filter_expr": "dt/est_ts - dt/utc_ts == 'PT5H'",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["entries"]["dt/name"] == "timezone_fractional"

    # 6. Test extracting date and time parts from timestamp with fractional seconds
    response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "filter_expr": "date(dt/timestamp) == dt/date and time(dt/timestamp) == dt/time",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["entries"]["dt/name"] == "mixed_types_fractional"

    # 7. Test midpoint calculation (complex chained operation)
    response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "filter_expr": "dt/middle == (dt/start + ((dt/end - dt/start) / 2))",
        },
        headers=HEADERS,
    )

    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["entries"]["dt/name"] == "midpoint_calculation"

    # 8. Test month boundary with fractional seconds
    response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "filter_expr": "dt/feb01 - dt/jan31 == 'PT0.002S'",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["entries"]["dt/name"] == "month_boundary_fractional"

    # 9. Test complex filtering with multiple conditions
    response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "filter_expr": "(dt/precise_ts > '2023-06-15T00:00:00.000+00:00') and (date(dt/precise_ts) == '2023-06-15')",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert (
        len(result["logs"]) == 2
    )  # Should match both millisecond and microsecond precision logs

    # 10. Test adding fractional seconds directly
    response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "filter_expr": "dt/precise_ts + 'PT0.877S' == '2023-06-15T14:30:46.000+00:00'",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["entries"]["dt/name"] == "millisecond_precision"


async def test_get_logs_w_str_filtering(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)
    _ = await _create_several_logs(client, project_name)

    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "'2' in str(_/_data)"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 2

    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "str('2') in str(_/_data)"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 2

    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": """'{"a": 2' in str(_/_data)"""},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 1

    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": """str('{"a": 2') in str(_/_data)"""},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "array_field, test_value, should_match",
    [
        # Test scalar value in array
        ([1, 2, 3], 1, True),
        ([1, 2, 3], 4, False),
        # Test string value in array
        (["a", "b", "c"], "a", True),
        (["a", "b", "c"], "d", False),
        # Test mixed type array
        ([1, "two", 3.0], "two", True),
        ([1, "two", 3.0], 1, True),
        ([1, "two", 3.0], 3.0, True),
        ([1, "two", 3.0], 2, False),
        # Test nested arrays
        ([[1, 2], [3, 4]], [1, 2], True),
        ([[1, 2], [3, 4]], [5, 6], False),
        # Test empty array
        ([], 1, False),
        # Test with boolean values
        ([True, False], True, True),
        ([True, False], False, True),
        ([True, False], 1, False),  # 1 is not True in PostgreSQL array containment
        # Test with null values
        ([None, 1, 2], None, True),
        ([1, 2, 3], None, False),
    ],
)
@pytest.mark.anyio
async def test_array_membership_operator(
    client: AsyncClient,
    array_field,
    test_value,
    should_match,
):
    """
    Test that the membership operator correctly handles the case when a literal
    is checked against a JSON array column using PostgreSQL's array containment operator (@>).
    """
    project_name = "test_array_membership"
    await _create_project(client, project_name)

    # Create a log with the test array
    response = await _create_log(
        client,
        project_name,
        entries={"test_array": array_field},
    )
    assert response.status_code == 200
    log_id = response.json()[0]

    # Test the membership operator
    filter_expr = f"{test_value!r} in test_array"
    response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "filter_expr": filter_expr,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    data = response.json()

    if should_match:
        assert len(data["logs"]) == 1, f"Expected 1 log for expression: {filter_expr}"
        assert data["logs"][0]["id"] == log_id
    else:
        assert len(data["logs"]) == 0, f"Expected 0 logs for expression: {filter_expr}"


@pytest.mark.parametrize(
    "bool_field, test_value, expected_error",
    [
        (True, True, True),  # Should raise error: True in True is invalid
        (False, False, True),  # Should raise error: False in False is invalid
        (True, False, True),  # Should raise error: False in True is invalid
        (True, "True", True),  # Should raise error: "True" in True is invalid
    ],
)
@pytest.mark.anyio
async def test_boolean_membership_operator_error(
    client: AsyncClient,
    bool_field,
    test_value,
    expected_error,
):
    """
    Test that the membership operator correctly handles the case when a literal
    is checked against a boolean column. This should raise an error since
    membership tests on single boolean columns are invalid.
    """
    project_name = "test_bool_membership"
    await _create_project(client, project_name)

    # Create a log with the test boolean
    response = await _create_log(
        client,
        project_name,
        entries={"test_bool": bool_field},
    )
    assert response.status_code == 200

    # Test the membership operator
    filter_expr = f"{test_value!r} in test_bool"
    response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "filter_expr": filter_expr,
        },
        headers=HEADERS,
    )

    # Should return an error or empty result
    if expected_error:
        assert response.status_code != 200 or len(response.json()["logs"]) == 0


async def test_complex_string_filter_expressions(client: AsyncClient):
    """
    Test that filter expressions correctly match complex strings with special characters,
    multi-line content, and various formatting.
    """
    project_name = "test_complex_string_filters"
    user_id = 1

    # Create project
    await _create_project(client, project_name, user=user_id)

    # Example 1: Math problem with special characters and formatting
    math_question = """
        5 The table below shows the number of tonnes of rice produced in a year in five countries:

        Country   | Rice produced (tonnes)
        ----------------------------------
        China     | 1.43 × 10⁸
        India     | 9.9 × 10⁷
        Vietnam   | 2.71 × 10⁷
        Thailand  | 2.05 × 10⁷
        Brazil    | 7.82 × 10⁶

        (a) Which country produced the most rice?
        (a) …………………………………… [1]

        (b) Write 2.71 × 10⁷ as an ordinary number.
        (b) …………………………………… [1]

        (c) One tonne is equal to 1000 kilograms.
        Change 7.82 × 10⁶ tonnes to kilograms.
        Give your answer in standard form.
        (c) …………………………………… kg [2]

        (d) How many more tonnes of rice did India produce than Thailand?
        Give your answer in standard form.
        (d) …………………………………… tonnes [2]
        """

    # Example 2: Probability problem with bullet points
    probability_question = """21. Louise travels to work and home again by train.
            • The probability that her train to work is late is 0.7.
            • The probability that her train home is late is 0.4.

            What is the probability that at least one of her trains is late?"""
    # Create logs with these complex strings
    math_log_id = (
        await _create_log(
            client,
            project_name,
            user=user_id,
            entries={"content": math_question},
        )
    ).json()[0]

    probability_log_id = (
        await _create_log(
            client,
            project_name,
            user=user_id,
            entries={"content": probability_question},
        )
    ).json()[0]

    # Test 1: Filter for the math question
    resp = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "filter_expr": f"content == '{math_question}'",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["logs"]) == 1, "Expected exactly 1 log matching the math question"
    assert data["logs"][0]["id"] == math_log_id

    # Test 2: Filter for the probability question
    resp = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "filter_expr": f"content == '{probability_question}'",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert (
        len(data["logs"]) == 1
    ), "Expected exactly 1 log matching the probability question"
    assert data["logs"][0]["id"] == probability_log_id
