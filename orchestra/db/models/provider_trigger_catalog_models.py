"""Staging state for imported provider trigger catalogs."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import (
    TIMESTAMP,
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

from orchestra.db.base import Base

JSON_EMPTY_OBJECT = sa.text("'{}'::jsonb")


class ProviderTriggerCatalogBootstrapState(Base):
    """Last applied import hash for one provider trigger catalog backend."""

    __tablename__ = "provider_trigger_catalog_bootstrap_state"

    id = Column(Integer, primary_key=True)
    environment = Column(String, nullable=False, index=True)
    backend_id = Column(String, nullable=False, index=True)
    desired_hash = Column(String(128), nullable=False)
    last_status = Column(
        String(32), nullable=False, server_default="pending", index=True
    )
    last_error = Column(Text, nullable=True)
    candidates_imported = Column(Integer, nullable=False, server_default="0")
    last_import_diagnostics_json = Column(
        JSONB,
        nullable=False,
        server_default=JSON_EMPTY_OBJECT,
    )
    last_imported_at = Column(TIMESTAMP(timezone=True), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "environment",
            "backend_id",
            name="uq_provider_trigger_catalog_bootstrap_env_backend",
        ),
    )


class ProviderTriggerCatalogSnapshot(Base):
    """Immutable imported provider trigger catalog snapshot."""

    __tablename__ = "provider_trigger_catalog_snapshots"

    id = Column(Integer, primary_key=True)
    backend_id = Column(String, nullable=False, index=True)
    environment = Column(String, nullable=False, index=True)
    catalog_version = Column(String, nullable=False)
    content_hash = Column(String(128), nullable=False)
    raw_entry_count = Column(Integer, nullable=False, server_default="0")
    imported_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "environment",
            "backend_id",
            "content_hash",
            name="uq_provider_trigger_catalog_snapshots_env_backend_hash",
        ),
        Index(
            "ix_pt_catalog_snapshots_env_backend_imported",
            "environment",
            "backend_id",
            "imported_at",
        ),
    )


class ProviderTriggerCatalogCandidate(Base):
    """One normalized provider trigger entry from a catalog snapshot."""

    __tablename__ = "provider_trigger_catalog_candidates"

    id = Column(Integer, primary_key=True)
    snapshot_id = Column(
        Integer,
        ForeignKey("provider_trigger_catalog_snapshots.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    backend_id = Column(String, nullable=False, index=True)
    provider_trigger_slug = Column(String, nullable=False, index=True)
    provider_version = Column(String, nullable=True)
    canonical_app_hint = Column(String, nullable=True)
    candidate_json = Column(JSONB, nullable=False, server_default=JSON_EMPTY_OBJECT)
    unit_hash = Column(String(128), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "snapshot_id",
            "provider_trigger_slug",
            name="uq_provider_trigger_catalog_candidates_snapshot_slug",
        ),
    )
