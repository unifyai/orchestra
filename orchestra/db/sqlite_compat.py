"""
Compatibility helpers for running the PostgreSQL models on SQLite in tests.

Whenever SQLAlchemy is using the *sqlite* dialect we teach it to emit a
plain "JSON" column instead of "JSONB".  SQLite stores this as TEXT and is
perfectly happy with it; the shim is a complete no-op on Postgres.
"""
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles


@compiles(JSONB, "sqlite")  # activated only for the SQLite dialect
def _compile_jsonb_sqlite(_type, compiler, **kw):  # noqa: D401
    return "JSON"
