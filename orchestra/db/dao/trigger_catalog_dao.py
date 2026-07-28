"""Persistence helpers for provider trigger catalog import staging."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orchestra.db.models.provider_trigger_catalog_models import (
    ProviderTriggerCatalogBootstrapState,
    ProviderTriggerCatalogCandidate,
    ProviderTriggerCatalogSnapshot,
)
from orchestra.provider_triggers.catalog_import.types import ProviderTriggerCatalogEntry


class TriggerCatalogDAO:
    """Read and write staged provider trigger catalog state."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_bootstrap_state(
        self,
        *,
        environment: str,
        backend_id: str,
    ) -> ProviderTriggerCatalogBootstrapState | None:
        return self.session.scalar(
            select(ProviderTriggerCatalogBootstrapState).where(
                ProviderTriggerCatalogBootstrapState.environment == environment,
                ProviderTriggerCatalogBootstrapState.backend_id == backend_id,
            ),
        )

    def get_or_create_bootstrap_state(
        self,
        *,
        environment: str,
        backend_id: str,
    ) -> ProviderTriggerCatalogBootstrapState:
        row = self.get_bootstrap_state(
            environment=environment,
            backend_id=backend_id,
        )
        if row is not None:
            return row
        row = ProviderTriggerCatalogBootstrapState(
            environment=environment,
            backend_id=backend_id,
            desired_hash="",
            last_status="pending",
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get_snapshot_by_hash(
        self,
        *,
        environment: str,
        backend_id: str,
        content_hash: str,
    ) -> ProviderTriggerCatalogSnapshot | None:
        return self.session.scalar(
            select(ProviderTriggerCatalogSnapshot).where(
                ProviderTriggerCatalogSnapshot.environment == environment,
                ProviderTriggerCatalogSnapshot.backend_id == backend_id,
                ProviderTriggerCatalogSnapshot.content_hash == content_hash,
            ),
        )

    def create_snapshot(
        self,
        *,
        environment: str,
        backend_id: str,
        catalog_version: str,
        content_hash: str,
        raw_entry_count: int,
    ) -> ProviderTriggerCatalogSnapshot:
        snapshot = ProviderTriggerCatalogSnapshot(
            environment=environment,
            backend_id=backend_id,
            catalog_version=catalog_version,
            content_hash=content_hash,
            raw_entry_count=raw_entry_count,
        )
        self.session.add(snapshot)
        self.session.flush()
        return snapshot

    def insert_candidates(
        self,
        *,
        snapshot_id: int,
        entries: list[ProviderTriggerCatalogEntry],
    ) -> list[ProviderTriggerCatalogCandidate]:
        rows: list[ProviderTriggerCatalogCandidate] = []
        for entry in entries:
            candidate_payload = entry.normalized_dict()
            if entry.raw_metadata:
                candidate_payload["raw_metadata"] = entry.raw_metadata
            row = ProviderTriggerCatalogCandidate(
                snapshot_id=snapshot_id,
                backend_id=entry.backend_id,
                provider_trigger_slug=entry.provider_trigger_slug,
                provider_version=entry.provider_version,
                canonical_app_hint=entry.canonical_app_hint,
                candidate_json=candidate_payload,
                unit_hash=entry.unit_hash(),
            )
            self.session.add(row)
            rows.append(row)
        self.session.flush()
        return rows

    def list_candidates_for_snapshot(
        self,
        snapshot_id: int,
        *,
        canonical_app_hints: set[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[ProviderTriggerCatalogCandidate]:
        """List one snapshot's candidates, optionally scoped to app hints.

        ``canonical_app_hints`` must be applied before ``limit``/``offset`` so
        a caller paging one app's rows gets a page window drawn from that
        app's candidates, not from the whole (unfiltered) snapshot.
        """

        query = select(ProviderTriggerCatalogCandidate).where(
            ProviderTriggerCatalogCandidate.snapshot_id == snapshot_id,
        )
        if canonical_app_hints is not None:
            query = query.where(
                func.lower(ProviderTriggerCatalogCandidate.canonical_app_hint).in_(
                    canonical_app_hints,
                ),
            )
        query = query.order_by(ProviderTriggerCatalogCandidate.provider_trigger_slug)
        if offset:
            query = query.offset(offset)
        if limit is not None:
            query = query.limit(limit)
        return list(self.session.scalars(query))
