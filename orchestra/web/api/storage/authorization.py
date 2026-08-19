"""Object-level authorization for the generic storage endpoints.

``/storage/signed-url`` and ``/storage/download`` accept an arbitrary
``gs://`` URI and historically validated only that the bucket was on the
allowlist, so any authenticated user could read any object in any allowed
bucket (reported externally 2026-08-12, finding F1). Every allowed bucket
encodes its owner in the object path, so this module derives the required
ownership check from the path and fails closed when a path cannot be
attributed to the caller.

Per-bucket path layouts (writers are the source of truth):

- assistant media:       ``{assistant_id}/{media_type}/{filename}`` plus
  ephemeral ``tmp/{hash}.{ext}`` objects that are only ever consumed via
  the signed URL minted at upload time.
- message attachments:   ``{assistant_id}/{attachment_id}_{filename}``,
  org-chat's synthetic ``org-{org_id}/...`` prefix, and a legacy
  ``{user_id}/...`` prefix.
- account photos:        ``user/{user_id}/...``, ``user-voice/{user_id}/...``,
  ``organization/{org_id}/...`` (team photos nest under the org prefix).
- presets:               shared, environment-agnostic gallery assets.
- call recordings:       authorized separately by the recordings branch in
  ``generate_signed_url`` (the comms gateway signs them).
- generic bucket:        flat hash-named objects with no owner attribution.
"""

import re

from fastapi import HTTPException, Request, status
from sqlalchemy.orm import Session

from orchestra.db.dao.organization_dao import OrganizationDAO
from orchestra.services.bucket_service import BucketService
from orchestra.web.api.utils.assistant_ownership import require_owned_assistant

_ORG_ATTACHMENT_PREFIX = "org-"

# Recording objects are named {deploy_env}/{assistant_id}/{room}_{ts}.mp3 by
# the comms gateway's egress request. The assistant segment is what
# authorizes playback, so a path without one cannot be served.
RECORDING_PATH_RE = re.compile(r"^[^/]+/(?P<assistant_id>\d+)/[^/]+\.mp3$")


def _denied() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Access to the requested object is not permitted",
    )


def _is_org_member(session: Session, user_id: str, org_id: int) -> bool:
    orgs = OrganizationDAO(session).get_user_organizations(user_id)
    return any(org.id == org_id for org in orgs)


def _shares_organization(session: Session, user_id: str, owner_id: str) -> bool:
    if owner_id == user_id:
        return True
    dao = OrganizationDAO(session)
    caller_orgs = {org.id for org in dao.get_user_organizations(user_id)}
    if not caller_orgs:
        return False
    owner_orgs = {org.id for org in dao.get_user_organizations(owner_id)}
    return bool(caller_orgs & owner_orgs)


def authorize_object_access(
    request: Request,
    session: Session,
    bucket_service: BucketService,
    bucket_name: str,
    object_path: str,
) -> None:
    """Enforce that the authenticated caller may read ``object_path``.

    System (admin-key) callers bypass ownership entirely, matching the
    other runtime surfaces. User-key callers must be attributable as the
    object's owner via the bucket's path convention; any path that cannot
    be attributed is denied.

    :raises HTTPException: 403 when the caller may not read the object
        (404/403 can also surface from the assistant ownership check).
    """
    if getattr(request.state, "is_system_api_key", False):
        return

    user_id = str(request.state.user_id)
    segments = object_path.split("/")
    head = segments[0] if segments else ""

    if bucket_name == bucket_service.presets_bucket_name:
        # Shared preset gallery: readable by any authenticated user.
        return

    if bucket_name == bucket_service.assistant_media_bucket_name:
        # ``tmp/`` objects are excluded deliberately: they are fetched by
        # external services through the upload-time signed URL and are
        # never re-signed on a user's behalf.
        if head.isdigit():
            require_owned_assistant(request, int(head), session)
            return
        raise _denied()

    if bucket_name == bucket_service.message_attachments_bucket_name:
        if head.isdigit():
            require_owned_assistant(request, int(head), session)
            return
        if head.startswith(_ORG_ATTACHMENT_PREFIX):
            org_part = head[len(_ORG_ATTACHMENT_PREFIX) :]
            if org_part.isdigit() and _is_org_member(
                session,
                user_id,
                int(org_part),
            ):
                return
            raise _denied()
        if head == user_id:
            # Legacy user-scoped prefix from before assistant-centric paths.
            return
        raise _denied()

    if bucket_name == bucket_service.account_photo_bucket_name:
        if len(segments) >= 2:
            kind, owner = segments[0], segments[1]
            if kind in ("user", "user-voice"):
                # Profile photos and voice samples are visible to the user
                # and anyone who shares an organization with them, matching
                # the member-list surfaces that render them.
                if _shares_organization(session, user_id, owner):
                    return
            elif kind == "organization" and owner.isdigit():
                if _is_org_member(session, user_id, int(owner)):
                    return
        raise _denied()

    if bucket_name == bucket_service.call_recordings_bucket_name:
        # Cloud deployments sign recordings via the comms-gateway branch in
        # ``generate_signed_url`` before reaching this check; this branch
        # authorizes the self-host local-storage path the same way — access
        # follows the assistant named in the object path.
        match = RECORDING_PATH_RE.match(object_path)
        if match:
            require_owned_assistant(
                request,
                int(match.group("assistant_id")),
                session,
            )
            return
        raise _denied()

    # The generic bucket's flat hash-named objects carry no owner and
    # cannot be attributed to a caller: fail closed.
    raise _denied()
