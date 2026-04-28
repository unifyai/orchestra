"""Decommission platform-issued assistant mailboxes.

Background
----------

Platform-issued ``@unify.ai`` (Google Workspace) and
``@unifyailtd123.onmicrosoft.com`` (Microsoft 365) mailboxes are being
retired in favour of BYOD email only. New platform mailboxes can no
longer be provisioned (``POST /assistant/{id}/contact`` returns 410 for
``contact_type='email'`` + ``provisioned_by='platform'``), and we now
need to tear down the rows that were provisioned before the cut-off.

What this worker does
---------------------

For every active ``AssistantContact`` row where ``contact_type='email'``
and ``provisioned_by='platform'`` (or, when targeted by id, just that
one row):

1. **Picks the right deprovision API by email domain**, not by the
   stored ``provider`` column.  This defends against the historical bug
   where some MS365 mailboxes were stored with ``provider='google_
   workspace'`` (e.g. contacts created before the MS365 column landed in
   2026-04).
2. **Calls the Communication service** to actually delete the mailbox
   (``DELETE /gmail/delete`` or ``DELETE /outlook/delete``).  Both
   endpoints are idempotent — an already-absent user is treated as
   success — so re-running the script after a partial failure is safe.
3. **Soft-deletes the AssistantContact row** (``status='deleted'``,
   ``deleted_at=now()``).  The row is kept on disk so billing /
   reconciliation history remains intact; it just stops appearing in
   active-contact lookups (and therefore stops being charged).
4. **Optionally cleans up sibling OAuth secrets** when, and only when,
   *all* of these hold:

   - the mailbox we just tore down was MS365, and
   - the assistant has *no other active email contact* (BYOD safety),
     and
   - ``--skip-secret-cleanup`` was not passed.

   Google Workspace platform mailboxes use admin-SDK service-account
   delegation (no per-mailbox OAuth tokens stored), so nothing to clean
   for those.  ``GOOGLE_*`` secret rows are *never* touched — when they
   exist they belong to BYOD Google connections.

Modes
-----

``--dry-run`` (default)
    List every row that *would* be processed, including any provider
    mismatches detected and which secrets *would* be cleaned.  No DB
    writes, no external HTTP calls.

``--apply``
    Actually run the teardown.

``--contact-id <id>``
    Restrict to a single ``AssistantContact.id``.  Use for incremental
    rollout.  The script refuses to touch a contact whose
    ``provisioned_by != 'platform'``.

``--all``
    Process every active platform-provisioned email row.

``--skip-secret-cleanup``
    Skip step 4 entirely.

``--continue-on-error``
    Keep going past the first failed row (default: stop).

Usage
-----

::

    # 1. Dry-run audit (always start here)
    python -m orchestra.workers.teardown_platform_mailboxes \\
        --dry-run --all

    # 2. Tear down one row (lowest-risk first)
    python -m orchestra.workers.teardown_platform_mailboxes \\
        --apply --contact-id 8

    # 3. Tear down everything
    python -m orchestra.workers.teardown_platform_mailboxes \\
        --apply --all --continue-on-error

Environment
-----------

Same as the Orchestra service:

- ``ORCHESTRA_DB_HOST``, ``ORCHESTRA_DB_PORT``, ``ORCHESTRA_DB_USER``,
  ``ORCHESTRA_DB_PASS``, ``ORCHESTRA_DB_BASE``
- ``UNITY_COMMS_URL`` — base URL of the Communication service
- ``ORCHESTRA_ADMIN_KEY`` — bearer token for the Communication API

Exit codes
----------

``0``
    Success (or, in dry-run, plan generated successfully).

``1``
    At least one row failed during ``--apply``.

``2``
    Misconfiguration (missing env vars, no target selected, etc.).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from orchestra.db.dao.assistant_secret_dao import AssistantSecretDAO
from orchestra.db.models.orchestra_models import AssistantContact
from orchestra.settings import settings
from orchestra.web.api.utils.assistant_infra import (
    delete_email,
    delete_outlook_email,
)

logger = logging.getLogger("teardown_platform_mailboxes")


# Domain → provider used by the actual mailbox host. The script routes
# the deprovision call by domain (not by the stored ``provider`` column)
# so we tolerate the historical mis-label of some MS365 contacts as
# ``google_workspace``.
DOMAIN_TO_PROVIDER: dict[str, str] = {
    "unify.ai": "google_workspace",
    "unifyailtd123.onmicrosoft.com": "microsoft_365",
}


# Secret-name prefixes tied to a platform-issued mailbox of each
# provider. ``MICROSOFT_*`` rows on a platform-MS365 assistant are the
# delegated-OAuth tokens for that very mailbox; once the mailbox is
# deleted from the tenant the tokens are dead, so it's safe to remove
# them. Google Workspace platform mailboxes use service-account
# delegation, so no per-mailbox OAuth secrets exist to clean up.
PROVIDER_SECRET_PREFIXES: dict[str, tuple[str, ...]] = {
    "microsoft_365": ("MICROSOFT_",),
    "google_workspace": (),
}


@dataclass
class TeardownPlan:
    """What the script would do to one platform-mailbox row."""

    contact_id: int
    assistant_id: int
    contact_value: str
    stored_provider: str | None
    effective_provider: str
    provider_mismatch: bool
    will_clean_secrets: bool
    secrets_to_delete: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TeardownResult:
    """What the script actually did to one platform-mailbox row."""

    contact_id: int
    assistant_id: int
    contact_value: str
    deprovision_status: str  # "ok" | "error"
    deprovision_detail: str
    soft_delete_status: str  # "ok" | "already_deleted" | "missing" | "error"
    secrets_deleted: int
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _provider_for_domain(contact_value: str) -> str | None:
    """Return the expected provider for a mailbox based on its domain."""
    if not contact_value or "@" not in contact_value:
        return None
    domain = contact_value.split("@", 1)[1].lower()
    return DOMAIN_TO_PROVIDER.get(domain)


def _build_session() -> Session:
    """Open a one-off SQLAlchemy session against the Orchestra DB.

    We deliberately don't import ``orchestra.web.application`` so this
    script can run as a Cloud Run Job / standalone process without
    booting the FastAPI app, Sentry, OpenTelemetry exporters, etc.
    """
    engine = create_engine(str(settings.db_url), pool_pre_ping=True)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()


def select_target_rows(
    session: Session,
    *,
    contact_id: int | None,
    include_all: bool,
) -> list[AssistantContact]:
    """Return the AssistantContact rows the script will operate on."""
    if contact_id is None and not include_all:
        raise SystemExit(
            "Refusing to run without --contact-id <id> or --all. "
            "Use --dry-run --all to enumerate.",
        )

    base_q = (
        session.query(AssistantContact)
        .filter(AssistantContact.contact_type == "email")
        .filter(AssistantContact.status != "deleted")
    )

    if contact_id is not None:
        rows = base_q.filter(AssistantContact.id == contact_id).all()
        if not rows:
            logger.warning("No active email contact with id=%s", contact_id)
            return []
        row = rows[0]
        if row.provisioned_by != "platform":
            logger.warning(
                "Refusing to touch contact id=%s (provisioned_by=%s); "
                "this script only operates on platform-issued mailboxes.",
                contact_id,
                row.provisioned_by,
            )
            return []
        return rows

    return (
        base_q.filter(AssistantContact.provisioned_by == "platform")
        .order_by(AssistantContact.id)
        .all()
    )


def plan_teardown(
    session: Session,
    rows: Iterable[AssistantContact],
    *,
    skip_secret_cleanup: bool,
) -> list[TeardownPlan]:
    """Build a per-row teardown plan without making any external calls."""
    plans: list[TeardownPlan] = []
    secret_dao = AssistantSecretDAO(session)
    for row in rows:
        domain_provider = _provider_for_domain(row.contact_value or "")
        # Domain-derived provider wins over the stored column; fall back
        # to the stored value if the domain is unrecognised so we never
        # silently route to the wrong API for an unexpected mailbox.
        effective = domain_provider or row.provider or "google_workspace"

        prefixes: tuple[str, ...] = (
            PROVIDER_SECRET_PREFIXES.get(effective, ())
            if not skip_secret_cleanup
            else ()
        )
        secrets_to_delete: list[str] = []
        will_clean_secrets = False
        if prefixes:
            other_emails_active = (
                session.query(AssistantContact)
                .filter(
                    AssistantContact.assistant_id == row.assistant_id,
                    AssistantContact.contact_type == "email",
                    AssistantContact.id != row.id,
                    AssistantContact.status != "deleted",
                )
                .count()
            )
            # Only ever clean platform-mailbox secrets when there's no
            # remaining active email contact for the assistant. The DB
            # already enforces this via ``uq_assistant_contact_type_active``
            # (one active contact per type per assistant), but we keep
            # the explicit check as a defence-in-depth BYOD safety net
            # in case that constraint is ever relaxed.
            if other_emails_active == 0:
                all_secrets = secret_dao.get_all(int(row.assistant_id))
                secrets_to_delete = sorted(
                    name
                    for name in all_secrets
                    if any(name.startswith(p) for p in prefixes)
                )
                will_clean_secrets = bool(secrets_to_delete)

        plans.append(
            TeardownPlan(
                contact_id=int(row.id),
                assistant_id=int(row.assistant_id),
                contact_value=row.contact_value or "",
                stored_provider=row.provider,
                effective_provider=effective,
                provider_mismatch=(
                    domain_provider is not None
                    and row.provider is not None
                    and domain_provider != row.provider
                ),
                will_clean_secrets=will_clean_secrets,
                secrets_to_delete=secrets_to_delete,
            ),
        )
    return plans


async def execute_plan(
    session: Session,
    plan: TeardownPlan,
    *,
    deploy_env: str | None,
) -> TeardownResult:
    """Apply one TeardownPlan to the DB + Communication service."""
    result = TeardownResult(
        contact_id=plan.contact_id,
        assistant_id=plan.assistant_id,
        contact_value=plan.contact_value,
        deprovision_status="pending",
        deprovision_detail="",
        soft_delete_status="pending",
        secrets_deleted=0,
    )

    # 1. External deprovision via Communication service. Both delete
    #    endpoints treat already-absent mailboxes as success.
    try:
        if plan.effective_provider == "microsoft_365":
            comms_resp = await delete_outlook_email(
                plan.contact_value,
                deploy_env=deploy_env,
            )
        else:
            comms_resp = await delete_email(
                plan.contact_value,
                deploy_env=deploy_env,
            )
        result.deprovision_status = "ok"
        result.deprovision_detail = json.dumps(comms_resp)[:500]
    except Exception as exc:  # noqa: BLE001
        result.deprovision_status = "error"
        result.error = f"deprovision failed: {exc!r}"
        return result

    # 2. Soft-delete the row (kept for billing audit history).
    try:
        row = session.get(AssistantContact, plan.contact_id)
        if row is None:
            result.soft_delete_status = "missing"
        elif row.status == "deleted":
            result.soft_delete_status = "already_deleted"
        else:
            row.status = "deleted"
            row.deleted_at = datetime.now(timezone.utc)
            session.flush()
            result.soft_delete_status = "ok"
    except Exception as exc:  # noqa: BLE001
        result.soft_delete_status = "error"
        result.error = (result.error + "; " if result.error else "") + (
            f"soft-delete failed: {exc!r}"
        )
        session.rollback()
        return result

    # 3. Optionally clean up sibling secrets.
    if plan.secrets_to_delete:
        try:
            secret_dao = AssistantSecretDAO(session)
            for name in plan.secrets_to_delete:
                if secret_dao.delete(plan.assistant_id, name):
                    result.secrets_deleted += 1
            session.flush()
        except Exception as exc:  # noqa: BLE001
            result.error = (result.error + "; " if result.error else "") + (
                f"secret cleanup failed: {exc!r}"
            )
            session.rollback()
            return result

    session.commit()
    return result


async def amain(args: argparse.Namespace) -> int:
    """Async entrypoint."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.apply:
        if not os.environ.get("UNITY_COMMS_URL"):
            logger.error("UNITY_COMMS_URL is not set; aborting.")
            return 2
        if not os.environ.get("ORCHESTRA_ADMIN_KEY"):
            logger.error("ORCHESTRA_ADMIN_KEY is not set; aborting.")
            return 2

    session = _build_session()
    try:
        rows = select_target_rows(
            session,
            contact_id=args.contact_id,
            include_all=args.all,
        )
        plans = plan_teardown(
            session,
            rows,
            skip_secret_cleanup=args.skip_secret_cleanup,
        )

        print("# === Teardown plan ===")
        print(json.dumps([p.to_dict() for p in plans], indent=2, default=str))
        print(f"# total rows: {len(plans)}")
        mismatches = [p for p in plans if p.provider_mismatch]
        if mismatches:
            print(
                f"# provider-mismatch rows: {len(mismatches)} "
                "(deprovision will route by email domain, not by stored provider)",
            )

        if args.dry_run:
            return 0

        results: list[TeardownResult] = []
        had_failure = False
        for plan in plans:
            logger.info(
                "Tearing down contact_id=%s value=%s assistant_id=%s ...",
                plan.contact_id,
                plan.contact_value,
                plan.assistant_id,
            )
            result = await execute_plan(
                session,
                plan,
                deploy_env=args.deploy_env,
            )
            results.append(result)
            logger.info("→ %s", json.dumps(result.to_dict(), default=str))
            if result.error:
                had_failure = True
                if not args.continue_on_error:
                    logger.error(
                        "Stopping after first failure; "
                        "pass --continue-on-error to proceed past failures.",
                    )
                    break

        print("# === Teardown results ===")
        print(json.dumps([r.to_dict() for r in results], indent=2, default=str))
        return 1 if had_failure else 0
    finally:
        session.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="teardown_platform_mailboxes",
        description=(
            "Decommission platform-issued assistant mailboxes "
            "(@unify.ai / @unifyailtd123.onmicrosoft.com). "
            "Soft-deletes the AssistantContact row, calls the "
            "Communication service to delete the actual mailbox, and "
            "optionally cleans up sibling MICROSOFT_* OAuth secrets."
        ),
    )

    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--contact-id",
        type=int,
        help="Process only the AssistantContact row with this id.",
    )
    target.add_argument(
        "--all",
        action="store_true",
        help="Process all active platform-issued email contacts.",
    )

    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Preview only — no DB writes, no external calls (default).",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Actually run the teardown.",
    )

    p.add_argument(
        "--skip-secret-cleanup",
        action="store_true",
        help=(
            "Do not delete any MICROSOFT_* secrets from assistant_secrets. "
            "Use when you want to clean those up in a separate, audited step."
        ),
    )
    p.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Keep going if one row fails (default: stop on first failure).",
    )
    p.add_argument(
        "--deploy-env",
        default=None,
        help=(
            "Optional deploy_env hint forwarded to delete_email / "
            "delete_outlook_email. Usually inferred from UNITY_COMMS_URL."
        ),
    )

    args = p.parse_args(argv)
    if args.apply:
        args.dry_run = False
    return args


def main() -> int:
    args = parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
