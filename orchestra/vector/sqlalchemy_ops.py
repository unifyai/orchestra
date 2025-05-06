"""SQLAlchemy wrappers for pgvector distance operators.

These helpers let you write expressive, type-checked queries like::

    stmt = (
        select(LogEmbedding)
        .order_by(cosine_distance(LogEmbedding.embedding, probe))
        .limit(10)
    )

without hand-writing raw SQL operators (<#>, <->, <=>).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import FunctionElement
from sqlalchemy.sql.visitors import Visitable


# ---------------------------------------------------------------------------
# Base helper to ease creation of operator wrappers
# ---------------------------------------------------------------------------
class _VectorOp(FunctionElement):
    inherit_cache: bool = True  # pragma: no cover – SQLAlchemy perf

    def __init__(self, a: Visitable, b: Visitable):  # noqa: WPS110
        super().__init__(a, b)


# ---------------------------------------------------------------------------
# L2 (Euclidean) distance  –   vector1 <-> vector2
# ---------------------------------------------------------------------------
class L2Distance(_VectorOp):
    name = "l2_distance"


@compiles(L2Distance)
def _compile_l2(element: L2Distance, compiler, **kw: Any):  # noqa: WPS110
    a, b = list(element.clauses)
    return f"({compiler.process(a)} <-> {compiler.process(b)})"


def l2(a: Visitable, b: Visitable):  # noqa: WPS110
    """Return an SQLAlchemy expression for L2 distance."""
    return L2Distance(a, b)


# ---------------------------------------------------------------------------
# Cosine distance  –  vector1 <#> vector2
# ---------------------------------------------------------------------------
class CosineDistance(_VectorOp):
    name = "cosine_distance"


@compiles(CosineDistance)
def _compile_cos(element: CosineDistance, compiler, **kw: Any):  # noqa: WPS110
    a, b = list(element.clauses)
    return f"({compiler.process(a)} <#> {compiler.process(b)})"


def cosine(a: Visitable, b: Visitable):  # noqa: WPS110
    """Return an SQLAlchemy expression for cosine distance."""
    return CosineDistance(a, b)


# ---------------------------------------------------------------------------
# Inner-product distance  –  vector1 <=> vector2
# ---------------------------------------------------------------------------
class InnerProductDistance(_VectorOp):
    name = "inner_product_distance"


@compiles(InnerProductDistance)
def _compile_ip(element: InnerProductDistance, compiler, **kw: Any):  # noqa: WPS110
    a, b = list(element.clauses)
    return f"({compiler.process(a)} <=> {compiler.process(b)})"


def ip(a: Visitable, b: Visitable):  # noqa: WPS110
    """Return an SQLAlchemy expression for inner-product distance."""
    return InnerProductDistance(a, b)


# ---------------------------------------------------------------------------
# Fall-back wrappers that assume auxiliary SQL functions exist in the DB.
# ---------------------------------------------------------------------------
class L1Distance(_VectorOp):
    name = "l1"


@compiles(L1Distance)
def _compile_l1(element: L1Distance, compiler, **kw: Any):  # noqa: WPS110
    a, b = list(element.clauses)
    return f"l1({compiler.process(a)}, {compiler.process(b)})"


def l1(a: Visitable, b: Visitable):  # noqa: WPS110
    return L1Distance(a, b)


class HammingDistance(_VectorOp):
    name = "hamming"


@compiles(HammingDistance)
def _compile_hamm(element: HammingDistance, compiler, **kw: Any):  # noqa: WPS110
    a, b = list(element.clauses)
    return f"hamming({compiler.process(a)}, {compiler.process(b)})"


def hamming(a: Visitable, b: Visitable):  # noqa: WPS110
    return HammingDistance(a, b)


class JaccardDistance(_VectorOp):
    name = "jaccard"


@compiles(JaccardDistance)
def _compile_jacc(element: JaccardDistance, compiler, **kw: Any):  # noqa: WPS110
    a, b = list(element.clauses)
    return f"jaccard({compiler.process(a)}, {compiler.process(b)})"


def jaccard(a: Visitable, b: Visitable):  # noqa: WPS110
    return JaccardDistance(a, b)


__all__ = [
    "l2",
    "cosine",
    "ip",
    "l1",
    "hamming",
    "jaccard",
]
