"""Tests for vector distance functions inside equation strings.

These tests ensure that calls like ``l2(embed('a'), embed('b'))`` are correctly
translated to Postgres pgvector distance operators when the equation string is
parsed via ``str_filter_exp_to_dict`` / ``build_sql_query``.

We monkey-patch :pyfunc:`orchestra.vector.utils.embed` to avoid network calls
and keep the compiled SQL deterministic.
"""

import re

import pytest
from sqlalchemy.dialects import postgresql

from orchestra.web.api.log.python2SQL.core import build_sql_query
from orchestra.web.api.log.python2SQL.parsers import str_filter_exp_to_dict

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

PG_DIALECT = postgresql.dialect()


def _compile(expr):
    """Compile a SQLAlchemy expression with literals in-lined."""
    return str(expr.compile(dialect=PG_DIALECT, compile_kwargs={"literal_binds": True}))


@pytest.fixture(autouse=True)
def _patch_embed(monkeypatch):
    """Replace the real embed() with a deterministic stub during tests."""

    monkeypatch.setattr(
        "orchestra.vector.utils.embed",
        lambda text, model="text-embedding-3-large": [0.1, 0.2, 0.3],
    )


# ---------------------------------------------------------------------------
# Parametrised tests for each distance helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "func_name, sql_snippet",
    [
        ("l2", "<->"),
        ("cosine", "<#>"),
        ("ip", "<=>"),
        ("l1", "l1("),
        ("hamming", "hamming("),
        ("jaccard", "jaccard("),
    ],
)
def test_vector_distance_in_equation(func_name: str, sql_snippet: str):
    """Distance functions should compile to the expected SQL operator/call."""

    expr_str = f"{func_name}(embed('hello'), embed('world'))"
    filter_dict = str_filter_exp_to_dict(expr_str)
    expr = build_sql_query(filter_dict, None, None, None)
    sql = _compile(expr)

    # Expect the pgvector cast and correct operator/function name
    assert "AS VECTOR" in sql, "Embedding literals should be cast to pgvector"
    assert sql_snippet in sql, f"Expected '{sql_snippet}' in SQL: {sql}"


# ---------------------------------------------------------------------------
# Explicit test just for embed usage within an equation string
# ---------------------------------------------------------------------------


def test_embed_only_in_equation():
    """A bare embed('text') inside an equation should compile to a vector literal."""

    expr_str = "embed('hello')"
    filter_dict = str_filter_exp_to_dict(expr_str)
    expr = build_sql_query(filter_dict, None, None, None)
    sql = _compile(expr)

    assert "AS VECTOR" in sql
    # Should contain our stubbed values
    assert re.search(r"\[0\.1[0-9\.]*,\s*0\.2[0-9\.]*,\s*0\.3[0-9\.]*\]", sql)
