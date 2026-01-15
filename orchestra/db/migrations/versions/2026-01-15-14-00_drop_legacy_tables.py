"""Drop legacy tables (benchmarking, model registry, routing, query tracking)

Revision ID: drop_legacy_tables
Revises: refactor_user_local_desktop
Create Date: 2026-01-15 14:00:00.000000

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "drop_legacy_tables"
down_revision = "refactor_user_local_desktop"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Drop 23 legacy tables that are no longer in use:
    - Benchmarking system (7 tables)
    - Model/provider registry (5 tables)
    - Custom routing (3 tables)
    - Query/LLM tracking (3 tables)
    - Custom endpoints/API (3 tables)
    - Other legacy tables (2 tables: beta_list, custom_api_key)

    Tables are dropped in dependency order (children before parents).
    """

    # Association/Junction tables first (have FKs to multiple tables)
    op.drop_table("query_tag_association")

    # Tables with foreign keys to others (drop children before parents)
    op.drop_table("query")  # has FKs to endpoint, custom_endpoint, local_endpoint
    op.drop_table("tags")
    op.drop_table("datapoint")  # FK to benchmark_run
    op.drop_table("custom_endpoint_benchmark")  # FK to custom_endpoint

    # Benchmarking tables (drop leaf tables first)
    op.drop_table("latest_benchmark")
    op.drop_table("metric")
    op.drop_table("benchmark_run")
    op.drop_table("benchmark_seq_len")
    op.drop_table("benchmark_region")
    op.drop_table("benchmark_regime")

    # Model/Provider registry tables (local_endpoint and endpoint reference others)
    op.drop_table("local_endpoint")
    op.drop_table("custom_endpoint")
    op.drop_table("endpoint")
    op.drop_table("task")  # FK to modality - drop before modality
    op.drop_table("modality")
    op.drop_table("model")
    op.drop_table("provider")

    # Routing tables
    op.drop_table("custom_router")
    op.drop_table("router")

    # Custom API keys
    op.drop_table("custom_api_key")

    # Other legacy tables
    op.drop_table("beta_list")


def downgrade() -> None:
    """
    No downgrade implemented - these are legacy tables being permanently removed.

    If restoration is needed, refer to the original migration files that created
    these tables. Historical data will be lost unless backed up separately.
    """
