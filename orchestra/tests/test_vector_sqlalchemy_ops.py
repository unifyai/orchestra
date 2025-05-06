import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import column

from orchestra.vector.sqlalchemy_ops import cosine, hamming, ip, jaccard, l1, l2

PG_DIALECT = postgresql.dialect()


def _compile(expr):
    """Compile SQLAlchemy expression to string using Postgres dialect."""
    return str(expr.compile(dialect=PG_DIALECT, compile_kwargs={"literal_binds": True}))


@pytest.mark.parametrize(
    "func, sql_snippet",
    [
        (l2, "<->"),
        (cosine, "<#>"),
        (ip, "<=>"),
        (l1, "l1("),
        (hamming, "hamming("),
        (jaccard, "jaccard("),
    ],
)
def test_vector_distance_compilation(func, sql_snippet):
    col_a = column("vec_a")
    col_b = column("vec_b")
    sql = _compile(func(col_a, col_b))
    assert sql_snippet in sql, f"Expected '{sql_snippet}' in SQL: {sql}"
