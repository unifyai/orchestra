"""add_safe_temporal_cast_functions

Revision ID: add_safe_temporal_cast_functions
Revises: 2b35f76ca925
Create Date: 2025-11-17 20:58:00.000000

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "add_safe_temporal_cast_functions"
down_revision = "2b35f76ca925"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Add custom PostgreSQL functions for safe temporal casting.

    These functions safely cast text values to temporal types (timestamp with time zone,
    time, date, interval) and return NULL for invalid values instead of raising errors.
    This is necessary for PostgreSQL 15.14 which doesn't have pg_input_is_valid().

    The functions handle any invalid format generically, including:
    - String "NULL"
    - Empty strings
    - Invalid date/time/interval formats
    - Any other garbage data
    """
    # Safe cast to timestamp with time zone
    op.execute(
        """
        CREATE OR REPLACE FUNCTION safe_cast_to_timestamptz(input_text TEXT)
        RETURNS TIMESTAMP WITH TIME ZONE AS $$
        BEGIN
            RETURN input_text::TIMESTAMP WITH TIME ZONE;
        EXCEPTION
            WHEN OTHERS THEN
                RETURN NULL;
        END;
        $$ LANGUAGE plpgsql IMMUTABLE;
        """,
    )

    # Safe cast to time
    op.execute(
        """
        CREATE OR REPLACE FUNCTION safe_cast_to_time(input_text TEXT)
        RETURNS TIME AS $$
        BEGIN
            RETURN input_text::TIME;
        EXCEPTION
            WHEN OTHERS THEN
                RETURN NULL;
        END;
        $$ LANGUAGE plpgsql IMMUTABLE;
        """,
    )

    # Safe cast to date
    op.execute(
        """
        CREATE OR REPLACE FUNCTION safe_cast_to_date(input_text TEXT)
        RETURNS DATE AS $$
        BEGIN
            RETURN input_text::DATE;
        EXCEPTION
            WHEN OTHERS THEN
                RETURN NULL;
        END;
        $$ LANGUAGE plpgsql IMMUTABLE;
        """,
    )

    # Safe cast to interval (for timedelta)
    op.execute(
        """
        CREATE OR REPLACE FUNCTION safe_cast_to_interval(input_text TEXT)
        RETURNS INTERVAL AS $$
        BEGIN
            RETURN input_text::INTERVAL;
        EXCEPTION
            WHEN OTHERS THEN
                RETURN NULL;
        END;
        $$ LANGUAGE plpgsql IMMUTABLE;
        """,
    )


def downgrade() -> None:
    """Remove the safe temporal cast functions."""
    op.execute("DROP FUNCTION IF EXISTS safe_cast_to_timestamptz(TEXT);")
    op.execute("DROP FUNCTION IF EXISTS safe_cast_to_time(TEXT);")
    op.execute("DROP FUNCTION IF EXISTS safe_cast_to_date(TEXT);")
    op.execute("DROP FUNCTION IF EXISTS safe_cast_to_interval(TEXT);")
