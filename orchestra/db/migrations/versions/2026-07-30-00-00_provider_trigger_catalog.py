"""Provider trigger catalog staging and certification tables.

Revision ID: provider_trigger_catalog
Revises: org_call_multiparty
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "provider_trigger_catalog"
down_revision = "org_call_multiparty"
branch_labels = None
depends_on = None

JSON_EMPTY_OBJECT = sa.text("'{}'::jsonb")


def upgrade() -> None:
    op.create_table(
        "provider_trigger_catalog_bootstrap_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("environment", sa.String(), nullable=False),
        sa.Column("backend_id", sa.String(), nullable=False),
        sa.Column("desired_hash", sa.String(length=128), nullable=False),
        sa.Column(
            "last_status",
            sa.String(length=32),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "candidates_imported",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "last_import_diagnostics_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSON_EMPTY_OBJECT,
        ),
        sa.Column("last_imported_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "environment",
            "backend_id",
            name="uq_provider_trigger_catalog_bootstrap_env_backend",
        ),
    )
    op.create_index(
        "ix_pt_catalog_bootstrap_environment",
        "provider_trigger_catalog_bootstrap_state",
        ["environment"],
    )
    op.create_index(
        "ix_pt_catalog_bootstrap_backend_id",
        "provider_trigger_catalog_bootstrap_state",
        ["backend_id"],
    )
    op.create_index(
        "ix_pt_catalog_bootstrap_last_status",
        "provider_trigger_catalog_bootstrap_state",
        ["last_status"],
    )

    op.create_table(
        "provider_trigger_catalog_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("backend_id", sa.String(), nullable=False),
        sa.Column("environment", sa.String(), nullable=False),
        sa.Column("catalog_version", sa.String(), nullable=False),
        sa.Column("content_hash", sa.String(length=128), nullable=False),
        sa.Column(
            "raw_entry_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "imported_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "environment",
            "backend_id",
            "content_hash",
            name="uq_provider_trigger_catalog_snapshots_env_backend_hash",
        ),
    )
    op.create_index(
        "ix_pt_catalog_snapshots_backend_id",
        "provider_trigger_catalog_snapshots",
        ["backend_id"],
    )
    op.create_index(
        "ix_pt_catalog_snapshots_environment",
        "provider_trigger_catalog_snapshots",
        ["environment"],
    )
    op.create_index(
        "ix_pt_catalog_snapshots_env_backend_imported",
        "provider_trigger_catalog_snapshots",
        ["environment", "backend_id", "imported_at"],
    )

    op.create_table(
        "provider_trigger_catalog_candidates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "snapshot_id",
            sa.Integer(),
            sa.ForeignKey(
                "provider_trigger_catalog_snapshots.id",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column("backend_id", sa.String(), nullable=False),
        sa.Column("provider_trigger_slug", sa.String(), nullable=False),
        sa.Column("provider_version", sa.String(), nullable=True),
        sa.Column("canonical_app_hint", sa.String(), nullable=True),
        sa.Column(
            "candidate_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSON_EMPTY_OBJECT,
        ),
        sa.Column("unit_hash", sa.String(length=128), nullable=False),
        sa.UniqueConstraint(
            "snapshot_id",
            "provider_trigger_slug",
            name="uq_provider_trigger_catalog_candidates_snapshot_slug",
        ),
    )
    op.create_index(
        "ix_pt_catalog_candidates_snapshot_id",
        "provider_trigger_catalog_candidates",
        ["snapshot_id"],
    )
    op.create_index(
        "ix_pt_catalog_candidates_backend_id",
        "provider_trigger_catalog_candidates",
        ["backend_id"],
    )
    op.create_index(
        "ix_pt_catalog_candidates_provider_trigger_slug",
        "provider_trigger_catalog_candidates",
        ["provider_trigger_slug"],
    )

    op.create_table(
        "provider_trigger_certifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("backend_id", sa.String(), nullable=False),
        sa.Column("provider_trigger_slug", sa.String(), nullable=False),
        sa.Column("candidate_unit_hash", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("blocked_reason", sa.String(length=64), nullable=True),
        sa.Column(
            "checks_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSON_EMPTY_OBJECT,
        ),
        sa.Column("certified_event_slug", sa.String(), nullable=True),
        sa.Column("certified_schema_version", sa.String(), nullable=True),
        sa.Column(
            "source_snapshot_id",
            sa.Integer(),
            sa.ForeignKey(
                "provider_trigger_catalog_snapshots.id",
                ondelete="SET NULL",
            ),
            nullable=True,
        ),
        sa.Column(
            "certification_history_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSON_EMPTY_OBJECT,
        ),
        sa.Column("certified_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "backend_id",
            "provider_trigger_slug",
            name="uq_provider_trigger_certifications_backend_slug",
        ),
    )
    op.create_index(
        "ix_pt_certifications_backend_id",
        "provider_trigger_certifications",
        ["backend_id"],
    )
    op.create_index(
        "ix_pt_certifications_provider_trigger_slug",
        "provider_trigger_certifications",
        ["provider_trigger_slug"],
    )
    op.create_index(
        "ix_pt_certifications_status",
        "provider_trigger_certifications",
        ["status"],
    )
    op.create_index(
        "ix_pt_certifications_source_snapshot_id",
        "provider_trigger_certifications",
        ["source_snapshot_id"],
    )

    op.create_table(
        "provider_trigger_promoted_registry_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_slug", sa.String(), nullable=False),
        sa.Column("schema_version", sa.String(), nullable=False),
        sa.Column(
            "event_definition_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "source_certification_id",
            sa.Integer(),
            sa.ForeignKey(
                "provider_trigger_certifications.id",
                ondelete="SET NULL",
            ),
            nullable=True,
        ),
        sa.Column(
            "promoted_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "event_slug",
            "schema_version",
            name="uq_provider_trigger_promoted_registry_event_version",
        ),
    )
    op.create_index(
        "ix_pt_promoted_registry_source_cert_id",
        "provider_trigger_promoted_registry_entries",
        ["source_certification_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_pt_promoted_registry_source_cert_id",
        table_name="provider_trigger_promoted_registry_entries",
    )
    op.drop_table("provider_trigger_promoted_registry_entries")
    op.drop_index(
        "ix_pt_certifications_source_snapshot_id",
        table_name="provider_trigger_certifications",
    )
    op.drop_index(
        "ix_pt_certifications_status",
        table_name="provider_trigger_certifications",
    )
    op.drop_index(
        "ix_pt_certifications_provider_trigger_slug",
        table_name="provider_trigger_certifications",
    )
    op.drop_index(
        "ix_pt_certifications_backend_id",
        table_name="provider_trigger_certifications",
    )
    op.drop_table("provider_trigger_certifications")
    op.drop_index(
        "ix_pt_catalog_candidates_provider_trigger_slug",
        table_name="provider_trigger_catalog_candidates",
    )
    op.drop_index(
        "ix_pt_catalog_candidates_backend_id",
        table_name="provider_trigger_catalog_candidates",
    )
    op.drop_index(
        "ix_pt_catalog_candidates_snapshot_id",
        table_name="provider_trigger_catalog_candidates",
    )
    op.drop_table("provider_trigger_catalog_candidates")
    op.drop_index(
        "ix_pt_catalog_snapshots_env_backend_imported",
        table_name="provider_trigger_catalog_snapshots",
    )
    op.drop_index(
        "ix_pt_catalog_snapshots_environment",
        table_name="provider_trigger_catalog_snapshots",
    )
    op.drop_index(
        "ix_pt_catalog_snapshots_backend_id",
        table_name="provider_trigger_catalog_snapshots",
    )
    op.drop_table("provider_trigger_catalog_snapshots")
    op.drop_index(
        "ix_pt_catalog_bootstrap_last_status",
        table_name="provider_trigger_catalog_bootstrap_state",
    )
    op.drop_index(
        "ix_pt_catalog_bootstrap_backend_id",
        table_name="provider_trigger_catalog_bootstrap_state",
    )
    op.drop_index(
        "ix_pt_catalog_bootstrap_environment",
        table_name="provider_trigger_catalog_bootstrap_state",
    )
    op.drop_table("provider_trigger_catalog_bootstrap_state")
