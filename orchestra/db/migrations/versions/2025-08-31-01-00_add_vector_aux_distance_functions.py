"""Add auxiliary vector distance functions (L1, Hamming, Jaccard).

Revision ID: add_vector_aux_distance_functions
Revises: 46c2450d64eb
Create Date: 2025-08-31 01:00:00
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "add_vector_aux_distance_functions"
down_revision = "46c2450d64eb"
branch_labels = None
depends_on = None


L1_SQL = """
CREATE OR REPLACE FUNCTION l1_distance(a vector, b vector)
RETURNS double precision
LANGUAGE sql IMMUTABLE STRICT AS $$
  SELECT sum(abs(x - y))::double precision
  FROM   unnest(a) WITH ORDINALITY AS ua(x, idx)
  JOIN   unnest(b) WITH ORDINALITY AS ub(y, idx) USING (idx);
$$;
"""


HAMMING_SQL = """
CREATE OR REPLACE FUNCTION hamming_distance(a vector, b vector)
RETURNS integer
LANGUAGE sql IMMUTABLE STRICT AS $$
  SELECT count(*)::int
  FROM   unnest(a) WITH ORDINALITY AS ua(x, idx)
  JOIN   unnest(b) WITH ORDINALITY AS ub(y, idx) USING (idx)
  WHERE  x <> y;
$$;
"""


JACCARD_SQL = """
CREATE OR REPLACE FUNCTION jaccard_distance(a vector, b vector)
RETURNS double precision
LANGUAGE sql IMMUTABLE STRICT AS $$
  WITH sa AS (
     SELECT array_agg(idx) AS idxs
     FROM unnest(a) WITH ORDINALITY AS t(val, idx)
     WHERE val <> 0
  ),
  sb AS (
     SELECT array_agg(idx) AS idxs
     FROM unnest(b) WITH ORDINALITY AS t(val, idx)
     WHERE val <> 0
  ),
  inter AS (
     SELECT cardinality(
       ARRAY(SELECT unnest(sa.idxs) INTERSECT SELECT unnest(sb.idxs))
     ) AS i_cnt FROM sa, sb
  ),
  uni AS (
     SELECT cardinality(
       ARRAY(SELECT unnest(sa.idxs) UNION SELECT unnest(sb.idxs))
     ) AS u_cnt FROM sa, sb
  )
  SELECT CASE WHEN u_cnt = 0 THEN 0::double precision
              ELSE 1 - (i_cnt::double precision / u_cnt::double precision)
         END
  FROM inter, uni;
$$;
"""


def upgrade() -> None:  # noqa: D401
    """Create custom vector distance functions."""

    op.execute("CREATE EXTENSION IF NOT EXISTS pgvector")

    op.execute(L1_SQL)
    op.execute(HAMMING_SQL)
    op.execute(JACCARD_SQL)


def downgrade() -> None:  # noqa: D401
    """Drop custom vector distance functions."""
    op.execute("DROP FUNCTION IF EXISTS jaccard_distance(vector, vector);")
    op.execute("DROP FUNCTION IF EXISTS hamming_distance(vector, vector);")
    op.execute("DROP FUNCTION IF EXISTS l1_distance(vector, vector);")
