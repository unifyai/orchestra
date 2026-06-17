"""Cloud Run Job and self-host entrypoint for Builtins artifact seeding.

The worker is intentionally artifact-oriented at the deployment boundary. The
current hosted artifact implemented here is the provider-backed integrations
catalog, materialized by the integrations service into Builtins contexts.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from orchestra.services.builtins_integration_sync import (
    BuiltinsSyncRequest,
    run_builtins_sync,
    write_bootstrap_state,
)
from orchestra.settings import settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

EXIT_SUCCESS = 0
EXIT_FAILED_SYNC = 1
EXIT_INVALID_PAYLOAD = 2
EXIT_MISSING_BUILTINS_PROJECT = 3
EXIT_PROVIDER_AUTH_FAILURE = 4
EXIT_DB_CONNECTIVITY_FAILURE = 5
EXIT_PARTIAL_BATCH_FAILURE = 6


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    request_source = parser.add_mutually_exclusive_group(required=True)
    request_source.add_argument(
        "--request-json",
        default="",
        help="Inline request JSON.",
    )
    request_source.add_argument(
        "--request-file",
        default="",
        help="Local request JSON file.",
    )
    request_source.add_argument(
        "--request-gcs-uri",
        default="",
        help="GCS URI for request JSON, for example gs://bucket/path/request.json.",
    )
    request_source.add_argument(
        "--request-stdin",
        action="store_true",
        help="Read request JSON from stdin.",
    )
    return parser.parse_args(argv)


def _load_gcs_payload(uri: str) -> dict:
    bucket_name, _, blob_name = uri.removeprefix("gs://").partition("/")
    if not uri.startswith("gs://") or not bucket_name or not blob_name:
        raise ValueError("GCS request URI must be gs://bucket/object")
    try:
        from google.cloud import storage
    except ImportError as exc:
        raise RuntimeError(
            "google-cloud-storage is required when using --request-gcs-uri",
        ) from exc
    return json.loads(
        storage.Client().bucket(bucket_name).blob(blob_name).download_as_text(),
    )


def _load_request_payload(args: argparse.Namespace) -> dict:
    if args.request_json:
        payload = json.loads(args.request_json)
    elif args.request_file:
        payload = json.loads(Path(args.request_file).read_text())
    elif args.request_gcs_uri:
        payload = _load_gcs_payload(args.request_gcs_uri)
        payload.setdefault("request_uri", args.request_gcs_uri)
    elif args.request_stdin:
        payload = json.loads(sys.stdin.read())
    else:
        raise RuntimeError("one request source is required")
    if not isinstance(payload, dict):
        raise ValueError(
            "Builtins artifacts seed request payload must be a JSON object",
        )
    artifact_kind = str(payload.get("artifact_kind") or "integrations")
    if artifact_kind != "integrations":
        raise ValueError(f"Unsupported Builtins artifact kind: {artifact_kind}")
    payload["artifact_kind"] = artifact_kind
    return payload


def _classify_failure(exc: Exception) -> int:
    message = str(exc).lower()
    if isinstance(exc, OperationalError) or "connection refused" in message:
        return EXIT_DB_CONNECTIVITY_FAILURE
    if "builtins project" in message and "does not exist" in message:
        return EXIT_MISSING_BUILTINS_PROJECT
    if (
        "auth" in message
        or "credential" in message
        or "401" in message
        or "403" in message
    ):
        return EXIT_PROVIDER_AUTH_FAILURE
    if "batch" in message:
        return EXIT_PARTIAL_BATCH_FAILURE
    return EXIT_FAILED_SYNC


def _emit(payload: dict) -> None:
    print(json.dumps(payload, sort_keys=True), flush=True)


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parse_args(argv or sys.argv[1:])
        payload = _load_request_payload(args)
        sync_request = BuiltinsSyncRequest.from_payload(payload)
    except Exception as exc:
        _emit(
            {
                "status": "failed",
                "error": f"invalid payload: {exc}",
                "exit_code": EXIT_INVALID_PAYLOAD,
            },
        )
        return EXIT_INVALID_PAYLOAD

    engine = create_engine(str(settings.db_url), pool_pre_ping=True)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    logger.info(
        "Starting Builtins artifacts seed job artifact=%s backend=%s environment=%s "
        "desired_hash=%s run_id=%s mode=%s workers=%s",
        "integrations",
        sync_request.backend_id,
        sync_request.environment,
        sync_request.desired_hash,
        sync_request.run_id,
        sync_request.mode,
        sync_request.workers,
    )
    try:
        with session_factory() as session:
            write_bootstrap_state(session, request=sync_request, status="running")
            session.commit()
        result = run_builtins_sync(session_factory, sync_request)
        with session_factory() as session:
            write_bootstrap_state(
                session,
                request=sync_request,
                status=result.status,
                result=result,
                error=result.error,
            )
            session.commit()
        payload = {
            **result.to_payload(),
            "artifact_kind": "integrations",
            "exit_code": EXIT_SUCCESS,
        }
        logger.info("Builtins artifacts seed result=%s", json.dumps(payload))
        _emit(payload)
        return EXIT_SUCCESS if result.status == "success" else EXIT_FAILED_SYNC
    except Exception as exc:
        exit_code = _classify_failure(exc)
        error_payload = {
            "status": "failed",
            "artifact_kind": "integrations",
            "run_id": sync_request.run_id,
            "desired_hash": sync_request.desired_hash,
            "request_uri": sync_request.request_uri,
            "completed_batches": 0,
            "skipped_batches": 0,
            "apps_upserted": 0,
            "tools_upserted": 0,
            "error": str(exc),
            "exit_code": exit_code,
        }
        try:
            with session_factory() as session:
                write_bootstrap_state(
                    session,
                    request=sync_request,
                    status="failed",
                    error=str(exc),
                )
                session.commit()
        except Exception:
            logger.exception(
                "Failed to write Builtins artifacts failure bootstrap state",
            )
        logger.exception("Builtins artifacts seed failed")
        _emit(error_payload)
        return exit_code


if __name__ == "__main__":
    sys.exit(main())
