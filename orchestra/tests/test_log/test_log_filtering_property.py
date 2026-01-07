from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import pytest
from hypothesis import HealthCheck, given, note, reject, settings
from hypothesis import strategies as st

from . import HEADERS, _create_log, _create_project

# Define constants for operators
NUMERIC_ARITHMETIC_OPS = ["+", "-", "*", "/", "%", "**", "//"]
COMPARISON_OPS = ["==", "!=", "<", ">", "<=", ">="]
NULL_COMPARISON_OPS = ["is", "is not"]
MEMBERSHIP_OPS = ["in", "not in"]
LOGICAL_OPS = ["and", "or"]
UNARY_OP = "not"

# Define constants for supported functions
SUPPORTED_FUNCTIONS = [
    "len",
    "str",
    "type",
    "round",
    "round_timestamp",
    "num_tokens",
    "exists",
    "version",
    "BASE",
    "isNone",
    "time",
    "date",
    "now",
]

# Define column types for our test data
COLUMN_TYPES = {
    "int_col": "int",
    "flt_col": "float",
    "str_col": "str",
    "bool_col": "bool",
    "ts_col": "datetime",
    "dt_col": "date",
    "tm_col": "time",
    "td_col": "timedelta",
    "list_col": "list",
    "dict_col": "dict",
}

# Dictionary to store all distinct values from sample data for each column
ACTUAL_COLUMN_VALUES = {
    "int_col": set(),
    "flt_col": set(),
    "str_col": set(),
    "bool_col": set(),
    "ts_col": set(),
    "dt_col": set(),
    "tm_col": set(),
    "td_col": set(),
    "list_col": set(),
    "dict_col": set(),
}

# Sample data to insert for testing
SAMPLE_DATA = [
    dict(
        int_col=5,
        flt_col=3.14,
        str_col="hello",
        bool_col=True,
        ts_col="2025-03-22T10:00:00",
        dt_col="2025-03-22",
        tm_col="10:15:00",
        td_col="P2D",
        list_col=[1, 2, 3],
        dict_col={"a": 1, "b": 2},
    ),
    dict(
        int_col=0,
        flt_col=-1.23,
        str_col="",
        bool_col=False,
        ts_col="2025-03-23T23:59:59",
        dt_col="2025-03-23",
        tm_col="23:59:59",
        td_col="PT5H",
        list_col=[4, 5],
        dict_col={"c": 3},
    ),
    dict(
        int_col=-10,
        flt_col=0.0,
        str_col="world",
        bool_col=True,
        ts_col="2025-03-21T12:30:45",
        dt_col="2025-03-21",
        tm_col="12:30:45",
        td_col="PT12H30M",
        list_col=[],
        dict_col={},
    ),
]

# Populate ACTUAL_COLUMN_VALUES with distinct values from SAMPLE_DATA
for row in SAMPLE_DATA:
    for col, val in row.items():
        # For container types, we need to convert to immutable types for set storage
        if col == "list_col":
            ACTUAL_COLUMN_VALUES[col].add(tuple(val))
        elif col == "dict_col":
            # Convert dict to a frozenset of items for hashability
            ACTUAL_COLUMN_VALUES[col].add(frozenset(val.items()))
        else:
            ACTUAL_COLUMN_VALUES[col].add(val)

# Convert sets back to lists for easier sampling
for col in ACTUAL_COLUMN_VALUES:
    ACTUAL_COLUMN_VALUES[col] = list(ACTUAL_COLUMN_VALUES[col])


@pytest.fixture(scope="function")
async def setup_test_data(client):
    project_name = "test_prop_based"
    # Create the project
    _ = await _create_project(client, project_name)
    r = await _create_log(client, project_name, entries=SAMPLE_DATA, params={})
    assert r.status_code == 200
    log_ids = r.json()
    return project_name, log_ids


# ===== ENHANCED STRATEGY DEFINITIONS =====


# Strategy for column references with optional type filtering
@st.composite
def column_ref_strategy(draw, allowed_types=None) -> Tuple[str, str]:
    """
    Generates a column reference with optional type filtering.

    Args:
        allowed_types: Optional list of types to restrict column selection

    Returns:
        Tuple[str, str]: (column_name, column_type)
    """
    if allowed_types:
        # Filter columns by the allowed types
        matching_cols = [
            col for col, col_type in COLUMN_TYPES.items() if col_type in allowed_types
        ]
        if not matching_cols:
            # If no matching columns, fall back to any column
            col_name = draw(st.sampled_from(list(COLUMN_TYPES.keys())))
        else:
            col_name = draw(st.sampled_from(matching_cols))
    else:
        # No type constraint, choose any column
        col_name = draw(st.sampled_from(list(COLUMN_TYPES.keys())))

    col_type = COLUMN_TYPES[col_name]
    return (col_name, col_type)


# Strategy for literal values from actual column data
@st.composite
def literal_from_column_strategy(draw, column_type) -> Tuple[str, str]:
    """
    Generates a literal value from actual column data of the specified type.

    Args:
        column_type: The type of column to get values from

    Returns:
        Tuple[str, str]: (literal_string, type_string)
    """
    # Find columns of the requested type
    matching_cols = [
        col for col, col_type in COLUMN_TYPES.items() if col_type == column_type
    ]

    if not matching_cols:
        # Fallback if no matching columns (shouldn't happen with our schema)
        return draw(literal_strategy(type_hint=column_type))

    # Choose a random column of the requested type
    col_name = draw(st.sampled_from(matching_cols))

    # Get actual values for this column
    values = ACTUAL_COLUMN_VALUES[col_name]
    if not values:
        # Fallback if no values (shouldn't happen with our data)
        return draw(literal_strategy(type_hint=column_type))

    # Choose a random value
    value = draw(st.sampled_from(values))

    # Format the value appropriately based on type
    if column_type == "str":
        # Strings need quotes
        return (f"'{value}'", column_type)
    elif column_type == "bool":
        # Booleans need proper capitalization
        return (str(value), column_type)
    elif column_type == "list":
        # Convert tuple back to list representation
        return (str(list(value)), column_type)
    elif column_type == "dict":
        # Convert frozenset back to dict representation
        dict_value = dict(value)
        return (str(dict_value), column_type)
    elif column_type in ["datetime", "date", "time", "timedelta"]:
        # Temporal types need quotes
        return (f"'{value}'", column_type)
    else:
        # Numeric types don't need special formatting
        return (str(value), column_type)


# Strategy for simple comparison expressions
@st.composite
def simple_comparison_strategy(draw) -> Tuple[str, str]:
    """
    Generates a simple comparison expression like (column op literal).

    Returns:
        Tuple[str, str]: (expression_string, type_string)
    """
    # Get a column reference
    col_name, col_type = draw(column_ref_strategy())

    # Decide if we want to do a None comparison with 'is' or 'is not'
    use_is_operator = (
        draw(st.booleans()) and draw(st.integers(min_value=1, max_value=10)) <= 3
    )

    if use_is_operator:
        # Use 'is' or 'is not' for None comparison
        op = draw(st.sampled_from(NULL_COMPARISON_OPS))
        return (f"({col_name} {op} None)", "bool")
    else:
        # Get a literal of the same type from actual data
        literal_val, _ = draw(literal_from_column_strategy(column_type=col_type))

        # Choose an appropriate comparison operator based on the type
        if col_type in ["int", "float", "datetime", "date", "time", "timedelta"]:
            # These types support all comparison operators
            op = draw(st.sampled_from(COMPARISON_OPS))
        else:
            # Other types only support equality operators
            op = draw(st.sampled_from(["==", "!="]))

        return (f"({col_name} {op} {literal_val})", "bool")


# Strategy for typed literals
@st.composite
def literal_strategy(draw, type_hint: Optional[str] = None) -> Tuple[str, str]:
    """
    Returns (literal_string, type_string).
    Generates literals relevant to test data.

    Args:
        type_hint: Optional type hint to constrain the generated value
    """
    if type_hint:
        t = type_hint
    else:
        t = draw(st.sampled_from(list(COLUMN_TYPES.values())))

    if t == "int":
        # Generate integers similar to test data: -10, 0, 5
        val = draw(st.sampled_from([-15, -10, -5, -1, 0, 1, 3, 5, 10]))
        return (str(val), "int")
    elif t == "float":
        # Generate floats similar to test data: -1.23, 0.0, 3.14
        val = draw(st.sampled_from([-2.5, -1.23, -0.5, 0.0, 0.5, 1.5, 3.14, 4.0]))
        return (f"{val:.2f}", "float")
    elif t == "str":
        # Generate strings similar to test data: "", "hello", "world"
        s = draw(st.sampled_from(["", "hello", "world", "test", "a", "b", "c"]))
        return (f"'{s}'", "str")
    elif t == "bool":
        b = draw(st.booleans())
        return ("True", "bool") if b else ("False", "bool")
    elif t == "date":
        # Use dates from test data
        d = draw(st.sampled_from(["2025-03-21", "2025-03-22", "2025-03-23"]))
        return (f"'{d}'", "date")
    elif t == "datetime":
        # Use timestamps from test data
        dt = draw(
            st.sampled_from(
                ["2025-03-21T12:30:45", "2025-03-22T10:00:00", "2025-03-23T23:59:59"],
            ),
        )
        return (f"'{dt}'", "datetime")
    elif t == "time":
        # Use times from test data
        tm = draw(st.sampled_from(["10:15:00", "12:30:45", "23:59:59"]))
        return (f"'{tm}'", "time")
    elif t == "timedelta":
        # Use timedeltas from test data
        td = draw(st.sampled_from(["P2D", "PT5H", "PT12H30M"]))
        return (f"'{td}'", "timedelta")
    elif t == "list":
        # Generate lists similar to test data
        list_opt = draw(st.sampled_from([[1, 2, 3], [4, 5], []]))
        return (f"{list_opt}", "list")
    elif t == "dict":
        # Generate dicts similar to test data
        dict_opt = draw(st.sampled_from([{"a": 1, "b": 2}, {"c": 3}, {}]))
        return (f"{dict_opt}", "dict")
    else:
        return ("0", "int")


# Strategy for bool function calls
@st.composite
def bool_function_strategy(draw) -> Tuple[str, str]:
    """
    Generates a boolean function call, including exists(), isNone(), and type checks.

    Returns:
        Tuple[str, str]: (expression_string, type_string)
    """
    function_kind = draw(st.sampled_from(["exists", "isNone"]))

    if function_kind == "exists":
        # exists() checks if a column exists and has a value
        col_name, _ = draw(column_ref_strategy())
        return (f"exists({col_name})", "bool")

    elif function_kind == "isNone":
        # isNone() checks if a column is None
        col_name, _ = draw(column_ref_strategy())
        return (f"isNone({col_name})", "bool")


# Strategy for arithmetic expressions
@st.composite
def arithmetic_strategy(draw, type_hint: Optional[str] = None) -> Tuple[str, str]:
    """
    Generates more complex arithmetic expressions with better operator coverage.
    Enhanced to work well with indexing expressions.

    Args:
        type_hint: Optional return type constraint

    Returns:
        Tuple[str, str]: (expression_string, type_string)
    """
    if not type_hint:
        type_hint = draw(st.sampled_from(["int", "float"]))

    if type_hint not in ["int", "float"]:
        return draw(terminal_strategy(type_hint=type_hint))

    # Complex expressions with varied operators
    complexity = draw(st.sampled_from(["simple", "medium", "complex"]))

    if complexity == "simple":
        # Simple binary operation
        if type_hint == "int":
            # Use higher weight for indexing in integer arithmetic
            left_source = draw(
                st.sampled_from(
                    [
                        "column",
                        "column",
                        "literal",
                        "indexing",
                        "indexing",
                        "nested_indexing",
                    ],
                ),
            )

            if left_source == "column":
                left_expr, _ = draw(column_ref_strategy(allowed_types=["int"]))
            elif left_source == "literal":
                left_expr, _ = draw(literal_strategy(type_hint="int"))
            elif left_source == "indexing":
                left_expr, _ = draw(indexing_strategy())
            else:  # nested_indexing
                left_expr, _ = draw(nested_indexing_strategy())

            right_source = draw(
                st.sampled_from(["column", "literal", "indexing", "nested_indexing"]),
            )

            if right_source == "column":
                right_expr, _ = draw(column_ref_strategy(allowed_types=["int"]))
            elif right_source == "literal":
                right_expr, _ = draw(literal_strategy(type_hint="int"))
            elif right_source == "indexing":
                right_expr, _ = draw(indexing_strategy())
            else:  # nested_indexing
                right_expr, _ = draw(nested_indexing_strategy())

            op = draw(st.sampled_from(["+", "-", "*", "%", "//", "**"]))

            return (f"({left_expr} {op} {right_expr})", "int")

        else:  # float
            left_type = draw(st.sampled_from(["int", "float"]))

            left_source = draw(
                st.sampled_from(["column", "literal", "indexing", "nested_indexing"]),
            )

            if left_source == "column":
                left_expr, _ = draw(column_ref_strategy(allowed_types=[left_type]))
            elif left_source == "literal":
                left_expr, _ = draw(literal_strategy(type_hint=left_type))
            elif left_source == "indexing":
                left_expr, _ = draw(indexing_strategy())
            else:  # nested_indexing
                left_expr, _ = draw(nested_indexing_strategy())

            right_type = draw(st.sampled_from(["int", "float"]))

            right_source = draw(
                st.sampled_from(["column", "literal", "indexing", "nested_indexing"]),
            )

            if right_source == "column":
                right_expr, _ = draw(column_ref_strategy(allowed_types=[right_type]))
            elif right_source == "literal":
                right_expr, _ = draw(literal_strategy(type_hint=right_type))
            elif right_source == "indexing":
                right_expr, _ = draw(indexing_strategy())
            else:  # nested_indexing
                right_expr, _ = draw(nested_indexing_strategy())

            op = draw(st.sampled_from(["+", "-", "*", "/", "%", "**"]))

            return (f"({left_expr} {op} {right_expr})", "float")

    elif complexity == "medium":
        # Three terms with parentheses
        if type_hint == "int":
            terms = []
            for _ in range(3):
                term_source = draw(
                    st.sampled_from(
                        ["column", "literal", "indexing", "nested_indexing"],
                    ),
                )

                if term_source == "column":
                    term, _ = draw(column_ref_strategy(allowed_types=["int"]))
                elif term_source == "literal":
                    term, _ = draw(literal_strategy(type_hint="int"))
                elif term_source == "indexing":
                    term, _ = draw(indexing_strategy())
                else:  # nested_indexing
                    term, _ = draw(nested_indexing_strategy())

                terms.append(term)

            ops = [draw(st.sampled_from(["+", "-", "*", "%", "//"])) for _ in range(2)]

            # Different combinations of parentheses
            paren_style = draw(st.sampled_from(["left", "right", "none"]))

            if paren_style == "left":
                return (
                    f"(({terms[0]} {ops[0]} {terms[1]}) {ops[1]} {terms[2]})",
                    "int",
                )
            elif paren_style == "right":
                return (
                    f"({terms[0]} {ops[0]} ({terms[1]} {ops[1]} {terms[2]}))",
                    "int",
                )
            else:
                return (f"({terms[0]} {ops[0]} {terms[1]} {ops[1]} {terms[2]})", "int")

        else:  # float
            terms = []
            types = [draw(st.sampled_from(["int", "float"])) for _ in range(3)]

            for i in range(3):
                term_source = draw(
                    st.sampled_from(
                        ["column", "literal", "indexing", "nested_indexing"],
                    ),
                )

                if term_source == "column":
                    term, _ = draw(column_ref_strategy(allowed_types=[types[i]]))
                elif term_source == "literal":
                    term, _ = draw(literal_strategy(type_hint=types[i]))
                elif term_source == "indexing":
                    term, _ = draw(indexing_strategy())
                else:  # nested_indexing
                    term, _ = draw(nested_indexing_strategy())

                terms.append(term)

            ops = [draw(st.sampled_from(["+", "-", "*", "/"])) for _ in range(2)]

            paren_style = draw(st.sampled_from(["left", "right", "none"]))

            if paren_style == "left":
                return (
                    f"(({terms[0]} {ops[0]} {terms[1]}) {ops[1]} {terms[2]})",
                    "float",
                )
            elif paren_style == "right":
                return (
                    f"({terms[0]} {ops[0]} ({terms[1]} {ops[1]} {terms[2]}))",
                    "float",
                )
            else:
                return (
                    f"({terms[0]} {ops[0]} {terms[1]} {ops[1]} {terms[2]})",
                    "float",
                )

    else:  # complex
        # More terms, nested expressions, mixed operators
        if type_hint == "int":
            depth = draw(st.integers(min_value=2, max_value=3))
            expr = draw(build_nested_arithmetic(depth, "int"))
            return (expr, "int")
        else:
            depth = draw(st.integers(min_value=2, max_value=3))
            expr = draw(build_nested_arithmetic(depth, "float"))
            return (expr, "float")


# Update the helper for building nested arithmetic expressions to include indexing
@st.composite
def build_nested_arithmetic(draw, depth: int, type_hint: str) -> str:
    """Helper to recursively build nested arithmetic expressions with indexing support."""
    if depth <= 0:
        if type_hint == "int":
            term_source = draw(
                st.sampled_from(["column", "literal", "indexing", "nested_indexing"]),
            )

            if term_source == "column":
                term, _ = draw(column_ref_strategy(allowed_types=["int"]))
            elif term_source == "literal":
                term, _ = draw(literal_strategy(type_hint="int"))
            elif term_source == "indexing":
                term, _ = draw(indexing_strategy())
            else:  # nested_indexing
                term, _ = draw(nested_indexing_strategy())

            return term
        else:  # float
            term_type = draw(st.sampled_from(["int", "float"]))
            term_source = draw(
                st.sampled_from(["column", "literal", "indexing", "nested_indexing"]),
            )

            if term_source == "column":
                term, _ = draw(column_ref_strategy(allowed_types=[term_type]))
            elif term_source == "literal":
                term, _ = draw(literal_strategy(type_hint=term_type))
            elif term_source == "indexing":
                term, _ = draw(indexing_strategy())
            else:  # nested_indexing
                term, _ = draw(nested_indexing_strategy())

            return term

    # Choose between a simple term and a recursive expression
    if draw(st.booleans()):
        if type_hint == "int":
            term_source = draw(
                st.sampled_from(["column", "literal", "indexing", "nested_indexing"]),
            )

            if term_source == "column":
                return draw(column_ref_strategy(allowed_types=["int"]))[0]
            elif term_source == "literal":
                return draw(literal_strategy(type_hint="int"))[0]
            elif term_source == "indexing":
                return draw(indexing_strategy())[0]
            else:  # nested_indexing
                return draw(nested_indexing_strategy())[0]
        else:
            term_type = draw(st.sampled_from(["int", "float"]))
            term_source = draw(
                st.sampled_from(["column", "literal", "indexing", "nested_indexing"]),
            )

            if term_source == "column":
                return draw(column_ref_strategy(allowed_types=[term_type]))[0]
            elif term_source == "literal":
                return draw(literal_strategy(type_hint=term_type))[0]
            elif term_source == "indexing":
                return draw(indexing_strategy())[0]
            else:  # nested_indexing
                return draw(nested_indexing_strategy())[0]

    # Build a nested expression
    left = draw(build_nested_arithmetic(depth - 1, type_hint))

    if type_hint == "int":
        op = draw(st.sampled_from(["+", "-", "*", "%", "//", "**"]))
        right_source = draw(
            st.sampled_from(["column", "literal", "indexing", "nested_indexing"]),
        )

        if right_source == "column":
            right_term, _ = draw(column_ref_strategy(allowed_types=["int"]))
        elif right_source == "literal":
            right_term, _ = draw(literal_strategy(type_hint="int"))
        elif right_source == "indexing":
            right_term, _ = draw(indexing_strategy())
        else:  # nested_indexing
            right_term, _ = draw(nested_indexing_strategy())
    else:  # float
        op = draw(st.sampled_from(["+", "-", "*", "/", "**"]))
        right_type = draw(st.sampled_from(["int", "float"]))
        right_source = draw(
            st.sampled_from(["column", "literal", "indexing", "nested_indexing"]),
        )

        if right_source == "column":
            right_term, _ = draw(column_ref_strategy(allowed_types=[right_type]))
        elif right_source == "literal":
            right_term, _ = draw(literal_strategy(type_hint=right_type))
        elif right_source == "indexing":
            right_term, _ = draw(indexing_strategy())
        else:  # nested_indexing
            right_term, _ = draw(nested_indexing_strategy())

    # Add parentheses for clarity and correct precedence
    return f"({left} {op} {right_term})"


# Add a complex indexing expression strategy for boolean comparisons
@st.composite
def complex_indexing_comparison_strategy(draw) -> Tuple[str, str]:
    """
    Generates complex boolean expressions involving combinations of indexing and arithmetic.
    For example: (x[0] + y['a'] > z[1]['b'] - 5)

    Returns:
        Tuple[str, str]: (expression_string, type_string)
    """
    # Generate left side (can be arithmetic + indexing)
    left_is_complex = draw(st.booleans())

    if left_is_complex:
        left_expr, _ = draw(arithmetic_strategy(type_hint="int"))
    else:
        left_source = draw(st.sampled_from(["indexing", "nested_indexing"]))
        if left_source == "indexing":
            left_expr, _ = draw(indexing_strategy())
        else:
            left_expr, _ = draw(nested_indexing_strategy())

    # Generate right side (can be arithmetic + indexing)
    right_is_complex = draw(st.booleans())

    if right_is_complex:
        right_expr, _ = draw(arithmetic_strategy(type_hint="int"))
    else:
        right_source = draw(st.sampled_from(["indexing", "nested_indexing", "literal"]))
        if right_source == "indexing":
            right_expr, _ = draw(indexing_strategy())
        elif right_source == "nested_indexing":
            right_expr, _ = draw(nested_indexing_strategy())
        else:
            right_expr, _ = draw(literal_strategy(type_hint="int"))

    # Choose comparison operator
    op = draw(st.sampled_from(COMPARISON_OPS))

    return (f"({left_expr} {op} {right_expr})", "bool")


# Helper for building nested arithmetic expressions
@st.composite
def build_nested_arithmetic(draw, depth: int, type_hint: str) -> str:
    """Helper to recursively build nested arithmetic expressions."""
    if depth <= 0:
        if type_hint == "int":
            term, _ = draw(
                st.one_of(
                    column_ref_strategy(allowed_types=["int"]),
                    literal_strategy(type_hint="int"),
                    container_operation_strategy(),
                ),
            )
        else:  # float
            term_type = draw(st.sampled_from(["int", "float"]))
            term, _ = draw(
                st.one_of(
                    column_ref_strategy(allowed_types=[term_type]),
                    literal_strategy(type_hint=term_type),
                ),
            )
        return term

    # Choose between a simple term and a recursive expression
    if draw(st.booleans()):
        if type_hint == "int":
            return draw(
                st.one_of(
                    column_ref_strategy(allowed_types=["int"]),
                    literal_strategy(type_hint="int"),
                    container_operation_strategy(),
                ),
            )[0]
        else:
            term_type = draw(st.sampled_from(["int", "float"]))
            return draw(
                st.one_of(
                    column_ref_strategy(allowed_types=[term_type]),
                    literal_strategy(type_hint=term_type),
                ),
            )[0]

    # Build a nested expression
    left = draw(build_nested_arithmetic(depth - 1, type_hint))

    if type_hint == "int":
        op = draw(st.sampled_from(["+", "-", "*", "%", "//", "**"]))
        right_term, _ = draw(
            st.one_of(
                column_ref_strategy(allowed_types=["int"]),
                literal_strategy(type_hint="int"),
                container_operation_strategy(),
            ),
        )
    else:  # float
        op = draw(st.sampled_from(["+", "-", "*", "/", "**"]))
        right_type = draw(st.sampled_from(["int", "float"]))
        right_term, _ = draw(
            st.one_of(
                column_ref_strategy(allowed_types=[right_type]),
                literal_strategy(type_hint=right_type),
            ),
        )

    # Add parentheses for clarity and correct precedence
    return f"({left} {op} {right_term})"


# Strategy for list/dict indexing expressions (improved)
@st.composite
def nested_indexing_strategy(draw, max_depth=2) -> Tuple[str, str]:
    """
    Generates nested indexing expressions like x[0][1], dict['a']['b'], etc.

    Args:
        max_depth: Maximum nesting depth for indexing

    Returns:
        Tuple[str, str]: (expression_string, type_string)
    """
    # Start with a container
    container_type = draw(st.sampled_from(["list", "dict"]))
    col_name, _ = draw(column_ref_strategy(allowed_types=[container_type]))

    current_expr = col_name
    current_type = container_type

    # Generate a chain of indexing operations
    depth = draw(st.integers(min_value=1, max_value=max_depth))

    for i in range(depth):
        if current_type == "list":
            # For lists, generate an integer index
            idx = draw(st.sampled_from([0, 1, 2, -1]))

            # For the last level, we're accessing an int element
            if i == depth - 1:
                current_expr = f"{current_expr}[{idx}]"
                current_type = "int"  # Assuming lists contain ints or other lists
            else:
                # For intermediate levels, we might be accessing a nested list
                # or we might be accessing a dictionary inside a list
                next_type = draw(st.sampled_from(["list", "dict", "int"]))

                # If we hit a terminal type, end the chain
                if next_type == "int":
                    current_expr = f"{current_expr}[{idx}]"
                    current_type = "int"
                    break

                current_expr = f"{current_expr}[{idx}]"
                current_type = next_type

        elif current_type == "dict":
            # For dicts, generate a string key
            keys = ["a", "b", "c", "key1", "key2", "nested"]
            key = draw(st.sampled_from(keys))

            # For the last level, we're accessing an int value
            if i == depth - 1:
                current_expr = f"{current_expr}['{key}']"
                current_type = "int"  # Assuming dicts contain ints or other containers
            else:
                # For intermediate levels, it could be a nested dict or a list in a dict
                next_type = draw(st.sampled_from(["list", "dict", "int"]))

                # If we hit a terminal type, end the chain
                if next_type == "int":
                    current_expr = f"{current_expr}['{key}']"
                    current_type = "int"
                    break

                current_expr = f"{current_expr}['{key}']"
                current_type = next_type

        else:
            # We've reached a non-container type, so stop indexing
            break

    return (current_expr, current_type)


# Modify the existing indexing_strategy to include nested indexing
@st.composite
def indexing_strategy(draw) -> Tuple[str, str]:
    """
    Generates more diverse indexing expressions for lists and dictionaries.
    Includes support for nested indexing.
    """
    # Decide between simple and nested indexing
    use_nested = draw(st.booleans())

    if use_nested:
        return draw(nested_indexing_strategy())

    # Original simple indexing logic
    container_type = draw(st.sampled_from(["list", "dict"]))

    if container_type == "list":
        col_name, _ = draw(column_ref_strategy(allowed_types=["list"]))
        idx = draw(st.sampled_from([0, 1, 2, -1]))  # Include negative indices
        expr = f"{col_name}[{idx}]"
        return (expr, "int")  # Assuming lists contain ints in test data
    else:  # dict indexing
        col_name, _ = draw(column_ref_strategy(allowed_types=["dict"]))
        # Use keys likely to exist in test data
        key = draw(st.sampled_from(["a", "b", "c", "key1", "key2"]))
        expr = f"{col_name}['{key}']"
        return (expr, "int")  # Assuming dicts contain ints


@st.composite
def temporal_expression_strategy(
    draw,
    type_hint: Optional[str] = None,
) -> Tuple[str, str]:
    """
    Generates expressions involving temporal data types (date, time, timestamp, timedelta).

    Args:
        type_hint: Optional return type constraint

    Returns:
        Tuple[str, str]: (expression_string, type_string)
    """
    if not type_hint:
        type_hint = draw(st.sampled_from(["datetime", "date", "time", "timedelta"]))

    if type_hint == "datetime":
        # Generate timestamp expressions
        expr_type = draw(st.sampled_from(["column", "literal", "function"]))

        if expr_type == "column":
            col_name, _ = draw(column_ref_strategy(allowed_types=["datetime"]))
            return (col_name, "datetime")

        elif expr_type == "literal":
            ts = draw(
                st.sampled_from(
                    [
                        "2025-03-21T12:30:45",
                        "2025-03-22T10:00:00",
                        "2025-03-23T23:59:59",
                    ],
                ),
            )
            return (f"'{ts}'", "datetime")

        else:  # function
            func = draw(st.sampled_from(["now", "round_timestamp"]))

            if func == "now":
                return ("now()", "datetime")
            else:
                col_name, _ = draw(column_ref_strategy(allowed_types=["datetime"]))
                precision = draw(st.integers(min_value=1, max_value=60))
                return (f"round_timestamp({col_name}, {precision})", "datetime")

    elif type_hint == "date":
        # Generate date expressions
        expr_type = draw(st.sampled_from(["column", "literal", "function"]))

        if expr_type == "column":
            col_name, _ = draw(column_ref_strategy(allowed_types=["date"]))
            return (col_name, "date")

        elif expr_type == "literal":
            dt = draw(st.sampled_from(["2025-03-21", "2025-03-22", "2025-03-23"]))
            return (f"'{dt}'", "date")

        else:  # function
            col_name, _ = draw(column_ref_strategy(allowed_types=["datetime"]))
            return (f"date({col_name})", "date")

    elif type_hint == "time":
        # Generate time expressions
        expr_type = draw(st.sampled_from(["column", "literal", "function"]))

        if expr_type == "column":
            col_name, _ = draw(column_ref_strategy(allowed_types=["time"]))
            return (col_name, "time")

        elif expr_type == "literal":
            tm = draw(st.sampled_from(["10:15:00", "12:30:45", "23:59:59"]))
            return (f"'{tm}'", "time")

        else:  # function
            col_name, _ = draw(column_ref_strategy(allowed_types=["datetime"]))
            return (f"time({col_name})", "time")

    else:  # timedelta
        # Generate timedelta expressions
        expr_type = draw(st.sampled_from(["column", "literal"]))

        if expr_type == "column":
            col_name, _ = draw(column_ref_strategy(allowed_types=["timedelta"]))
            return (col_name, "timedelta")

        else:  # literal
            td = draw(st.sampled_from(["P2D", "PT5H", "PT12H30M"]))
            return (f"'{td}'", "timedelta")


# Enhanced strategy for container operations (lists and dicts)
@st.composite
def container_operation_strategy(draw) -> Tuple[str, str]:
    """
    Generates expressions involving container operations (list/dict).
    """
    container_type = draw(st.sampled_from(["list", "dict"]))
    operation = draw(st.sampled_from(["access", "len", "membership"]))

    if container_type == "list":
        col_name, _ = draw(column_ref_strategy(allowed_types=["list"]))

        if operation == "access":
            # Generate indexing expression
            return draw(indexing_strategy())

        elif operation == "len":
            # Generate length expression
            return (f"len({col_name})", "int")

        else:  # membership
            # Generate membership test
            val, _ = draw(
                st.one_of(
                    column_ref_strategy(allowed_types=["int"]),
                    literal_strategy(type_hint="int"),
                ),
            )
            op = draw(st.sampled_from(MEMBERSHIP_OPS))
            return (f"({val} {op} {col_name})", "bool")

    else:  # dict
        col_name, _ = draw(column_ref_strategy(allowed_types=["dict"]))

        if operation == "access":
            # Generate dict access expression
            key = draw(st.sampled_from(["a", "b", "c"]))
            return (f"{col_name}['{key}']", "int")

        elif operation == "len":
            # Generate length expression
            return (f"len({col_name})", "int")

        else:  # key existence
            # Test if a key exists in the dict
            key = draw(st.sampled_from(["a", "b", "c"]))
            in_expr = draw(st.booleans())
            if in_expr:
                return (f"('{key}' in {col_name})", "bool")
            else:
                return (f"('{key}' in {col_name}.keys())", "bool")


# Enhanced strategy for terminal expressions
@st.composite
def terminal_strategy(draw, type_hint: Optional[str] = None) -> Tuple[str, str]:
    """
    Generates a terminal expression with improved coverage of all supported operators.

    Args:
        type_hint: Optional type constraint

    Returns:
        Tuple[str, str]: (expression_string, type_string)
    """
    if type_hint is None:
        type_hint = draw(st.sampled_from(list(COLUMN_TYPES.values())))

    # Create a weighted list by repeating elements
    weighted_choices = ["column"] * 60 + ["literal"] * 30 + ["function"] * 10

    # Choose terminal kind with weighted distribution
    terminal_kind = draw(st.sampled_from(weighted_choices))

    if terminal_kind == "column":
        matching_cols = [
            col for col, col_type in COLUMN_TYPES.items() if col_type == type_hint
        ]

        if not matching_cols:
            return draw(literal_strategy(type_hint=type_hint))

        col_name = draw(st.sampled_from(matching_cols))
        return (col_name, type_hint)

    elif terminal_kind == "literal":
        return draw(literal_strategy(type_hint=type_hint))

    else:  # function
        # Generate function calls with broader coverage
        if type_hint == "bool":
            func = draw(st.sampled_from(["exists", "isNone"]))
            col_name, _ = draw(column_ref_strategy())
            return (f"{func}({col_name})", "bool")

        elif type_hint == "int":
            func = draw(st.sampled_from(["len", "round", "version", "BASE"]))

            if func == "len":
                container_type = draw(st.sampled_from(["str", "list", "dict"]))
                col_name, _ = draw(column_ref_strategy(allowed_types=[container_type]))
                return (f"len({col_name})", "int")

            elif func == "round":
                col_name, _ = draw(column_ref_strategy(allowed_types=["float"]))
                use_digits = draw(st.booleans())
                if use_digits:
                    digits = draw(st.integers(min_value=0, max_value=3))
                    return (f"round({col_name}, {digits})", "int")
                else:
                    return (f"round({col_name})", "int")

            elif func == "version":
                col_name, _ = draw(column_ref_strategy(allowed_types=["str"]))
                return (f"version({col_name})", "int")

            else:  # BASE
                col_name, _ = draw(column_ref_strategy(allowed_types=["int"]))
                return (f"BASE({col_name})", "int")

        elif type_hint == "str":
            col_name, _ = draw(column_ref_strategy())
            return (f"str({col_name})", "str")

        elif type_hint == "datetime":
            func = draw(st.sampled_from(["now", "round_timestamp"]))

            if func == "now":
                return ("now()", "datetime")
            else:
                col_name, _ = draw(column_ref_strategy(allowed_types=["datetime"]))
                precision = draw(st.integers(min_value=1, max_value=60))
                return (f"round_timestamp({col_name}, {precision})", "datetime")

        elif type_hint == "date":
            col_name, _ = draw(column_ref_strategy(allowed_types=["datetime"]))
            return (f"date({col_name})", "date")

        elif type_hint == "time":
            col_name, _ = draw(column_ref_strategy(allowed_types=["datetime"]))
            return (f"time({col_name})", "time")

        else:
            return draw(
                st.one_of(
                    column_ref_strategy(allowed_types=[type_hint]),
                    literal_strategy(type_hint=type_hint),
                ),
            )


# Strategy for standalone truthiness expressions (no comparison)
@st.composite
def truthiness_strategy(draw) -> Tuple[str, str]:
    """
    Generates expressions that test Python truthiness of a value.

    These are expressions like `dict_col.get('key')` used as boolean conditions,
    testing whether a value exists and is truthy.

    Returns:
        Tuple[str, str]: (expression_string, type_string)
    """
    expr_kind = draw(st.sampled_from(["dict_get", "column_direct", "negated"]))

    if expr_kind == "dict_get":
        # dict_col.get('key') - checks if key exists and value is truthy
        col_name, _ = draw(column_ref_strategy(allowed_types=["dict"]))
        key = draw(st.sampled_from(["a", "b", "c", "missing_key"]))
        return (f"{col_name}.get('{key}')", "bool")

    elif expr_kind == "column_direct":
        # Direct column as truthiness (e.g., bool_col, int_col, str_col)
        # Any type can be used for truthiness
        col_name, col_type = draw(column_ref_strategy())
        return (col_name, "bool")

    else:  # negated
        # not dict_col.get('key')
        col_name, _ = draw(column_ref_strategy(allowed_types=["dict"]))
        key = draw(st.sampled_from(["a", "b", "c"]))
        return (f"(not {col_name}.get('{key}'))", "bool")


# Strategy for membership with (expr or []) fallback pattern
@st.composite
def membership_or_fallback_strategy(draw) -> Tuple[str, str]:
    """
    Generates membership tests with the (expr or []) fallback pattern.

    Pattern: value in (list_expr or [])

    This tests safe iteration over potentially null/missing arrays.

    Returns:
        Tuple[str, str]: (expression_string, type_string)
    """
    # Left side: value to search for
    left_kind = draw(st.sampled_from(["literal", "column"]))

    if left_kind == "literal":
        # Use integer literals that might be in test data
        val = draw(st.sampled_from([1, 2, 3, 4, 5]))
        left_expr = str(val)
    else:
        # Use an integer column
        left_expr, _ = draw(column_ref_strategy(allowed_types=["int"]))

    # Right side: (list_col or []) or (dict_col.get('key') or [])
    right_kind = draw(st.sampled_from(["list_column", "dict_get"]))

    if right_kind == "list_column":
        list_col, _ = draw(column_ref_strategy(allowed_types=["list"]))
        right_expr = f"({list_col} or [])"
    else:
        dict_col, _ = draw(column_ref_strategy(allowed_types=["dict"]))
        key = draw(st.sampled_from(["a", "b", "c", "arr"]))
        right_expr = f"({dict_col}.get('{key}') or [])"

    op = draw(st.sampled_from(MEMBERSHIP_OPS))
    return (f"({left_expr} {op} {right_expr})", "bool")


# Enhanced strategy for membership tests
@st.composite
def membership_strategy(draw) -> Tuple[str, str]:
    """
    Generates a membership test expression (a in [b, c, d]).
    """
    # Get a column reference for the left side
    left_col_name, left_col_type = draw(column_ref_strategy())

    # Decide whether to test membership in a column or a literal list
    test_target = draw(st.sampled_from(["column", "literal_list"]))

    if test_target == "column":
        # Test membership in a list column
        list_col_name, _ = draw(column_ref_strategy(allowed_types=["list"]))
        op = draw(st.sampled_from(MEMBERSHIP_OPS))
        return (f"({left_col_name} {op} {list_col_name})", "bool")

    else:  # literal_list
        # Create a list of values to test membership in
        num_items = draw(st.integers(min_value=1, max_value=3))
        items = []

        for _ in range(num_items):
            # Generate items of the same type as the column
            item_val, _ = draw(literal_from_column_strategy(column_type=left_col_type))
            items.append(item_val)

        list_expr = "[" + ", ".join(items) + "]"
        op = draw(st.sampled_from(MEMBERSHIP_OPS))

        return (f"({left_col_name} {op} {list_expr})", "bool")


# Strategy for value function calls
@st.composite
def value_function_strategy(draw, type_hint: Optional[str] = None) -> Tuple[str, str]:
    """
    Generates a non-boolean function call.

    Args:
        type_hint: Optional return type constraint

    Returns:
        Tuple[str, str]: (expression_string, type_string)
    """
    if type_hint is None:
        # Choose a return type that's supported by our functions
        type_hint = draw(
            st.sampled_from(["int", "float", "str", "datetime", "date", "time"]),
        )

    if type_hint == "int":
        # Integer-returning functions
        func_name = draw(
            st.sampled_from(["len", "round", "version", "BASE", "num_tokens"]),
        )

        if func_name == "len":
            # len works on strings, lists, dicts
            container_type = draw(st.sampled_from(["str", "list", "dict"]))
            col_name, _ = draw(column_ref_strategy(allowed_types=[container_type]))
            return (f"len({col_name})", "int")

        elif func_name == "round":
            # round convert float to int
            col_name, _ = draw(column_ref_strategy(allowed_types=["float"]))
            # Optionally add digits parameter
            use_digits = draw(st.booleans())
            if use_digits:
                digits = draw(st.integers(min_value=0, max_value=3))
                return (f"round({col_name}, {digits})", "int")
            else:
                return (f"round({col_name})", "int")

        elif func_name == "version":
            # version takes a string
            col_name, _ = draw(column_ref_strategy(allowed_types=["str"]))
            return (f"version({col_name})", "int")

        elif func_name == "num_tokens":
            # num_tokens works on any column type
            col_name, _ = draw(column_ref_strategy())
            return (f"num_tokens({col_name})", "int")
        else:  # BASE
            # BASE works on int columns
            col_name, _ = draw(column_ref_strategy(allowed_types=["int"]))
            return (f"BASE({col_name})", "int")

    elif type_hint == "str":
        # String-returning functions: str
        # str works on any type
        col_name, _ = draw(column_ref_strategy())
        return (f"str({col_name})", "str")

    elif type_hint == "datetime":
        # Timestamp-returning functions: now, round_timestamp
        func_name = draw(st.sampled_from(["now", "round_timestamp"]))

        if func_name == "now":
            return ("now()", "datetime")
        else:
            # round_timestamp takes a timestamp and a precision
            col_name, _ = draw(column_ref_strategy(allowed_types=["datetime"]))
            precision = draw(st.integers(min_value=1, max_value=60))
            return (f"round_timestamp({col_name}, {precision})", "datetime")

    elif type_hint == "date":
        # Date-returning functions: date
        # date extracts date from timestamp
        col_name, _ = draw(column_ref_strategy(allowed_types=["datetime"]))
        return (f"date({col_name})", "date")

    elif type_hint == "time":
        # Time-returning functions: time
        # time extracts time from timestamp
        col_name, _ = draw(column_ref_strategy(allowed_types=["datetime"]))
        return (f"time({col_name})", "time")

    else:
        # No functions available for other return types
        # Fall back to terminal
        return draw(terminal_strategy(type_hint=type_hint))


@st.composite
def bool_filter_strategy(draw, max_depth: int = 3) -> Tuple[str, str]:
    """
    Generates more diverse boolean filter expressions with better coverage of operators.
    Enhanced to include complex indexing expressions.

    Args:
        max_depth: Maximum recursion depth for nested expressions

    Returns:
        Tuple[str, str]: (expression_string, type_string)
    """
    # Base case
    if max_depth <= 0 or draw(st.integers(min_value=1, max_value=10)) <= 2:
        expr_kind = draw(
            st.sampled_from(
                [
                    "comparison",  # Simple comparison (column op literal)
                    "membership",  # Membership test (a in b)
                    "function",  # Boolean function (exists, isNone, etc.)
                    "value_comparison",  # Compare results of functions or complex expressions
                    "null_comparison",  # Explicit NULL comparison (is None, is not None)
                    "complex_indexing",  # Complex indexing with arithmetic
                    "truthiness",  # Standalone truthiness (dict.get('key') as bool)
                    "membership_or_fallback",  # Safe membership (x in (arr or []))
                ],
            ),
        )

        if expr_kind == "comparison":
            return draw(simple_comparison_strategy())

        elif expr_kind == "membership":
            return draw(membership_strategy())

        elif expr_kind == "function":
            return draw(bool_function_strategy())

        elif expr_kind == "null_comparison":
            # Explicit NULL comparison
            col_name, _ = draw(column_ref_strategy())
            op = draw(st.sampled_from(NULL_COMPARISON_OPS))
            return (f"({col_name} {op} None)", "bool")

        elif expr_kind == "complex_indexing":
            # Use the complex indexing comparison strategy
            return draw(complex_indexing_comparison_strategy())

        elif expr_kind == "truthiness":
            # Standalone truthiness expression (e.g., dict_col.get('key'))
            return draw(truthiness_strategy())

        elif expr_kind == "membership_or_fallback":
            # Membership with (expr or []) fallback
            return draw(membership_or_fallback_strategy())

        else:  # value_comparison
            # Compare complex values
            value_type = draw(
                st.sampled_from(["int", "float", "str", "datetime", "date", "time"]),
            )

            # Generate left and right expressions with appropriate types
            left_source = draw(
                st.sampled_from(
                    [
                        "function",
                        "indexing",
                        "nested_indexing",
                        "arithmetic",
                        "column",
                        "temporal",
                    ],
                ),
            )

            if left_source == "function" and value_type in [
                "int",
                "float",
                "str",
                "datetime",
                "date",
                "time",
            ]:
                left_expr, _ = draw(value_function_strategy(type_hint=value_type))
            elif left_source == "indexing" and value_type == "int":
                left_expr, _ = draw(indexing_strategy())
            elif left_source == "nested_indexing" and value_type == "int":
                left_expr, _ = draw(nested_indexing_strategy())
            elif left_source == "arithmetic" and value_type in ["int", "float"]:
                left_expr, _ = draw(arithmetic_strategy(type_hint=value_type))
            elif left_source == "temporal" and value_type in [
                "datetime",
                "date",
                "time",
                "timedelta",
            ]:
                left_expr, _ = draw(temporal_expression_strategy(type_hint=value_type))
            else:  # column
                left_col, _ = draw(column_ref_strategy(allowed_types=[value_type]))
                left_expr = left_col

            right_source = draw(
                st.sampled_from(
                    [
                        "function",
                        "indexing",
                        "nested_indexing",
                        "arithmetic",
                        "column",
                        "literal",
                        "temporal",
                    ],
                ),
            )

            if right_source == "function" and value_type in [
                "int",
                "float",
                "str",
                "datetime",
                "date",
                "time",
            ]:
                right_expr, _ = draw(value_function_strategy(type_hint=value_type))
            elif right_source == "indexing" and value_type == "int":
                right_expr, _ = draw(indexing_strategy())
            elif right_source == "nested_indexing" and value_type == "int":
                right_expr, _ = draw(nested_indexing_strategy())
            elif right_source == "arithmetic" and value_type in ["int", "float"]:
                right_expr, _ = draw(arithmetic_strategy(type_hint=value_type))
            elif right_source == "temporal" and value_type in [
                "datetime",
                "date",
                "time",
                "timedelta",
            ]:
                right_expr, _ = draw(temporal_expression_strategy(type_hint=value_type))
            elif right_source == "column":
                right_col, _ = draw(column_ref_strategy(allowed_types=[value_type]))
                right_expr = right_col
            else:  # literal
                right_val, _ = draw(
                    literal_from_column_strategy(column_type=value_type),
                )
                right_expr = right_val

            # Choose an appropriate comparison operator
            if value_type in ["int", "float", "datetime", "date", "time", "timedelta"]:
                op = draw(st.sampled_from(COMPARISON_OPS))
            else:
                op = draw(st.sampled_from(["==", "!="]))

            return (f"({left_expr} {op} {right_expr})", "bool")

    # Non-base case (build complex expressions)
    expr_kind = draw(
        st.sampled_from(
            [
                "logical",  # Combine with logical operators
                "negation",  # Negate an expression
            ],
        ),
    )

    if expr_kind == "logical":
        left_expr, _ = draw(bool_filter_strategy(max_depth=max_depth - 1))
        right_expr, _ = draw(bool_filter_strategy(max_depth=max_depth - 1))
        op = draw(st.sampled_from(LOGICAL_OPS))

        return (f"({left_expr} {op} {right_expr})", "bool")

    else:  # negation
        expr, _ = draw(bool_filter_strategy(max_depth=max_depth - 1))
        return (f"(not {expr})", "bool")


def evaluate_in_python(expr_str: str, row: Dict[str, Any]) -> bool:
    """
    Evaluates a filter expression in Python against a row of data.
    """
    # Handle 'is' and 'is not' operators before column replacement
    code = expr_str.replace(" is not None", " != None").replace(" is None", " == None")

    # Replace column names with row["col_name"]
    for col in COLUMN_TYPES.keys():
        code = code.replace(col, f'row["{col}"]')

    local_env = {
        "row": row,
        "isNone": lambda x: (x is None),
        "len": len,
        "str": str,
        "type": lambda x: type(x).__name__,  # Add type function
        "round": round,
        "date": lambda x: x,  # Pass-through for date values
        "time": lambda x: x,  # Pass-through for time values
        "now": lambda: datetime.now(timezone.utc).isoformat(),
        "exists": lambda x: x is not None,
        "version": lambda x, y=None: 1,
        "BASE": lambda x: x,  # Simplified BASE function for testing
        "round_timestamp": lambda ts, precision: (
            datetime.fromisoformat(ts)
            .replace(
                microsecond=0,
                second=0,
                minute=(datetime.fromisoformat(ts).minute // precision) * precision,
            )
            .isoformat()
            if isinstance(ts, str)
            else ts
        ),
        "None": None,
    }
    try:
        val = eval(code, {"__builtins__": {}}, local_env)
        return bool(val)
    except Exception as e:
        print(f"Error evaluating expression: {code}\nError: {e}")
        return False


@given(expr_and_type=bool_filter_strategy(max_depth=2))
@pytest.mark.skip(reason="Enable when needed")
@settings(
    max_examples=500,
    deadline=None,
    suppress_health_check=[
        HealthCheck.function_scoped_fixture,
    ],
)
async def test_property_based_filtering(client, setup_test_data, expr_and_type):
    """
    Property-based test for the text2SQL filter expression logic.

    For each generated expression:
    1. Evaluate it in Python against the sample data
    2. Send it to the server and get the filtered logs
    3. Compare the results

    This tests that the server's SQL query generation (via build_sql_query)
    returns the same results as our Python reference implementation.
    """
    (expr_str, expr_type) = expr_and_type
    print(f"Testing expression: {expr_str}")

    # Ensure we're only testing boolean expressions for filtering
    if expr_type != "bool":
        reject(f"Expression must be boolean for filtering, got {expr_type}: {expr_str}")
    project_name, log_ids = setup_test_data

    resp = await client.get(
        "/v0/logs",
        params={"project_name": project_name, "filter_expr": expr_str},
        headers=HEADERS,
    )

    if resp.status_code != 200:
        note(f"Server rejected expression '{expr_str}' => {resp.text}")


if __name__ == "__main__":
    pytest.main()
