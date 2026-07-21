"""DAO for external_field_binding rows."""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import select

from orchestra.db.models.core_models import ExternalFieldBinding


class ExternalFieldBindingDAO:
    def __init__(self, session):
        self.session = session

    def upsert(
        self,
        *,
        project_id: int,
        context_id: int,
        field_name: str,
        connector_id: str,
        binding: dict[str, Any],
        bump_version: bool = False,
    ) -> ExternalFieldBinding:
        existing = self.get(
            project_id=project_id,
            context_id=context_id,
            field_name=field_name,
        )
        if existing is None:
            row = ExternalFieldBinding(
                project_id=project_id,
                context_id=context_id,
                field_name=field_name,
                connector_id=connector_id,
                binding=binding,
                binding_version=1,
                is_active=True,
            )
            self.session.add(row)
            self.session.flush()
            return row

        previous = dict(existing.binding or {})
        existing.connector_id = connector_id
        existing.binding = binding
        existing.is_active = True
        if bump_version or previous != binding:
            existing.binding_version = int(existing.binding_version or 1) + 1
        self.session.flush()
        return existing

    def get(
        self,
        *,
        project_id: int,
        context_id: int,
        field_name: str,
    ) -> Optional[ExternalFieldBinding]:
        return (
            self.session.execute(
                select(ExternalFieldBinding).where(
                    ExternalFieldBinding.project_id == project_id,
                    ExternalFieldBinding.context_id == context_id,
                    ExternalFieldBinding.field_name == field_name,
                ),
            )
            .scalars()
            .first()
        )

    def list_active(
        self,
        *,
        project_id: int,
        context_id: int,
        field_names: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        q = select(ExternalFieldBinding).where(
            ExternalFieldBinding.project_id == project_id,
            ExternalFieldBinding.context_id == context_id,
            ExternalFieldBinding.is_active.is_(True),
        )
        if field_names is not None:
            q = q.where(ExternalFieldBinding.field_name.in_(field_names))
        rows = self.session.execute(q).scalars().all()
        return [
            {
                "field_name": r.field_name,
                "connector_id": r.connector_id,
                "binding": dict(r.binding or {}),
                "binding_version": int(r.binding_version or 1),
                "is_active": bool(r.is_active),
            }
            for r in rows
        ]

    def deactivate(
        self,
        *,
        project_id: int,
        context_id: int,
        field_name: str,
    ) -> bool:
        row = self.get(
            project_id=project_id,
            context_id=context_id,
            field_name=field_name,
        )
        if row is None:
            return False
        row.is_active = False
        self.session.flush()
        return True
