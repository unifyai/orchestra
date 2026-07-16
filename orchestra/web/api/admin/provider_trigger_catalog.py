"""Admin diagnostics for provider trigger catalog import."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from orchestra.db.dao.trigger_catalog_dao import TriggerCatalogDAO
from orchestra.db.dependencies import get_db_session
from orchestra.provider_triggers.catalog_import.registry import (
    supported_trigger_catalog_backends,
)
from orchestra.services.trigger_catalog_import_service import (
    TriggerCatalogImportService,
)
from orchestra.web.api.dependencies import auth_admin_key

router = APIRouter()


@router.get("/provider-trigger-catalog/bootstrap")
def list_provider_trigger_catalog_bootstrap(
    session: Session = Depends(get_db_session),
    _: str = Depends(auth_admin_key),
) -> dict:
    """Return bootstrap/import state for staged provider trigger catalogs."""

    dao = TriggerCatalogDAO(session)
    rows = []
    for backend_id in supported_trigger_catalog_backends():
        for environment in ("selfhost", "staging", "production"):
            row = dao.get_bootstrap_state(
                environment=environment,
                backend_id=backend_id,
            )
            if row is None:
                continue
            rows.append(
                {
                    "environment": row.environment,
                    "backend_id": row.backend_id,
                    "desired_hash": row.desired_hash,
                    "last_status": row.last_status,
                    "last_error": row.last_error,
                    "candidates_imported": row.candidates_imported,
                    "last_imported_at": row.last_imported_at,
                    "last_import_diagnostics_json": row.last_import_diagnostics_json,
                },
            )
    return {"bootstrap_states": rows}


@router.post("/provider-trigger-catalog/import/{backend_id}")
def import_provider_trigger_catalog(
    backend_id: str,
    environment: str = "selfhost",
    session: Session = Depends(get_db_session),
    _: str = Depends(auth_admin_key),
) -> dict:
    """Import one provider trigger catalog into staging."""

    service = TriggerCatalogImportService(session)
    try:
        result = service.import_catalog(
            backend_id=backend_id,
            environment=environment,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception:
        session.commit()
        raise
    session.commit()
    return {
        "backend_id": result.backend_id,
        "environment": result.environment,
        "skipped": result.skipped,
        "content_hash": result.content_hash,
        "catalog_version": result.catalog_version,
        "entry_count": result.entry_count,
        "snapshot_id": result.snapshot_id,
        "diagnostics": result.diagnostics,
    }
