import base64
import io
import json
import logging
import math
import os
import re
import time
import urllib.request
from decimal import Decimal
from typing import Any, List, Literal, NamedTuple, Optional

import mutagen
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.encoders import jsonable_encoder
from sqlalchemy import delete, or_, select, tuple_
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchestra.db.dao.api_key_dao import ApiKeyDAO
from orchestra.db.dao.assistant_contact_dao import AssistantContactDAO
from orchestra.db.dao.assistant_dao import AssistantDAO
from orchestra.db.dao.assistant_secret_dao import AssistantSecretDAO
from orchestra.db.dao.assistant_workspace_file_access_dao import (
    AssistantWorkspaceFileAccessDAO,
)
from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.desktop_dao import DesktopDAO
from orchestra.db.dao.log_event_dao import LogEventDAO
from orchestra.db.dao.ms_teams_bot_dao import MsTeamsBotDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.db.dao.role_dao import RoleDAO
from orchestra.db.dao.slack_dao import SlackDAO
from orchestra.db.dao.team_dao import TeamDAO
from orchestra.db.dao.user_dao import UserDAO
from orchestra.db.dao.voice_dao import VoiceDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import (
    CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS,
    CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
    CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
    CONTACT_MEMBERSHIP_SCOPE_TEAM,
    TEAM_STATUS_ACTIVE,
    Assistant,
    AssistantConsoleConfig,
    AssistantExternalIPRotation,
    ContactMembership,
    Context,
    LogEvent,
    LogEventContext,
    Organization,
    Project,
    Team,
    TeamAssistantMembership,
    User,
)
from orchestra.lib.billing import get_billing_entity
from orchestra.services.assistant_bootstrap import ensure_owner_contact_row
from orchestra.services.assistant_cleanup_service import (
    CleanupSource,
    build_cleanup_spec_from_assistant,
    deprovision_assistant_contacts,
    enqueue_cleanup_tasks,
    process_assistant_cleanup_tasks,
    purge_assistant_owner,
)
from orchestra.services.assistant_external_ip_service import (
    ensure_pending_assistant_external_ip,
    reconcile_assistant_external_ip,
    record_assistant_external_ip_attachment,
    record_timezone_pool_location_intent,
    release_managed_desktop_external_ip,
    request_assistant_external_ip_rotation,
    run_assistant_external_ip_rotation,
)
from orchestra.services.assistant_team_ownership_service import (
    TeamOwnershipTransferError,
    transfer_assistant_to_team_owned,
)
from orchestra.services.bucket_service import create_bucket_service
from orchestra.services.cartesia_service import CartesiaAPIError, CartesiaService
from orchestra.services.contact_membership_service import (
    PERSONAL_BOSS_CONTACT_ID,
    PERSONAL_SELF_CONTACT_ID,
    ensure_personal_contact_memberships,
    ensure_team_contact_memberships,
)
from orchestra.services.coordinator_multiplayer import (
    MultiplayerFlipError,
    display_name_conflict,
    flip_coordinator_to_multiplayer,
    is_reserved_coordinator_name,
)
from orchestra.services.coordinator_service import (
    build_onboarding_catalog,
    compose_voice_intro_briefing,
    compute_onboarding_render,
    derive_onboarding_progress,
    emit_onboarding_session_started_event,
    emit_onboarding_step_completed_event_safe_sync,
    emit_onboarding_step_event,
    emit_onboarding_step_reset_event,
    emit_onboarding_step_skipped_event,
    emit_onboarding_step_started_event,
    emit_secret_landed_event,
    get_coordinator_state,
    heal_coordinator_universal_contacts,
    require_authorized_coordinator,
    require_authorized_delegate_target,
    reset_coordinator_state,
    seed_coordinator_transcript,
    set_coordinator_state,
)
from orchestra.services.deepgram_service import DeepgramAPIError, DeepgramService
from orchestra.services.elevenlabs_service import ElevenLabsAPIError, ElevenLabsService
from orchestra.services.managed_desktop_service import (
    MANAGED_DESKTOP_MODES,
    charge_managed_desktop_first_month,
    disable_managed_desktop,
    get_managed_desktop_monthly_cost,
    managed_desktop_entitled,
)
from orchestra.services.openai_service import OpenAIAPIError, OpenAIService
from orchestra.services.org_wide_sharing_service import (
    add_assistant_to_team,
    enroll_assistant_in_org_wide_team,
)
from orchestra.services.personal_workspace_service import personal_workspace_is_disabled
from orchestra.services.replicate_service import ReplicateAPIError, ReplicateService
from orchestra.services.team_cleanup_service import purge_assistant_memberships
from orchestra.services.team_membership_refresh_service import (
    publish_membership_refreshes_best_effort,
)
from orchestra.services.universal_unity_contacts import (
    AMBIGUOUS_UNIVERSAL_ADMIN_LOOKUP_DETAIL,
    UNIVERSAL_CONTACT_TYPES,
    drifted_universal_coordinator_contact_types,
    is_ambiguous_universal_admin_contact_lookup,
    missing_universal_coordinator_contact_types,
)
from orchestra.services.universal_unity_discord import (
    ensure_universal_unity_discord_pool,
    get_universal_unity_discord_bot_id,
    notify_comms_discord_sync,
)
from orchestra.settings import settings
from orchestra.web.api.assistant.default_models import list_model_options
from orchestra.web.api.assistant.schema import (
    AdminUpdateAssistant,
    AdminUpdateAssistantResponse,
    AdminUpdateUserByAssistant,
    AdminUpdateUserByAssistantResponse,
    AssistantContactCreate,
    AssistantContactIdentityRoot,
    AssistantContactRead,
    AssistantContactRemoval,
    AssistantContactUpdate,
    AssistantCreate,
    AssistantPhotoUploadResponse,
    AssistantRead,
    AssistantSpendingLimitResponse,
    AssistantSpendResponse,
    AssistantStatus,
    AssistantTransferResponse,
    AssistantTransferToOrgRequest,
    AssistantTransferToPersonalRequest,
    AssistantTransferToTeamOwnedRequest,
    AssistantTransferToTeamOwnedResponse,
    AssistantUpdate,
    AssistantUserDesktopLink,
    AssistantVideoUploadResponse,
    ConnectRequest,
    ConnectResponse,
    ConsoleConfigRead,
    Contact,
    ContactMembershipCreate,
    ContactMembershipDeleteResponse,
    ContactMembershipRead,
    ContactMembershipUpsertResponse,
    CoordinatorDelegateRequest,
    CoordinatorDelegateResponse,
    CoordinatorMultiplayerFlip,
    CoordinatorResetResponse,
    CoordinatorStateResponse,
    CoordinatorStateUpdate,
    CoordinatorTranscriptSeed,
    CoordinatorTranscriptSeedResponse,
    CoordinatorWakeupResponse,
    DefaultModelOptionRead,
    GrantedFeaturesResponse,
    InfoResponse,
    ManagedDesktopEnable,
    ManagedDesktopIPRotationRead,
    ManagedDesktopNetworkIdentityRead,
    ManagedDesktopNetworkIdentityReport,
    ManagedDesktopStatusRead,
    OnboardingCatalog,
    OnboardingSessionStarted,
    OnboardingSessionStartedResponse,
    OnboardingStepEventRequest,
    OnboardingStepEventResponse,
    PhotoGenerateRequest,
    ReplicatePredictionResponse,
    SecretCreate,
    SecretUpdate,
    SpendingLimitRequest,
    VoiceCreate,
    VoiceDesignCreateFromPreviewRequest,
    VoiceDesignGeneratePreviewsAPIResponse,
    VoiceDesignGeneratePreviewsRequest,
    VoiceGenerateRequest,
    VoiceRead,
    WorkspaceFileAccessAdminResponse,
    WorkspaceFileListResponse,
    WorkspaceFileNode,
    WorkspaceFilePolicy,
    WorkspaceFilePolicyUpdate,
)
from orchestra.web.api.dependencies import require_console_origin_for_free_accounts
from orchestra.web.api.utils.assistant_infra import (
    comms_explicitly_configured,
    create_phone_number,
    create_pubsub_topic,
    delegate_to_colleague_runtime,
    delete_phone_number,
    delete_pubsub_topic,
    get_runtime_status,
    log_pre_hire_chat,
    reawaken_assistant,
    trigger_contact_sync_safe,
    wake_up_assistant,
    wake_up_coordinator_best_effort,
)
from orchestra.web.api.utils.assistant_ownership import require_owned_assistant


class ResolvedContactIds(NamedTuple):
    """Resolved self and boss contact ids for one assistant."""

    self_contact_id: int
    boss_contact_id: int


def normalize_phone_parameter(raw_phone: Optional[str]) -> Optional[str]:
    """
    Normalize phone parameter that may have been URL-decoded.
    FastAPI URL-decodes '+' to space, so convert leading space back to '+'.
    """
    if raw_phone and raw_phone.startswith(" "):
        return "+" + raw_phone[1:]
    return raw_phone


def _open_request_session(request: Request) -> Session:
    """Open a standalone session bound to the app's current engine."""

    session_factory = request.app.state.db_session_factory
    fresh_session: Session = session_factory()
    fresh_session.info["request_state"] = request.state
    return fresh_session


router = APIRouter()
admin_router = APIRouter()

_prediction_owners: dict[str, str] = {}

RUNTIME_FACING_ASSISTANT_UPDATE_FIELDS = frozenset(
    {
        "first_name",
        "surname",
        "age",
        "nationality",
        "about",
        "timezone",
        "desktop_mode",
        "voice_id",
        "voice_provider",
        "default_model",
        "default_reasoning_effort",
        "slow_brain_model",
        "slow_brain_reasoning_effort",
    },
)


def _runtime_update_requires_reawaken(
    existing_assistant: Assistant,
    update_data: dict,
) -> bool:
    """Return True when a PATCH changes fields consumed by runtime startup/update."""
    for field_name in RUNTIME_FACING_ASSISTANT_UPDATE_FIELDS.intersection(update_data):
        if getattr(existing_assistant, field_name, None) != update_data[field_name]:
            return True
    return False


def _build_console_config_read(
    cfg: "AssistantConsoleConfig | None",
) -> "ConsoleConfigRead | None":
    """Convert an ORM ``AssistantConsoleConfig`` row to the API schema."""
    if cfg is None:
        return None
    layout: dict = {"mode": cfg.layout_mode}
    if cfg.layout_default_tab:
        layout["defaultTab"] = cfg.layout_default_tab
    tabs = None
    if cfg.tabs_hidden or cfg.tabs_order:
        tabs = {}
        if cfg.tabs_hidden:
            tabs["hidden"] = cfg.tabs_hidden
        if cfg.tabs_order:
            tabs["order"] = cfg.tabs_order
    theme = None
    if cfg.theme_brand_name or cfg.theme_accent_color:
        theme = {}
        if cfg.theme_brand_name:
            theme["brandName"] = cfg.theme_brand_name
        if cfg.theme_accent_color:
            theme["accentColor"] = cfg.theme_accent_color
    return ConsoleConfigRead(
        version=cfg.version,
        layout=layout,
        tabs=tabs,
        theme=theme,
    )


def _resolved_contact_ids_for_assistants(
    session: Session,
    assistant_ids: list[int],
    *,
    repair_missing_personal_overlays: bool = False,
) -> dict[int, ResolvedContactIds]:
    """Resolve assistant-self and boss contact ids for AssistantRead payloads."""

    if not assistant_ids:
        return {}

    def load_resolved_contact_ids() -> dict[int, dict[str, int]]:
        relationship_values = {
            CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
            CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS,
        }
        rows = (
            session.query(
                ContactMembership.id,
                ContactMembership.assistant_id,
                ContactMembership.contact_id,
                ContactMembership.relationship,
            )
            .filter(
                ContactMembership.assistant_id.in_(assistant_ids),
                ContactMembership.target_scope == CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
                ContactMembership.relationship.in_(relationship_values),
            )
            .order_by(
                ContactMembership.assistant_id,
                ContactMembership.relationship,
                ContactMembership.id,
            )
            .all()
        )

        resolved: dict[int, dict[str, int]] = {
            assistant_id: {} for assistant_id in assistant_ids
        }
        seen: set[tuple[int, str]] = set()
        for _, assistant_id, contact_id, relationship_name in rows:
            key = (assistant_id, relationship_name)
            if key in seen:
                continue
            seen.add(key)

            if relationship_name == CONTACT_MEMBERSHIP_RELATIONSHIP_SELF:
                resolved[assistant_id][
                    CONTACT_MEMBERSHIP_RELATIONSHIP_SELF
                ] = contact_id
            elif relationship_name == CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS:
                resolved[assistant_id][
                    CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS
                ] = contact_id
        return resolved

    def missing_required_ids(
        resolved_ids: dict[int, dict[str, int]],
    ) -> list[int]:
        return [
            assistant_id
            for assistant_id, contact_ids in resolved_ids.items()
            if CONTACT_MEMBERSHIP_RELATIONSHIP_SELF not in contact_ids
            or CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS not in contact_ids
        ]

    resolved = load_resolved_contact_ids()
    missing_assistant_ids = missing_required_ids(resolved)
    if missing_assistant_ids and repair_missing_personal_overlays:
        logging.warning(
            "Missing personal contact overlays for assistants; repairing: %s",
            missing_assistant_ids,
        )
        ensure_personal_contact_memberships(session, missing_assistant_ids)
        resolved = load_resolved_contact_ids()

    remaining_missing_assistant_ids = missing_required_ids(resolved)
    if remaining_missing_assistant_ids:
        logging.warning(
            "Missing personal contact overlays for assistants after repair; "
            "using fallback contact ids: %s",
            remaining_missing_assistant_ids,
        )

    return {
        assistant_id: ResolvedContactIds(
            self_contact_id=contact_ids.get(
                CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
                PERSONAL_SELF_CONTACT_ID,
            ),
            boss_contact_id=contact_ids.get(
                CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS,
                PERSONAL_BOSS_CONTACT_ID,
            ),
        )
        for assistant_id, contact_ids in resolved.items()
    }


def _resolved_contact_identity_roots_for_assistants(
    session: Session,
    assistant_ids: list[int],
    *,
    team_ids_by_assistant: dict[int, list[int]] | None = None,
    personal_ids_by_assistant: dict[int, ResolvedContactIds] | None = None,
) -> dict[int, list[AssistantContactIdentityRoot]]:
    """Resolve self/boss contact ids for every readable assistant root."""

    if not assistant_ids:
        return {}

    if team_ids_by_assistant is None:
        team_ids_by_assistant = TeamDAO(session).team_ids_for_assistants(
            assistant_ids,
        )

    if personal_ids_by_assistant is None:
        personal_ids_by_assistant = _resolved_contact_ids_for_assistants(
            session,
            assistant_ids,
        )
    roots_by_assistant: dict[int, list[AssistantContactIdentityRoot]] = {}
    for assistant_id in assistant_ids:
        personal_ids = personal_ids_by_assistant[assistant_id]
        roots_by_assistant[assistant_id] = [
            AssistantContactIdentityRoot(
                target_scope=CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
                target_team_id=None,
                self_contact_id=personal_ids.self_contact_id,
                boss_contact_id=personal_ids.boss_contact_id,
            ),
        ]

    active_team_ids_by_assistant = {
        assistant_id: set(team_ids_by_assistant.get(assistant_id, []))
        for assistant_id in assistant_ids
    }
    active_team_ids = {
        team_id
        for team_ids in active_team_ids_by_assistant.values()
        for team_id in team_ids
    }
    if not active_team_ids:
        return roots_by_assistant

    relationship_values = {
        CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
        CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS,
    }

    def _fetch_team_identity_rows(
        *,
        pairs: set[tuple[int, int]] | None = None,
    ) -> list[tuple[int, int, int | None, int, str]]:
        query = session.query(
            ContactMembership.id,
            ContactMembership.assistant_id,
            ContactMembership.target_team_id,
            ContactMembership.contact_id,
            ContactMembership.relationship,
        ).filter(
            ContactMembership.assistant_id.in_(assistant_ids),
            ContactMembership.target_scope == CONTACT_MEMBERSHIP_SCOPE_TEAM,
            ContactMembership.target_team_id.in_(active_team_ids),
            ContactMembership.relationship.in_(relationship_values),
        )
        if pairs:
            query = query.filter(
                tuple_(
                    ContactMembership.assistant_id,
                    ContactMembership.target_team_id,
                ).in_(sorted(pairs)),
            )
        return query.order_by(
            ContactMembership.assistant_id,
            ContactMembership.target_team_id,
            ContactMembership.relationship,
            ContactMembership.id,
        ).all()

    def _collect_ids_by_root(
        rows: list[tuple[int, int, int | None, int, str]],
    ) -> dict[tuple[int, int], dict[str, int]]:
        ids: dict[tuple[int, int], dict[str, int]] = {}
        seen: set[tuple[int, int, str]] = set()
        for _, assistant_id, target_team_id, contact_id, relationship_name in rows:
            if target_team_id is None:
                continue
            if target_team_id not in active_team_ids_by_assistant[assistant_id]:
                continue

            key = (assistant_id, target_team_id, relationship_name)
            if key in seen:
                continue
            seen.add(key)
            ids.setdefault((assistant_id, target_team_id), {})[
                relationship_name
            ] = contact_id
        return ids

    ids_by_root = _collect_ids_by_root(_fetch_team_identity_rows())
    missing_pairs = {
        (assistant_id, team_id)
        for assistant_id in assistant_ids
        for team_id in active_team_ids_by_assistant[assistant_id]
        if (
            CONTACT_MEMBERSHIP_RELATIONSHIP_SELF
            not in ids_by_root.get((assistant_id, team_id), {})
            or CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS
            not in ids_by_root.get((assistant_id, team_id), {})
        )
    }
    if missing_pairs:
        ensure_team_contact_memberships(session, sorted(missing_pairs))
        ids_by_root.update(
            _collect_ids_by_root(_fetch_team_identity_rows(pairs=missing_pairs)),
        )

    for assistant_id in assistant_ids:
        for team_id in sorted(active_team_ids_by_assistant[assistant_id]):
            contact_ids = ids_by_root.get((assistant_id, team_id), {})
            if (
                CONTACT_MEMBERSHIP_RELATIONSHIP_SELF not in contact_ids
                or CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS not in contact_ids
            ):
                logging.warning(
                    "Missing team contact identity for assistant %s in team %s",
                    assistant_id,
                    team_id,
                )
                continue

            roots_by_assistant[assistant_id].append(
                AssistantContactIdentityRoot(
                    target_scope=CONTACT_MEMBERSHIP_SCOPE_TEAM,
                    target_team_id=team_id,
                    self_contact_id=contact_ids[CONTACT_MEMBERSHIP_RELATIONSHIP_SELF],
                    boss_contact_id=contact_ids[CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS],
                ),
            )

    return roots_by_assistant


def _contact_id_pair(
    contact_ids_by_assistant: dict[int, ResolvedContactIds],
    assistant_id: int,
) -> ResolvedContactIds:
    """Return resolved contact ids for an assistant."""

    try:
        return contact_ids_by_assistant[assistant_id]
    except KeyError:
        logging.error(
            "Missing personal contact overlays for assistant %s",
            assistant_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="missing_contact_overlay",
        )


def _derive_workspace_provider(
    secrets: Optional[dict[str, str]],
) -> Optional[str]:
    """Resolve the OAuth-connected workspace provider from granted-scopes.

    Uses the same Google-first precedence as ``get_granted_features`` so the
    profile card and the workspace dialog can never disagree. Reflects the
    workspace the user OAuth-connected, which for a Coordinator is distinct
    from its platform mailbox tenant (``email_provider``).
    """
    if not secrets:
        return None
    if secrets.get("GOOGLE_GRANTED_SCOPES"):
        return "google"
    if secrets.get("MICROSOFT_GRANTED_SCOPES"):
        return "microsoft"
    return None


def _build_assistant_read(
    a: Assistant,
    session: Session,
    *,
    api_key: Optional[str] = None,
    user_first_name: Optional[str] = None,
    user_last_name: Optional[str] = None,
    user_email: Optional[str] = None,
    user_image: Optional[str] = None,
    user_whatsapp_number: Optional[str] = None,
    team_ids: Optional[List[int]] = None,
    team_summaries: Optional[list[dict[str, Any]]] = None,
    self_contact_id: Optional[int] = None,
    boss_contact_id: Optional[int] = None,
    contact_identity_roots: Optional[list[AssistantContactIdentityRoot]] = None,
    contacts: Optional[list] = None,
    secrets: Optional[dict] = None,
    workspace_secrets: Optional[dict] = None,
    resolve_workspace_secrets: bool = True,
    resolve_slack_install: bool = False,
    resolve_ms_teams_install: bool = False,
    include_internal: bool = False,
    requesting_user_id: Optional[str] = None,
) -> AssistantRead:
    """Build an ``AssistantRead`` from an ORM ``Assistant``.

    Contact fields (phone, email, whatsapp, etc.) are populated from the
    ``AssistantContact`` table.  User-side contact info (``user_phone``,
    ``user_whatsapp_number``) is sourced from the ``User`` profile.

    Args:
        user_whatsapp_number: When provided, used directly.  When ``None``
            the value is fetched from ``User.whatsapp_number``.
        contacts: Pre-fetched list of active ``AssistantContact`` rows for
            this assistant.  When ``None`` the contacts are fetched from
            the database.  Callers that build many ``AssistantRead``
            objects at once should batch-fetch contacts via
            ``AssistantContactDAO.get_active_contacts_for_assistants()`` and pass them in to
            avoid N+1 queries.
    """
    # Resolve the requesting user's *own* desktop linked to this assistant.
    # A shared assistant can be linked to a different machine per user, so the
    # read reflects whoever is asking (falling back to the owner for internal
    # callers that don't carry a requesting identity).
    desktop_dao = DesktopDAO(session)
    user_desktop_url = None
    user_desktop_mode = None
    user_desktop_filesys_sync = None
    link_row = desktop_dao.get_link_for_user(
        a.agent_id,
        requesting_user_id or a.user_id,
    )
    if link_row is not None:
        link, desktop = link_row
        user_desktop_url = desktop.url
        user_desktop_mode = desktop.os
        user_desktop_filesys_sync = link.filesys_sync

    # The full per-user desktop map is admin/runtime-only so members of a shared
    # assistant don't see each other's machine URLs.
    user_desktops: list[AssistantUserDesktopLink] = []
    user_desktop_filesync_keys: dict[str, str] = {}
    if include_internal:
        for link, desktop in desktop_dao.list_links_for_assistant(a.agent_id):
            user_desktops.append(
                AssistantUserDesktopLink(
                    owner_user_id=link.owner_user_id,
                    url=desktop.url,
                    os=desktop.os,
                    filesys_sync=link.filesys_sync,
                    sftp_tunnel_host=link.sftp_tunnel_host,
                    sftp_tunnel_port=link.sftp_tunnel_port,
                ),
            )
            # Private keys ride a separate admin/runtime-only field so they
            # never leak into the assistant pod env via user_desktops.
            if link.filesync_sshkey:
                user_desktop_filesync_keys[link.owner_user_id] = link.filesync_sshkey

    team_dao = TeamDAO(session)
    if team_ids is None:
        team_ids = team_dao.team_ids_for_assistant(a.agent_id)
    if team_summaries is None:
        team_summaries = team_dao.team_summaries_for_assistant(a.agent_id)
    if self_contact_id is None or boss_contact_id is None:
        resolved_contact_ids = _resolved_contact_ids_for_assistants(
            session,
            [a.agent_id],
        )[a.agent_id]
        if self_contact_id is None:
            self_contact_id = resolved_contact_ids.self_contact_id
        if boss_contact_id is None:
            boss_contact_id = resolved_contact_ids.boss_contact_id

    if contact_identity_roots is None:
        contact_identity_roots = _resolved_contact_identity_roots_for_assistants(
            session,
            [a.agent_id],
            team_ids_by_assistant={a.agent_id: team_ids},
            personal_ids_by_assistant={
                a.agent_id: ResolvedContactIds(
                    self_contact_id=self_contact_id,
                    boss_contact_id=boss_contact_id,
                ),
            },
        )[a.agent_id]

    # Resolve contact fields from AssistantContact rows
    if contacts is None:
        contact_dao = AssistantContactDAO(session)
        contacts = contact_dao.get_active_contacts_for_assistant(a.agent_id)

    contact_map: dict[str, object] = {}
    for c in contacts:
        contact_map[c.contact_type] = c

    phone_contact = contact_map.get("phone")
    email_contact = contact_map.get("email")
    whatsapp_contact = contact_map.get("whatsapp")
    discord_contact = contact_map.get("discord")

    # User-side contact info comes from the User profile
    user_obj = session.get(User, a.user_id)
    user_phone_number = user_obj.phone_number if user_obj else None
    if user_whatsapp_number is None:
        user_whatsapp_number = user_obj.whatsapp_number if user_obj else None
    user_discord_id = user_obj.discord_id if user_obj else None

    # The connected-workspace provider is derived from the granted-scopes
    # secrets. Precedence for the source dict:
    #   1. ``workspace_secrets`` — a caller-batched two-key map, used by list
    #      endpoints that do NOT expose the full ``secrets`` field (avoids both
    #      an N+1 and leaking scope strings into the response).
    #   2. ``secrets`` — the full dict, when the caller already batched it and
    #      exposes it (admin list path).
    #   3. A targeted two-key DAO lookup for single-assistant reads (one
    #      assistant — negligible).
    # Narrow-field list callers that deliberately skip secrets pass
    # ``resolve_workspace_secrets=False`` to suppress the fallback for a field
    # they didn't request.
    ws_source = workspace_secrets if workspace_secrets is not None else secrets
    if ws_source is None and resolve_workspace_secrets:
        _secret_dao = AssistantSecretDAO(session)
        ws_source = {
            "GOOGLE_GRANTED_SCOPES": _secret_dao.get(
                a.agent_id,
                "GOOGLE_GRANTED_SCOPES",
            ),
            "MICROSOFT_GRANTED_SCOPES": _secret_dao.get(
                a.agent_id,
                "MICROSOFT_GRANTED_SCOPES",
            ),
        }
    workspace_provider = _derive_workspace_provider(ws_source)

    # Slack's ``bot_user_id`` is workspace-scoped — it lives on the owner's
    # ``slack_installs`` row, not on any per-assistant contact — so unlike
    # Discord it is never surfaced by the contact map. Resolve it from the
    # active install for the assistant's owner (org first, else personal user)
    # so the runtime can send outbound Slack before any inbound Slack event.
    # Gated to the runtime bootstrap read path to avoid a per-assistant query
    # on Console list endpoints that don't need it.
    assistant_slack_bot_user_id: Optional[str] = None
    assistant_slack_team_id: Optional[str] = None
    if resolve_slack_install:
        slack_dao = SlackDAO(session)
        install = (
            slack_dao.get_install_for_org(a.organization_id)
            if a.organization_id is not None
            else slack_dao.get_install_for_user(a.user_id)
        )
        assistant_slack_bot_user_id = install.bot_user_id if install else None
        assistant_slack_team_id = install.slack_team_id if install else None

    # The Teams bot is an org-installed app, so — like Slack's bot_user_id —
    # its existence lives on the owner's install row rather than on any
    # per-assistant contact. Resolving it here is what lets a headless task
    # know the channel exists at all; without it the capability is only ever
    # discovered by receiving an inbound Teams activity, which a scheduled
    # run never does. Gated to the runtime bootstrap read path.
    assistant_has_ms_teams_bot: Optional[bool] = None
    assistant_ms_teams_tenant_id: Optional[str] = None
    if resolve_ms_teams_install:
        ms_teams_install = MsTeamsBotDAO(session).get_install_for_owner(
            a.organization_id,
            a.user_id,
        )
        assistant_has_ms_teams_bot = ms_teams_install is not None
        assistant_ms_teams_tenant_id = (
            ms_teams_install.tenant_id if ms_teams_install else None
        )

    return AssistantRead(
        agent_id=str(a.agent_id),
        user_id=a.user_id,
        organization_id=a.organization_id,
        owner_team_id=a.owner_team_id,
        first_name=a.first_name,
        surname=a.surname,
        job_title=a.job_title,
        age=a.age,
        nationality=a.nationality,
        profile_photo=a.profile_photo,
        profile_video=a.profile_video,
        desktop_mode=a.desktop_mode,
        user_desktop_filesys_sync=user_desktop_filesys_sync,
        user_desktop_url=user_desktop_url,
        user_desktop_mode=user_desktop_mode,
        user_desktops=user_desktops,
        user_desktop_filesync_keys=user_desktop_filesync_keys,
        about=a.about,
        phone_country=(phone_contact.country_code if phone_contact else None),
        weekly_limit=(float(a.weekly_limit) if a.weekly_limit is not None else None),
        max_parallel=a.max_parallel,
        created_at=a.created_at,
        updated_at=a.updated_at,
        phone=(phone_contact.contact_value if phone_contact else None),
        email=(email_contact.contact_value if email_contact else None),
        email_provider=(email_contact.provider if email_contact else None),
        workspace_provider=workspace_provider,
        user_phone=user_phone_number,
        user_whatsapp_number=user_whatsapp_number,
        assistant_whatsapp_number=(
            whatsapp_contact.contact_value if whatsapp_contact else None
        ),
        user_discord_id=user_discord_id,
        assistant_discord_bot_id=(
            discord_contact.contact_value if discord_contact else None
        ),
        assistant_slack_bot_user_id=assistant_slack_bot_user_id,
        assistant_slack_team_id=assistant_slack_team_id,
        assistant_has_ms_teams_bot=assistant_has_ms_teams_bot,
        assistant_ms_teams_tenant_id=assistant_ms_teams_tenant_id,
        voice_id=a.voice_id,
        voice_provider=a.voice_provider,
        default_model=a.default_model,
        default_reasoning_effort=a.default_reasoning_effort,
        slow_brain_model=a.slow_brain_model,
        slow_brain_reasoning_effort=a.slow_brain_reasoning_effort,
        timezone=a.timezone,
        is_local=a.is_local,
        is_coordinator=a.is_coordinator,
        is_multiplayer=a.is_multiplayer,
        monthly_spending_cap=(
            float(a.monthly_spending_cap)
            if a.monthly_spending_cap is not None
            else None
        ),
        managed_desktop_status=a.managed_desktop_status,
        managed_desktop_monthly_cost=(
            float(a.managed_desktop_monthly_cost)
            if a.managed_desktop_monthly_cost is not None
            else None
        ),
        desktop_filesync_sshkey=(
            a.desktop_filesync_sshkey if include_internal else None
        ),
        api_key=api_key,
        user_first_name=user_first_name,
        user_last_name=user_last_name,
        user_email=user_email,
        user_image=user_image,
        team_ids=team_ids,
        team_summaries=team_summaries,
        self_contact_id=self_contact_id,
        boss_contact_id=boss_contact_id,
        contact_identity_roots=contact_identity_roots,
        secrets=secrets,
        console_config=_build_console_config_read(a.console_config),
    )


def _is_hidden_workspace_coordinator_for_user(
    assistant: Assistant,
    *,
    user_id: str,
) -> bool:
    """Return whether an org Coordinator row is hidden from this user.

    Only single-player coordinators are private to their owner; a
    multiplayer twin is a visible colleague like any hired teammate.
    """
    return (
        assistant.is_private_coordinator
        and assistant.organization_id is not None
        and assistant.user_id != user_id
    )


def _self_heal_coordinator_contacts(
    session: Session,
    *,
    coordinators: List[Assistant],
    contacts_by_assistant: dict[int, list],
    contact_dao: AssistantContactDAO,
) -> bool:
    """Backfill and reconcile universal contacts for Coordinators on read.

    Coordinator contacts (email / phone / WhatsApp / Discord) are
    platform-managed pools provisioned at Coordinator creation. Coordinators
    that predate that rollout (or a newly added channel) otherwise only get them
    via the onboarding provisioning call, so a long-lived Coordinator can show
    missing contacts indefinitely. Likewise, when a pool identifier is repointed
    in settings (e.g. the shared Coordinator email moves to a new address), the
    value stored on existing Coordinators would otherwise stay stale until the
    next provisioning call. Healing here, on the natural read path, closes both
    gaps without a console round-trip.

    ``coordinators`` must already be filtered to rows the requesting user owns
    and is authorized to provision (callers do their own ownership/permission
    checks). For each Coordinator we heal channels that are configured for this
    deployment but either *missing* on the Coordinator or *drifted* from the
    configured value; channels that aren't set up and non-universal (manually
    set) contacts are left untouched. ``contacts_by_assistant`` is refreshed in
    place so the freshly provisioned/reconciled contacts surface in this same
    response.

    Best-effort: any failure is swallowed (the next read retries) and the
    session is left usable for the rest of the response build.

    Returns ``True`` when Unity should be pinged to (re)sync the shared Discord
    bot pool — i.e. this read created the pool row, reactivated it, or rotated
    its bot token. Discord is the only channel that needs an out-of-band sync;
    Unity resolves email/phone/WhatsApp routing per message.
    """
    if not coordinators:
        return False

    # Reconcile the shared Discord bot pool (id + token) once per pass. The pool
    # is platform-global (one row, not per-Coordinator), so it lives outside the
    # loop. ``changed`` captures creation, reactivation, and token rotation —
    # every case where Unity must re-pull the bot credentials. Token rotations
    # don't surface as a Coordinator contact drift (the contact stores the bot
    # *id*, which is unchanged), so this is the only place they get healed.
    discord_pool_changed = False
    if get_universal_unity_discord_bot_id():
        try:
            _discord_pool, discord_pool_changed = ensure_universal_unity_discord_pool(
                session,
            )
        except Exception:
            logging.warning(
                "Coordinator Discord pool self-heal failed",
                exc_info=True,
            )

    healed_ids: list[int] = []
    for coordinator in coordinators:
        # Capture the id up front: a failed heal flush expires the ORM instance,
        # so reading ``coordinator.agent_id`` afterwards would itself emit a
        # query against the now-poisoned session and re-raise.
        coordinator_id = coordinator.agent_id
        present_contacts = contacts_by_assistant.get(coordinator_id, [])
        present_types = [c.contact_type for c in present_contacts]
        missing = missing_universal_coordinator_contact_types(present_types)
        drifted = drifted_universal_coordinator_contact_types(present_contacts)
        to_heal = [
            contact_type
            for contact_type in UNIVERSAL_CONTACT_TYPES
            if contact_type in set(missing) | set(drifted)
        ]
        if not to_heal:
            continue
        try:
            heal_coordinator_universal_contacts(
                session,
                coordinator=coordinator,
                contact_types=to_heal,
            )
        except Exception:
            # Best-effort: roll back so the failed flush doesn't poison the
            # session for the rest of the response build (the next read retries).
            session.rollback()
            logging.warning(
                "Coordinator contact self-heal failed for %s",
                coordinator_id,
                exc_info=True,
            )
            continue
        healed_ids.append(coordinator_id)

    if not healed_ids and not discord_pool_changed:
        return False

    try:
        session.commit()
    except Exception:
        session.rollback()
        logging.warning(
            "Coordinator contact self-heal commit failed",
            exc_info=True,
        )
        return False

    # Surface the freshly provisioned contacts in this same response.
    if healed_ids:
        refreshed = contact_dao.get_active_contacts_for_assistants(healed_ids)
        for healed_id in healed_ids:
            contacts_by_assistant[healed_id] = []
        for contact in refreshed:
            contacts_by_assistant.setdefault(contact.assistant_id, []).append(contact)

    return discord_pool_changed


@router.post(
    "/assistant",
    response_model=InfoResponse[AssistantRead],
    status_code=status.HTTP_200_OK,
    summary="Create a new assistant",
    description="Creates a new assistant for the authenticated user with the specified configuration.",
    tags=["Assistant Management"],
    dependencies=[Depends(require_console_origin_for_free_accounts)],
    responses={
        200: {
            "description": "Assistant created successfully",
            "content": {
                "application/json": {
                    "example": {
                        "info": {
                            "agent_id": "123",
                            "first_name": "Alice",
                            "surname": "Smith",
                            "age": 25,
                            "weekly_limit": 40.0,
                            "max_parallel": 3,
                            "created_at": "2025-04-25T12:00:00Z",
                            "updated_at": "2025-04-25T12:00:00Z",
                            "phone": "+1-555-123-4567",
                            "email": "alice.smith@example.com",
                            "voice_id": "bf0a246a-8642-498a-9950-80c35e9276b5",
                            "phone_country": "US",
                        },
                    },
                },
            },
        },
        402: {
            "description": "Insufficient credits",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Insufficient credits to create an assistant.",
                    },
                },
            },
        },
        409: {
            "description": "Assistant already exists for this scope and name key.",
            "content": {
                "application/json": {
                    "example": {
                        "detail": {
                            "error": "assistant_already_exists",
                            "message": "Assistant with this name already exists in this scope.",
                            "existing_id": 123,
                        },
                    },
                },
            },
        },
        422: {
            "description": "Validation Error",
            "content": {
                "application/json": {
                    "example": {
                        "detail": [
                            {
                                "loc": ["body", "first_name"],
                                "msg": "field required",
                                "type": "value_error.missing",
                            },
                        ],
                    },
                },
            },
        },
    },
)
async def create_assistant(
    assistant_in: AssistantCreate,
    request: Request,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_db_session),
) -> InfoResponse[AssistantRead]:
    """
    Create a new assistant for the authenticated user.

    This endpoint allows users to create a personalized assistant with specific
    attributes like name, age, and operational limits. Each assistant is tied
    to the authenticated user's account. When called with an organization API
    key, the assistant lives inside that organization but still records the
    caller as its creator/lifecycle owner.
    """
    user_id = request.state.user_id
    user_dao = UserDAO(session)
    assistant_dao = AssistantDAO(session)
    api_key_dao = ApiKeyDAO(session)
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    log_event_dao = LogEventDAO(session, context_dao)
    api_keys = api_key_dao.filter(user_id=user_id)
    if not api_keys:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized. Please contact support to get an API key.",
        )
    assistant = None

    # Base creation cost (contact provisioning costs are handled separately
    # via the dedicated POST /assistant/{id}/contact endpoint).
    total_creation_cost = settings.assistant_creation_cost
    managed_desktop_upfront = Decimal("0")
    if assistant_in.desktop_mode in MANAGED_DESKTOP_MODES:
        managed_desktop_upfront = get_managed_desktop_monthly_cost(
            session,
            assistant_in.desktop_mode,
        )

    # Phase 1: Pre-checks and prepare assistant data
    try:
        # Get organization context from API key (None = personal, int = org)
        organization_id = getattr(request.state, "organization_id", None)
        resource_access_dao = ResourceAccessDAO(session)
        role_dao = RoleDAO(session)

        # For org context, check assistant:write permission
        if organization_id is not None:
            has_permission = resource_access_dao.check_org_member_permission(
                user_id,
                organization_id,
                "assistant:write",
            )
            if not has_permission:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You do not have permission to create assistants in this organization.",
                )

        # Team-owned assistants require an org key and a team in that org.
        owner_team = None
        if assistant_in.owner_team_id is not None:
            if organization_id is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        "owner_team_id requires an organization API key; "
                        "team-owned assistants live inside an organization."
                    ),
                )
            owner_team = session.get(Team, assistant_in.owner_team_id)
            if owner_team is None or owner_team.organization_id != organization_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=(
                        f"Team {assistant_in.owner_team_id} not found in "
                        "this organization."
                    ),
                )

        if settings.charges_billing and (
            total_creation_cost > 0 or managed_desktop_upfront > 0
        ):
            try:
                billing_entity = get_billing_entity(session, user_id, organization_id)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail="Billing is not set up. Please add a payment method first.",
                )
            if not billing_entity.has_sufficient_credits(
                Decimal(str(total_creation_cost)) + managed_desktop_upfront,
            ):
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail="Insufficient credits to create an assistant.",
                )

        if is_reserved_coordinator_name(assistant_in.first_name):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "coordinator_name_is_reserved",
                    "message": (
                        "That first name is reserved for the workspace "
                        "coordinator. Pick a name of its own."
                    ),
                },
            )

        existing_assistant = assistant_dao.find_by_natural_key(
            user_id=user_id,
            organization_id=organization_id,
            first_name=assistant_in.first_name,
            surname=assistant_in.surname,
        )
        if existing_assistant is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "assistant_already_exists",
                    "message": (
                        "Assistant with this name already exists in this scope."
                    ),
                    "existing_id": existing_assistant.agent_id,
                    "first_name": assistant_in.first_name,
                    "surname": assistant_in.surname,
                    "organization_id": organization_id,
                },
            )

        parsed_weekly_limit = (
            Decimal(assistant_in.weekly_limit)
            if assistant_in.weekly_limit is not None
            else None
        )

        assistant = assistant_dao.create_assistant(
            user_id=user_id,
            first_name=assistant_in.first_name,
            surname=assistant_in.surname,
            age=assistant_in.age,
            nationality=assistant_in.nationality,
            profile_photo=assistant_in.profile_photo,
            profile_video=assistant_in.profile_video,
            desktop_mode=assistant_in.desktop_mode,
            about=assistant_in.about,
            weekly_limit=parsed_weekly_limit,
            max_parallel=assistant_in.max_parallel,
            voice_id=assistant_in.voice_id,
            voice_provider=assistant_in.voice_provider,
            default_model=assistant_in.default_model,
            default_reasoning_effort=assistant_in.default_reasoning_effort,
            slow_brain_model=assistant_in.slow_brain_model,
            slow_brain_reasoning_effort=assistant_in.slow_brain_reasoning_effort,
            timezone=assistant_in.timezone,
            organization_id=organization_id,
            owner_team_id=assistant_in.owner_team_id,
            is_local=assistant_in.is_local or False,
            job_title=assistant_in.job_title,
        )
        if assistant_in.owner_team_id is None:
            # Team-owned assistants have no personal root: their contact
            # overlays are the owning team's (created via team enrollment
            # below), never personal self/boss rows.
            ensure_personal_contact_memberships(
                session,
                [assistant.agent_id],
                repair_existing=False,
            )

        # Org assistants retain the creator in `user_id`; org access is granted
        # separately through resource access so other members can collaborate.
        if organization_id is not None:
            owner_role = role_dao.get_by_name("Owner", organization_id=None)
            if owner_role:
                resource_access_dao.grant_access(
                    resource_type="assistant",
                    resource_id=assistant.agent_id,
                    role_id=owner_role.id,
                    grantee_type="user",
                    grantee_id=user_id,
                )

        # Create "Assistants" project if it doesn't exist (for logging purposes)
        ASSISTANTS_PROJECT_NAME = "Assistants"
        assistants_project: Project | None

        if organization_id is not None:
            # For org context, check if project exists in org (not user-access based)
            org_projects = project_dao.filter(
                organization_id=organization_id,
                name=ASSISTANTS_PROJECT_NAME,
            )
            assistants_project = org_projects[0][0] if org_projects else None

            if not assistants_project:
                # Create org Assistants project
                project_dao.create(
                    user_id=None,
                    organization_id=organization_id,
                    name=ASSISTANTS_PROJECT_NAME,
                    description="Project to manage and track all organization assistants.",
                    is_versioned=False,
                )
                session.flush()

                # Fetch the created project
                org_projects = project_dao.filter(
                    organization_id=organization_id,
                    name=ASSISTANTS_PROJECT_NAME,
                )
                assistants_project = org_projects[0][0] if org_projects else None

                # Grant Owner role to creator
                if assistants_project:
                    owner_role = role_dao.get_by_name("Owner", organization_id=None)
                    if owner_role:
                        resource_access_dao.grant_access(
                            resource_type="project",
                            resource_id=assistants_project.id,
                            role_id=owner_role.id,
                            grantee_type="user",
                            grantee_id=user_id,
                        )

                    # Grant Member access to all other existing org members
                    org_members = organization_member_dao.filter(
                        organization_id=organization_id,
                    )
                    member_role = role_dao.get_by_name("Member", organization_id=None)
                    if member_role:
                        for member_row in org_members:
                            member = member_row[0]
                            if member.user_id != user_id:
                                resource_access_dao.grant_access(
                                    resource_type="project",
                                    resource_id=assistants_project.id,
                                    role_id=member_role.id,
                                    grantee_type="user",
                                    grantee_id=member.user_id,
                                )
            else:
                # Project exists - check if user already has access
                has_access = resource_access_dao.check_user_permission(
                    user_id,
                    "project",
                    assistants_project.id,
                    "project:read",
                )
                if not has_access:
                    # Grant Member role to user
                    member_role = role_dao.get_by_name("Member", organization_id=None)
                    if member_role:
                        resource_access_dao.grant_access(
                            resource_type="project",
                            resource_id=assistants_project.id,
                            role_id=member_role.id,
                            grantee_type="user",
                            grantee_id=user_id,
                        )
        else:
            # Personal API key - check user access
            assistants_project = project_dao.get_by_user_and_name(
                user_id=user_id,
                name=ASSISTANTS_PROJECT_NAME,
                organization_id=None,
            )
            if not assistants_project:
                # Create personal Assistants project
                project_dao.create(
                    user_id=user_id,
                    organization_id=None,
                    name=ASSISTANTS_PROJECT_NAME,
                    description="Project to manage and track all your assistants.",
                    is_versioned=False,
                )
                session.flush()
                assistants_project = project_dao.get_by_user_and_name(
                    user_id=user_id,
                    name=ASSISTANTS_PROJECT_NAME,
                    organization_id=None,
                )

        if assistants_project is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="assistants_project_missing",
            )
        if owner_team is None:
            # Team-owned assistants never get a personal `{user}/{agent}`
            # Contacts context; their contact surface is the owning team's.
            ensure_owner_contact_row(
                session,
                assistant=assistant,
                project=assistants_project,
            )

        sharing_refresh_payloads = []
        if owner_team is not None:
            owning_result = add_assistant_to_team(
                session,
                team=owner_team,
                assistant=assistant,
                actor_user_id=user_id,
            )
            sharing_refresh_payloads.extend(owning_result.refresh_payloads)
        if organization_id is not None and not assistant.is_coordinator:
            org = session.get(Organization, organization_id)
            if org is not None and org.org_wide_sharing_enabled:
                sharing_result = enroll_assistant_in_org_wide_team(
                    session,
                    org=org,
                    assistant=assistant,
                    actor_user_id=user_id,
                )
                sharing_refresh_payloads.extend(sharing_result.refresh_payloads)

        if assistant_in.desktop_mode in MANAGED_DESKTOP_MODES:
            billing_entity = get_billing_entity(session, user_id, organization_id)
            charge_managed_desktop_first_month(
                session,
                assistant=assistant,
                desktop_mode=assistant_in.desktop_mode,
                billing_entity=billing_entity,
                user_id=user_id,
                organization_id=organization_id,
            )
            ensure_pending_assistant_external_ip(session, assistant=assistant)

        # Commit the assistant creation before infrastructure setup
        # This ensures the assistant persists even if we refresh the session later
        session.commit()
        await publish_membership_refreshes_best_effort(sharing_refresh_payloads)

        assistant_id = assistant.agent_id
        # Infrastructure creation with rollback on failure
        # NOTE: Contact provisioning (phone, email, WhatsApp) is now handled
        # exclusively via the dedicated POST /assistant/{id}/contact endpoint.
        created_pubsub = None

        if assistant_in.create_infra:
            current_infra_step = "initializing"
            try:
                # Step 1: create pubsub topic
                current_infra_step = "create_pubsub_topic"
                pubsub_response = await create_pubsub_topic(
                    str(assistant_id),
                )
                if "detail" in pubsub_response:
                    raise Exception(
                        f"Pubsub topic creation failed: {pubsub_response['detail']}",
                    )
                created_pubsub = True
                print(f"PUBSUB CREATED: {assistant_id}")

                # Refresh database session after long infrastructure operations
                logging.info(
                    f"Refreshing database session after infrastructure setup for assistant {assistant_id}",
                )
                session.close()
                session = next(get_db_session(request))
                assistant_dao = AssistantDAO(session)

                # Commit the infrastructure updates
                session.commit()
                print(f"ASSISTANT UPDATED: {assistant_id}")

                # Retrieve the updated assistant for the final response
                assistant = assistant_dao.get_assistant_by_id(
                    user_id=user_id,
                    agent_id=assistant_id,
                    organization_id=organization_id,
                )

            except Exception as infra_error:
                # Use repr() to always show exception type, even if str() is empty
                print(
                    f"INFRA ERROR at step '{current_infra_step}': "
                    f"{type(infra_error).__name__}: {infra_error!r}",
                )

                # can't rollback infra if the setup isn't complete so need to wait
                time.sleep(10)

                # Refresh database session to avoid stale connections during rollback
                logging.warning(
                    f"Infrastructure setup failed for assistant {assistant_id}, refreshing session for rollback",
                )
                session.close()
                session = next(get_db_session(request))
                assistant_dao = AssistantDAO(session)
                context_dao = ContextDAO(session)
                project_dao = ProjectDAO(
                    session,
                    organization_member_dao,
                    context_dao,
                )

                # Rollback infrastructure in reverse order
                rollback_errors = []

                if created_pubsub:
                    result = await delete_pubsub_topic(
                        str(assistant_id),
                    )
                    if not result.get("success"):
                        rollback_errors.append(
                            "Failed to delete pubsub topic: "
                            f"{result.get('error') or result.get('reason') or 'cleanup incomplete'}",
                        )
                print(f"PUBSUB DELETED: {assistant_id}")

                # Delete the assistant record since infrastructure failed
                try:
                    # First, delete the chat context if it was created
                    if assistant_in.pre_hire_chat:
                        try:
                            context_name = f"{user_id}/{assistant_id}/Transcripts"
                            assistants_project = project_dao.get_by_user_and_name(
                                user_id=user_id,
                                name="Assistants",
                                organization_id=None,
                            )
                            if assistants_project:
                                context_to_delete = context_dao.filter(
                                    project_id=assistants_project.id,
                                    name=context_name,
                                )
                                if context_to_delete:
                                    context_dao.delete(context_to_delete[0][0].id)
                                    logging.info(
                                        f"Deleted chat transcript context for failed assistant {assistant_id}",
                                    )
                        except Exception as e_ctx_del:
                            rollback_errors.append(
                                f"Failed to delete chat context: {str(e_ctx_del)}",
                            )
                    assistant_dao.delete_assistant(
                        user_id=user_id,
                        agent_id=assistant_id,
                    )
                    # Commit the assistant deletion
                    session.commit()
                except Exception as e:
                    rollback_errors.append(f"Failed to delete assistant: {str(e)}")
                print(f"ASSISTANT DELETED: {assistant_id}")

                error_msg = f"Infrastructure setup failed: {infra_error}"
                if rollback_errors:
                    error_msg += f" Rollback issues: {'; '.join(rollback_errors)}"
                logging.error(error_msg, exc_info=True)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Infrastructure setup failed",
                )

    except IntegrityError as e:
        session.rollback()
        logging.error(f"Database error creating assistant: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Database error creating assistant",
        )
    except HTTPException:
        raise
    except Exception as e_prepare:
        logging.error(f"Failed to create assistant: {e_prepare}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to create assistant",
        )

    # Phase 2: Deduct credits from the correct billing account (user or org)
    # when a creation cost is configured.
    if settings.charges_billing and total_creation_cost > 0:
        try:
            from orchestra.db.dao.billing_account_dao import BillingAccountDAO

            billing_entity = get_billing_entity(session, user_id, organization_id)
            BillingAccountDAO(session).deduct_credits(
                billing_entity.billing_account_id,
                float(total_creation_cost),
                category="hire",
                assistant_id=assistant.agent_id if assistant else None,
                user_id=user_id,
                organization_id=organization_id,
                description="Assistant creation",
                detail={
                    "event": "assistant_creation",
                    "assistant_id": assistant.agent_id if assistant else None,
                },
            )
            session.commit()
        except Exception as e_commit:
            logging.error(
                f"Payment processing failed for assistant creation: {e_commit}",
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Payment processing failed",
            )

    if assistant is None:
        # Should ideally not be reached if Phase 1 fails
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create assistant.",
        )

    # Phase 3: Wake up assistant (skip for local assistants -- unity runs locally)
    if assistant_in.is_local:
        print(f"SKIPPED WAKEUP (local assistant): {assistant.agent_id}")
    elif settings.is_self_host or not comms_explicitly_configured():
        # No adapters/comms backend to talk to (self-host or local/CI stub
        # stack). Runtime convergence happens elsewhere; onboarding must not
        # hard-fail just because there is nothing to wake up.
        print(f"SKIPPED WAKEUP (no comms backend configured): {assistant.agent_id}")
    else:
        response = await wake_up_assistant(
            assistant.agent_id,
        )
        if response.status_code != 200:
            logging.error(f"Failed to wake up assistant: {response.text}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to wake up assistant.",
            )
        else:
            print(f"ASSISTANT AWAKENED: {assistant.agent_id}")

    # (Optional) Log pre-hire chat if provided
    if assistant_in.pre_hire_chat:
        try:
            # Convert Pydantic models to dictionaries for the webhook payload
            chat_messages = jsonable_encoder(assistant_in.pre_hire_chat)
            await log_pre_hire_chat(
                assistant_id=str(assistant.agent_id),
                messages=chat_messages,
            )
        except Exception as e_log:
            # We don't rollback the whole assistant creation for a logging failure,
            # but we should log it as a warning.
            logging.warning(
                f"Failed to log pre-hire chat for assistant {assistant.agent_id} via webhook. Error: {str(e_log)}",
            )

    # No onboarding narration on specialist hire: the console
    # immediately swaps the active assistant to the freshly-hired
    # specialist (which ends onboarding mode on the Coordinator), so
    # any acknowledgement from the Coordinator would land in a chat
    # the user has already moved away from.

    # Phase 4: Prepare and return response
    if assistant_in.desktop_mode in MANAGED_DESKTOP_MODES:
        background_tasks.add_task(
            reconcile_assistant_external_ip,
            request.app.state.db_session_factory,
            assistant_id=assistant.agent_id,
        )
    return InfoResponse(
        info=_build_assistant_read(assistant, session),
    )


@router.post(
    "/assistant/{coordinator_id}/transcript-seed",
    response_model=InfoResponse[CoordinatorTranscriptSeedResponse],
    status_code=status.HTTP_200_OK,
    summary="Seed a Coordinator transcript opener",
    tags=["Assistant Management"],
)
async def seed_coordinator_transcript_endpoint(
    coordinator_id: int,
    seed: CoordinatorTranscriptSeed,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[CoordinatorTranscriptSeedResponse]:
    """Persist the Coordinator opener transcript once."""
    coordinator = require_authorized_coordinator(
        session,
        coordinator_id=coordinator_id,
        user_id=request.state.user_id,
    )
    log_event_id = seed_coordinator_transcript(
        session,
        coordinator=coordinator,
        content=seed.content,
        source_assistant_id=seed.source_assistant_id,
    )
    session.commit()
    return InfoResponse(
        info=CoordinatorTranscriptSeedResponse(log_event_id=log_event_id),
    )


@router.post(
    "/assistant/{coordinator_id}/reset",
    response_model=InfoResponse[CoordinatorResetResponse],
    status_code=status.HTTP_200_OK,
    summary="Reset Coordinator-owned state",
    tags=["Assistant Management"],
)
async def reset_coordinator_endpoint(
    coordinator_id: int,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[CoordinatorResetResponse]:
    """Clear Coordinator-owned state, transcripts, and exchange contexts."""
    coordinator = require_authorized_coordinator(
        session,
        coordinator_id=coordinator_id,
        user_id=request.state.user_id,
    )
    reset_coordinator_state(session, coordinator=coordinator)
    session.commit()
    return InfoResponse(
        info=CoordinatorResetResponse(coordinator_id=str(coordinator.agent_id)),
    )


@router.post(
    "/assistant/{coordinator_id}/multiplayer",
    response_model=InfoResponse[AssistantRead],
    status_code=status.HTTP_200_OK,
    summary="Flip a Coordinator to multiplayer mode (one-way)",
    tags=["Assistant Management"],
    responses={
        409: {"description": "The coordinator is already multiplayer."},
        422: {"description": "Identity requirements not met (name/voice)."},
    },
)
async def flip_coordinator_multiplayer_endpoint(
    coordinator_id: int,
    flip: CoordinatorMultiplayerFlip,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[AssistantRead]:
    """Trade the twin's private surface for a hire-like outward identity.

    Applies the owned name/voice/avatar, retires every shared-pool contact,
    and provisions the dedicated alias email in one transaction, then
    reawakens the runtime so it comes back up in multiplayer mode.
    """
    coordinator = require_authorized_coordinator(
        session,
        coordinator_id=coordinator_id,
        user_id=request.state.user_id,
    )
    try:
        flip_coordinator_to_multiplayer(
            session,
            coordinator=coordinator,
            first_name=flip.first_name,
            surname=flip.surname,
            voice_id=flip.voice_id,
            voice_provider=flip.voice_provider,
            profile_photo=flip.profile_photo,
        )
    except MultiplayerFlipError as e:
        already = coordinator.is_multiplayer
        raise HTTPException(
            status_code=(
                status.HTTP_409_CONFLICT
                if already
                else status.HTTP_422_UNPROCESSABLE_ENTITY
            ),
            detail=str(e),
        )
    session.commit()

    from orchestra.web.api.utils.assistant_infra import reawaken_assistant

    alias_contact = AssistantContactDAO(session).get_contact_by_assistant_and_type(
        coordinator.agent_id,
        "email",
    )
    wake_reasons = [
        {
            "type": "coordinator_multiplayer_flipped",
            "alias_email": alias_contact.contact_value if alias_contact else "",
        },
    ]
    try:
        await reawaken_assistant(
            str(coordinator.agent_id),
            data={
                "assistant_id": str(coordinator.agent_id),
                "wake_reasons": json.dumps(wake_reasons),
            },
        )
    except Exception as e:
        logger.warning(
            "Failed to reawaken coordinator %s after multiplayer flip: %s",
            coordinator.agent_id,
            e,
        )
    return InfoResponse(info=_build_assistant_read(coordinator, session))


def _coordinator_state_response(
    session: Session,
    *,
    coordinator: Assistant,
) -> CoordinatorStateResponse:
    """Compose the state snapshot plus derived onboarding progress.

    ``completed_step_ids`` is re-derived from durable domain state on
    every read (see ``derive_onboarding_progress``) so the console
    checklist and Unity's openers agree on what is already done even
    when the completing action happened in an earlier session. The
    derivation runs exactly once per read — the render reuses the same
    state row and derived progress — and is skipped entirely when
    onboarding is inactive, where the checklist no longer renders.
    """
    state = get_coordinator_state(session, coordinator=coordinator)
    actively_onboarding = bool(state.get("onboarding_active"))
    completed_step_ids = (
        derive_onboarding_progress(session, coordinator=coordinator, state=state)
        if actively_onboarding
        else []
    )
    onboarding = (
        compute_onboarding_render(
            session,
            coordinator=coordinator,
            state=state,
            completed=completed_step_ids,
        )
        if actively_onboarding
        else None
    )
    return CoordinatorStateResponse(
        coordinator_id=coordinator.agent_id,
        completed_step_ids=completed_step_ids,
        onboarding=onboarding,
        voice_intro_briefing=(
            compose_voice_intro_briefing(onboarding) if onboarding else ""
        ),
        **state,
    )


@router.get(
    "/assistant/onboarding/catalog",
    response_model=InfoResponse[OnboardingCatalog],
    status_code=status.HTTP_200_OK,
    summary="Read the static, deployment-gated onboarding catalog",
    tags=["Assistant Management"],
)
async def get_onboarding_catalog_endpoint() -> InfoResponse[OnboardingCatalog]:
    """Return the canonical onboarding structure + copy for this deployment.

    Static (no per-user state) and the single source of truth both Console
    and Unity read for phase/step titles, descriptions, time estimates, and
    suggestion chips. ``local_only`` phases are already filtered out on
    hosted deployments, so consumers never re-implement the gate.
    """
    return InfoResponse(info=OnboardingCatalog(**build_onboarding_catalog()))


@router.get(
    "/assistant/{coordinator_id}/state",
    response_model=InfoResponse[CoordinatorStateResponse],
    status_code=status.HTTP_200_OK,
    summary="Read the Coordinator's onboarding state",
    tags=["Assistant Management"],
)
async def get_coordinator_state_endpoint(
    coordinator_id: int,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[CoordinatorStateResponse]:
    """Return the latest Coordinator/State snapshot for this workspace."""
    coordinator = require_authorized_coordinator(
        session,
        coordinator_id=coordinator_id,
        user_id=request.state.user_id,
    )
    return InfoResponse(
        info=_coordinator_state_response(session, coordinator=coordinator),
    )


@router.patch(
    "/assistant/{coordinator_id}/state",
    response_model=InfoResponse[CoordinatorStateResponse],
    status_code=status.HTTP_200_OK,
    summary="Update the Coordinator's onboarding state",
    tags=["Assistant Management"],
)
async def update_coordinator_state_endpoint(
    coordinator_id: int,
    update: CoordinatorStateUpdate,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[CoordinatorStateResponse]:
    """Update the Coordinator's onboarding state.

    Used by the assistants page when the user pauses or resumes
    onboarding (writes ``onboarding_active``), and when the user
    advances, skips, or resets checklist steps (writes ``onboarding_step``
    and related fields).
    """
    coordinator = require_authorized_coordinator(
        session,
        coordinator_id=coordinator_id,
        user_id=request.state.user_id,
    )
    previous_state = get_coordinator_state(session, coordinator=coordinator)
    next_state = set_coordinator_state(
        session,
        coordinator=coordinator,
        onboarding_active=update.onboarding_active,
        onboarding_step=update.onboarding_step,
        clear_onboarding_step=update.clear_onboarding_step,
        skip_onboarding_step=update.skip_onboarding_step,
        unskip_onboarding_step=update.unskip_onboarding_step,
        reset_onboarding_step=update.reset_onboarding_step,
        skip_onboarding_phase=update.skip_onboarding_phase,
        unskip_onboarding_phase=update.unskip_onboarding_phase,
        intro_watched=update.intro_watched,
        pending_chat_intro=update.pending_chat_intro,
        onboarding_step_completion=(
            (
                update.onboarding_step_completion.step_id,
                update.onboarding_step_completion.completed,
            )
            if update.onboarding_step_completion is not None
            else None
        ),
    )
    # Commit the state write immediately so its Coordinator/State advisory
    # lock is released before the event emissions below, which POST to the
    # adapters. Holding the lock across network I/O starves concurrent state
    # writers (other PATCHes, the picker-resolution event) into Postgres
    # lock timeouts.
    session.commit()
    # The event branches below all read the same post-update progress; derive
    # it at most once per request (each derivation walks the whole onboarding
    # graph with per-step probes).
    derived_completed: list[str] | None = None

    def _completed_step_ids() -> list[str]:
        nonlocal derived_completed
        if not next_state.get("onboarding_active"):
            return []
        if derived_completed is None:
            derived_completed = derive_onboarding_progress(
                session,
                coordinator=coordinator,
                state=next_state,
            )
        return derived_completed

    if (
        update.onboarding_step
        and next_state.get("onboarding_active")
        and previous_state.get("onboarding_step") != update.onboarding_step
    ):
        await emit_onboarding_step_started_event(
            session,
            coordinator=coordinator,
            step_id=update.onboarding_step,
            completed_step_ids=_completed_step_ids(),
            skipped_step_ids=next_state.get("skipped_step_ids", []),
        )
    if update.skip_onboarding_step:
        await emit_onboarding_step_skipped_event(
            session,
            coordinator=coordinator,
            step_id=update.skip_onboarding_step,
            completed_step_ids=_completed_step_ids(),
            skipped_step_ids=next_state.get("skipped_step_ids", []),
        )
    if update.reset_onboarding_step:
        await emit_onboarding_step_reset_event(
            session,
            coordinator=coordinator,
            step_id=update.reset_onboarding_step,
            completed_step_ids=_completed_step_ids(),
            skipped_step_ids=next_state.get("skipped_step_ids", []),
        )
    if (
        update.onboarding_step_completion is not None
        and update.onboarding_step_completion.completed
        and next_state.get("onboarding_active")
    ):
        emit_onboarding_step_completed_event_safe_sync(
            session,
            coordinator=coordinator,
            step_id=update.onboarding_step_completion.step_id,
            completed_step_ids=_completed_step_ids(),
            skipped_step_ids=next_state.get("skipped_step_ids", []),
        )
    session.commit()
    return InfoResponse(
        info=_coordinator_state_response(session, coordinator=coordinator),
    )


@router.post(
    "/assistant/{coordinator_id}/onboarding-step-event",
    response_model=InfoResponse[OnboardingStepEventResponse],
    status_code=status.HTTP_200_OK,
    summary="Emit the graph-owned event attached to one onboarding step",
    tags=["Assistant Management"],
)
async def emit_onboarding_step_event_endpoint(
    coordinator_id: int,
    body: OnboardingStepEventRequest,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[OnboardingStepEventResponse]:
    """Fire the canonical event for a user-triggered onboarding row."""
    coordinator = require_authorized_coordinator(
        session,
        coordinator_id=coordinator_id,
        user_id=request.state.user_id,
    )
    emitted = await emit_onboarding_step_event(
        session,
        coordinator=coordinator,
        step_id=body.step_id,
        chip_id=body.chip_id,
    )
    session.commit()
    return InfoResponse(
        info=OnboardingStepEventResponse(
            coordinator_id=str(coordinator.agent_id),
            step_id=body.step_id,
            chip_id=body.chip_id,
            emitted=emitted,
        ),
    )


@router.post(
    "/assistant/{coordinator_id}/onboarding-session-started",
    response_model=InfoResponse[OnboardingSessionStartedResponse],
    status_code=status.HTTP_200_OK,
    summary="Notify the Coordinator that the onboarding picker just resolved",
    tags=["Assistant Management"],
)
async def notify_onboarding_session_started_endpoint(
    coordinator_id: int,
    body: OnboardingSessionStarted,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[OnboardingSessionStartedResponse]:
    """Fire the picker-resolution event so Unity opens the session.

    Best-effort: the emission is gated server-side on
    ``Coordinator/State.onboarding_active``, so a stale picker
    submit (e.g. the user already skipped onboarding in another
    tab) silently no-ops. The endpoint always returns 200; the
    response body carries an ``emitted`` flag the client can use
    for telemetry but doesn't need for correctness.
    """
    coordinator = require_authorized_coordinator(
        session,
        coordinator_id=coordinator_id,
        user_id=request.state.user_id,
    )
    emitted = await emit_onboarding_session_started_event(
        session,
        coordinator=coordinator,
        medium=body.medium,
    )
    return InfoResponse(
        info=OnboardingSessionStartedResponse(
            coordinator_id=str(coordinator.agent_id),
            emitted=emitted,
        ),
    )


@router.post(
    "/assistant/{coordinator_id}/wakeup",
    response_model=InfoResponse[CoordinatorWakeupResponse],
    status_code=status.HTTP_200_OK,
    summary="Wake the Coordinator runtime early",
    tags=["Assistant Management"],
    dependencies=[Depends(require_console_origin_for_free_accounts)],
)
async def wake_coordinator_endpoint(
    coordinator_id: int,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[CoordinatorWakeupResponse]:
    """Start the Coordinator GKE job without waiting for a user action.

    Best-effort: a transient adapters outage is logged and the endpoint
    still returns 200 so Console can fire this during onboarding without
    blocking navigation.
    """
    coordinator = require_authorized_coordinator(
        session,
        coordinator_id=coordinator_id,
        user_id=request.state.user_id,
    )
    await wake_up_coordinator_best_effort(coordinator.agent_id)
    return InfoResponse(
        info=CoordinatorWakeupResponse(
            coordinator_id=str(coordinator.agent_id),
            attempted=not (settings.is_self_host or not comms_explicitly_configured()),
        ),
    )


@router.post(
    "/assistant/{target_assistant_id}/delegate",
    response_model=InfoResponse[CoordinatorDelegateResponse],
    status_code=status.HTTP_200_OK,
    summary="Assign asynchronous work to a colleague assistant",
    tags=["Assistant Management"],
    dependencies=[Depends(require_console_origin_for_free_accounts)],
)
async def delegate_to_colleague_endpoint(
    target_assistant_id: int,
    request_body: CoordinatorDelegateRequest,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[CoordinatorDelegateResponse]:
    """Dispatch a Coordinator assignment to the target colleague runtime."""
    coordinator, target = require_authorized_delegate_target(
        session,
        target_assistant_id=target_assistant_id,
        user_id=request.state.user_id,
    )
    delivery = await delegate_to_colleague_runtime(
        assistant_id=target.agent_id,
        requested_by_assistant_id=coordinator.agent_id,
        instruction=request_body.instruction,
        intent=request_body.intent,
        dedupe_key=request_body.dedupe_key,
        related_context=request_body.related_context,
    )
    return InfoResponse(
        info=CoordinatorDelegateResponse(
            coordinator_id=coordinator.agent_id,
            target_assistant_id=target.agent_id,
            status=str(delivery.get("status") or "accepted"),
            activation_id=delivery.get("activation_id"),
            accepted=bool(delivery.get("accepted", True)),
            completion_status=str(
                delivery.get("completion_status") or "pending_async",
            ),
            receipt_type=str(
                delivery.get("receipt_type") or "async_delegation_receipt",
            ),
            message=str(
                delivery.get("message")
                or CoordinatorDelegateResponse.model_fields["message"].default,
            ),
        ),
    )


@router.get(
    "/assistant",
    response_model=InfoResponse[List[AssistantRead]],
    status_code=status.HTTP_200_OK,
    summary="List all assistants",
    description="Returns a list of all assistants belonging to the authenticated user.",
    tags=["Assistant Management"],
    responses={
        200: {
            "description": "List of assistants retrieved successfully",
            "content": {
                "application/json": {
                    "example": {
                        "info": [
                            {
                                "agent_id": "123",
                                "first_name": "Alice",
                                "surname": "Smith",
                                "age": 25,
                                "weekly_limit": 40.0,
                                "max_parallel": 3,
                                "phone": "+1-555-123-4567",
                                "email": "alice.smith@example.com",
                                "nationality": "United States",
                                "profile_photo": "https://example.com/photos/alice.jpg",
                                "profile_video": "https://example.com/videos/alice.mp4",
                                "about": "Mathematician and writer known for work on Analytical Engine",
                                "voice_id": "bf0a246a-8642-498a-9950-80c35e9276b5",
                                "voice_provider": "cartesia",
                                "phone_country": "US",
                                "timezone": "America/New_York",
                                "created_at": "2025-04-25T12:00:00Z",
                                "updated_at": "2025-04-25T12:00:00Z",
                            },
                            {
                                "agent_id": "456",
                                "first_name": "Bob",
                                "surname": "Jones",
                                "age": 30,
                                "weekly_limit": 35.5,
                                "max_parallel": 2,
                                "phone": "+1-555-987-6543",
                                "email": "bob.jones@example.com",
                                "nationality": "Mexico",
                                "profile_photo": "https://example.com/photos/bob.jpg",
                                "profile_video": "https://example.com/videos/bob.mp4",
                                "about": "Machine learning expert with focus on computer vision",
                                "voice_id": "bf0a246a-8642-498a-9950-80c35e9276b5",
                                "voice_provider": "cartesia",
                                "phone_country": "CA",
                                "timezone": "America/Vancouver",
                                "created_at": "2025-04-24T10:30:00Z",
                                "updated_at": "2025-04-24T10:30:00Z",
                            },
                        ],
                    },
                },
            },
        },
    },
)
def list_assistants(
    request: Request,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_db_session),
    phone: Optional[str] = Query(
        None,
        description="Only return assistants whose phone number matches this E.164-style value (leading '+' is URL-encoded).",
    ),
    email: Optional[str] = Query(
        None,
        description="Only return assistants whose email address matches this value.",
    ),
    agent_id: Optional[int] = Query(
        None,
        description="Only return assistants whose agent_id matches this value.",
    ),
    list_all_org: bool = Query(
        False,
        description="If True and using an org API key, list ALL assistants in the organization (not just those created by the current user). Requires assistant:read permission.",
    ),
) -> InfoResponse[List[AssistantRead]]:
    """
    List assistants based on API key context.

    For personal API key: Returns all personal assistants created by the user.
    For org API key (list_all_org=False): Returns assistants created by the user in this org.
    For org API key (list_all_org=True): Returns ALL assistants in the org (requires assistant:read permission).
    """
    # Correct for URL-decoded '+' in query parameters.
    phone = normalize_phone_parameter(phone)

    assistant_dao = AssistantDAO(session)
    user_id = request.state.user_id

    # Get organization context from API key
    organization_id = getattr(request.state, "organization_id", None)

    try:
        if organization_id is not None and list_all_org:
            # Org context with list_all_org=True: list all org assistants
            # Check if user has assistant:read permission
            resource_access_dao = ResourceAccessDAO(session)
            has_permission = resource_access_dao.check_org_member_permission(
                user_id,
                organization_id,
                "assistant:read",
            )
            if not has_permission:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You do not have permission to view all assistants in this organization.",
                )
            assistants = assistant_dao.list_all_org_assistants(
                organization_id=organization_id,
                requesting_user_id=user_id,
                phone=phone,
                email=email,
                agent_id=agent_id,
            )
        else:
            # Personal context OR org context with list_all_org=False
            assistants = assistant_dao.list_assistants_for_user(
                user_id,
                organization_id=organization_id,
                phone=phone,
                email=email,
                agent_id=agent_id,
            )
        voice_dao = VoiceDAO(session)

        user_dao = UserDAO(session)
        users = {a.user_id: user_dao.get_by_id(a.user_id)[0] for a in assistants}

        # Batch-fetch contacts for all assistants (avoids N+1 queries)
        contact_dao = AssistantContactDAO(session)
        all_contacts = contact_dao.get_active_contacts_for_assistants(
            [a.agent_id for a in assistants],
        )
        contacts_by_assistant: dict[int, list] = {}
        for c in all_contacts:
            contacts_by_assistant.setdefault(c.assistant_id, []).append(c)

        # Batch-fetch only the workspace granted-scope secrets so each read can
        # report the OAuth-connected workspace provider without an N+1 — and
        # without exposing the full secrets set (this endpoint doesn't return
        # ``secrets``).
        from orchestra.db.models.orchestra_models import AssistantSecret

        workspace_secrets_by_assistant: dict[int, dict[str, str]] = {}
        _scope_agent_ids = [a.agent_id for a in assistants]
        if _scope_agent_ids:
            _scope_rows = (
                session.query(AssistantSecret)
                .filter(
                    AssistantSecret.agent_id.in_(_scope_agent_ids),
                    AssistantSecret.secret_name.in_(
                        ["GOOGLE_GRANTED_SCOPES", "MICROSOFT_GRANTED_SCOPES"],
                    ),
                )
                .all()
            )
            for s in _scope_rows:
                workspace_secrets_by_assistant.setdefault(s.agent_id, {})[
                    s.secret_name
                ] = s.secret_value

        # Backfill any missing platform-managed Coordinator contacts on read so
        # Coordinators predating the universal-contact rollout self-heal on the
        # owner's next visit. Mutates ``contacts_by_assistant`` in place. Also
        # useful to self-heal existing coordinators after new contact types are
        # configured.
        owned_coordinators = [
            a for a in assistants if a.is_coordinator and a.user_id == user_id
        ]
        if _self_heal_coordinator_contacts(
            session,
            coordinators=owned_coordinators,
            contacts_by_assistant=contacts_by_assistant,
            contact_dao=contact_dao,
        ):
            background_tasks.add_task(notify_comms_discord_sync)

        team_dao = TeamDAO(session)
        assistant_ids = [a.agent_id for a in assistants]
        team_ids_by_assistant = team_dao.team_ids_for_assistants(
            assistant_ids,
        )
        team_summaries_by_assistant = team_dao.team_summaries_for_assistants(
            assistant_ids,
        )
        contact_ids_by_assistant = _resolved_contact_ids_for_assistants(
            session,
            assistant_ids,
        )
        contact_identity_roots_by_assistant = (
            _resolved_contact_identity_roots_for_assistants(
                session,
                assistant_ids,
                team_ids_by_assistant=team_ids_by_assistant,
                personal_ids_by_assistant=contact_ids_by_assistant,
            )
        )

        return InfoResponse(
            info=[
                _build_assistant_read(
                    a,
                    session,
                    user_first_name=(
                        users[a.user_id].name if users.get(a.user_id) else None
                    ),
                    user_last_name=(
                        users[a.user_id].last_name if users.get(a.user_id) else None
                    ),
                    user_email=users[a.user_id].email if users.get(a.user_id) else None,
                    user_image=users[a.user_id].image if users.get(a.user_id) else None,
                    user_whatsapp_number=(
                        users[a.user_id].whatsapp_number
                        if users.get(a.user_id)
                        else None
                    ),
                    contacts=contacts_by_assistant.get(a.agent_id, []),
                    team_ids=team_ids_by_assistant.get(a.agent_id, []),
                    team_summaries=team_summaries_by_assistant.get(
                        a.agent_id,
                        [],
                    ),
                    self_contact_id=_contact_id_pair(
                        contact_ids_by_assistant,
                        a.agent_id,
                    ).self_contact_id,
                    boss_contact_id=_contact_id_pair(
                        contact_ids_by_assistant,
                        a.agent_id,
                    ).boss_contact_id,
                    contact_identity_roots=contact_identity_roots_by_assistant.get(
                        a.agent_id,
                        [],
                    ),
                    workspace_secrets=workspace_secrets_by_assistant.get(
                        a.agent_id,
                        {},
                    ),
                    requesting_user_id=user_id,
                )
                for a in assistants
            ],
        )
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error fetching assistants: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Error fetching assistants",
        )


def _require_assistant_write_access(
    session: Session,
    *,
    request: Request,
    assistant: Assistant,
    assistant_id: int,
) -> None:
    user_id = request.state.user_id
    organization_id = getattr(request.state, "organization_id", None)
    if organization_id is not None:
        resource_access_dao = ResourceAccessDAO(session)
        has_permission = resource_access_dao.check_user_permission(
            user_id,
            "assistant",
            assistant_id,
            "assistant:write",
        )
        if not has_permission:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to modify this assistant.",
            )


def _build_managed_desktop_status_read(
    session: Session,
    assistant: Assistant,
) -> ManagedDesktopStatusRead:
    monthly_cost = None
    if assistant.desktop_mode in MANAGED_DESKTOP_MODES:
        monthly_cost = float(
            get_managed_desktop_monthly_cost(session, assistant.desktop_mode),
        )
    elif assistant.managed_desktop_monthly_cost is not None:
        monthly_cost = float(assistant.managed_desktop_monthly_cost)
    external_ip = assistant.external_ip
    rotation = (
        session.query(AssistantExternalIPRotation)
        .filter(AssistantExternalIPRotation.external_ip_id == external_ip.id)
        .order_by(AssistantExternalIPRotation.requested_at.desc())
        .first()
        if external_ip is not None
        else None
    )
    return ManagedDesktopStatusRead(
        desktop_mode=assistant.desktop_mode,
        managed_desktop_status=assistant.managed_desktop_status,
        monthly_cost=monthly_cost,
        managed_desktop_enabled_at=assistant.managed_desktop_enabled_at,
        managed_desktop_grace_period_started_at=(
            assistant.managed_desktop_grace_period_started_at
        ),
        network_identity=(
            ManagedDesktopNetworkIdentityRead(
                gcp_address_name=external_ip.gcp_address_name,
                address=external_ip.address,
                region=external_ip.region,
                pool_location=external_ip.pool_location,
                hostname=external_ip.hostname,
                state=external_ip.state,
                active_operation=external_ip.active_operation,
                rotation=(
                    ManagedDesktopIPRotationRead(
                        id=rotation.id,
                        state=rotation.state,
                        error=rotation.error,
                        old_address=rotation.old_address,
                        candidate_address=rotation.candidate_address,
                        rollback_expires_at=rotation.rollback_expires_at,
                        requested_at=rotation.requested_at,
                        completed_at=rotation.completed_at,
                    )
                    if rotation is not None
                    else None
                ),
            )
            if external_ip is not None
            else None
        ),
    )


@admin_router.post("/assistant/{assistant_id}/managed-desktop/network-identity")
def report_managed_desktop_network_identity(
    assistant_id: int,
    payload: ManagedDesktopNetworkIdentityReport,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Record the assistant IP actually attached by the deployment control plane."""

    assistant = session.get(Assistant, assistant_id)
    if assistant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )
    try:
        external_ip = record_assistant_external_ip_attachment(
            session,
            assistant_id=assistant_id,
            **payload.model_dump(),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    session.commit()
    return {
        "assistant_id": assistant_id,
        "gcp_address_name": external_ip.gcp_address_name,
        "address": external_ip.address,
        "region": external_ip.region,
        "pool_location": external_ip.pool_location,
        "hostname": external_ip.hostname,
    }


@router.get(
    "/assistant/{assistant_id}/managed-desktop",
    response_model=InfoResponse[ManagedDesktopStatusRead],
    tags=["Assistant Management"],
)
def get_managed_desktop_status(
    assistant_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_db_session),
) -> InfoResponse[ManagedDesktopStatusRead]:
    user_id = request.state.user_id
    organization_id = getattr(request.state, "organization_id", None)
    assistant_dao = AssistantDAO(session)
    assistant = assistant_dao.get_assistant_by_id(
        user_id=user_id,
        agent_id=assistant_id,
        organization_id=organization_id,
    )
    if not assistant or _is_hidden_workspace_coordinator_for_user(
        assistant,
        user_id=user_id,
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )
    external_ip = assistant.external_ip
    if managed_desktop_entitled(assistant) and (
        external_ip is None
        or (
            external_ip.state != "retained"
            and (
                not external_ip.desired_pool_location
                or external_ip.pool_location != external_ip.desired_pool_location
            )
        )
    ):
        background_tasks.add_task(
            reconcile_assistant_external_ip,
            request.app.state.db_session_factory,
            assistant_id=assistant_id,
        )
    elif (
        external_ip is not None
        and external_ip.state not in {"released", "retained"}
        and external_ip.gcp_address_name
    ):
        # A VM may still be detaching the address when disable is requested.
        # Recheck on subsequent status reads until deploy confirms deletion.
        background_tasks.add_task(
            release_managed_desktop_external_ip,
            request.app.state.db_session_factory,
            assistant_id=assistant_id,
        )
    return InfoResponse(info=_build_managed_desktop_status_read(session, assistant))


@router.post(
    "/assistant/{assistant_id}/managed-desktop/network-identity/rotate",
    response_model=InfoResponse[ManagedDesktopIPRotationRead],
    status_code=status.HTTP_202_ACCEPTED,
    tags=["Assistant Management"],
)
async def rotate_managed_desktop_network_identity(
    assistant_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_db_session),
) -> InfoResponse[ManagedDesktopIPRotationRead]:
    """Request a guarded, asynchronous egress-IP rotation for a desktop."""

    user_id = request.state.user_id
    organization_id = getattr(request.state, "organization_id", None)
    assistant = AssistantDAO(session).get_assistant_by_id(
        user_id=user_id,
        agent_id=assistant_id,
        organization_id=organization_id,
    )
    if not assistant or _is_hidden_workspace_coordinator_for_user(
        assistant,
        user_id=user_id,
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )
    _require_assistant_write_access(
        session,
        request=request,
        assistant=assistant,
        assistant_id=assistant_id,
    )
    if not managed_desktop_entitled(assistant):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Computer Use must be active before rotating its IP.",
        )
    try:
        rotation = request_assistant_external_ip_rotation(session, assistant=assistant)
        session.commit()
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))

    background_tasks.add_task(
        run_assistant_external_ip_rotation,
        request.app.state.db_session_factory,
        assistant_id=assistant_id,
        operation_id=rotation.id,
    )
    return InfoResponse(
        info=ManagedDesktopIPRotationRead(
            id=rotation.id,
            state=rotation.state,
            error=rotation.error,
            old_address=rotation.old_address,
            candidate_address=rotation.candidate_address,
            rollback_expires_at=rotation.rollback_expires_at,
            requested_at=rotation.requested_at,
            completed_at=rotation.completed_at,
        ),
    )


@router.get(
    "/assistant/{assistant_id}/managed-desktop/network-identity/rotation",
    response_model=InfoResponse[ManagedDesktopIPRotationRead],
    tags=["Assistant Management"],
)
def get_managed_desktop_network_identity_rotation(
    assistant_id: int,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[ManagedDesktopIPRotationRead]:
    """Read the newest rotation operation visible to the assistant owner."""

    user_id = request.state.user_id
    organization_id = getattr(request.state, "organization_id", None)
    assistant = AssistantDAO(session).get_assistant_by_id(
        user_id=user_id,
        agent_id=assistant_id,
        organization_id=organization_id,
    )
    if not assistant or _is_hidden_workspace_coordinator_for_user(
        assistant,
        user_id=user_id,
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )
    _require_assistant_write_access(
        session,
        request=request,
        assistant=assistant,
        assistant_id=assistant_id,
    )
    external_ip = assistant.external_ip
    rotation = (
        session.query(AssistantExternalIPRotation)
        .filter(AssistantExternalIPRotation.external_ip_id == external_ip.id)
        .order_by(AssistantExternalIPRotation.requested_at.desc())
        .first()
        if external_ip is not None
        else None
    )
    if rotation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Rotation not found.",
        )
    return InfoResponse(
        info=ManagedDesktopIPRotationRead(
            id=rotation.id,
            state=rotation.state,
            error=rotation.error,
            old_address=rotation.old_address,
            candidate_address=rotation.candidate_address,
            rollback_expires_at=rotation.rollback_expires_at,
            requested_at=rotation.requested_at,
            completed_at=rotation.completed_at,
        ),
    )


@router.post(
    "/assistant/{assistant_id}/managed-desktop",
    response_model=InfoResponse[AssistantRead],
    tags=["Assistant Management"],
)
async def enable_managed_desktop_endpoint(
    assistant_id: int,
    payload: ManagedDesktopEnable,
    request: Request,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_db_session),
) -> InfoResponse[AssistantRead]:
    user_id = request.state.user_id
    organization_id = getattr(request.state, "organization_id", None)
    assistant_dao = AssistantDAO(session)
    assistant = assistant_dao.get_assistant_by_id(
        user_id=user_id,
        agent_id=assistant_id,
        organization_id=organization_id,
    )
    if not assistant or _is_hidden_workspace_coordinator_for_user(
        assistant,
        user_id=user_id,
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )
    _require_assistant_write_access(
        session,
        request=request,
        assistant=assistant,
        assistant_id=assistant_id,
    )
    if managed_desktop_entitled(assistant):
        if assistant.desktop_mode == payload.desktop_mode:
            ensure_pending_assistant_external_ip(session, assistant=assistant)
            session.commit()
            background_tasks.add_task(
                reconcile_assistant_external_ip,
                request.app.state.db_session_factory,
                assistant_id=assistant_id,
            )
            return InfoResponse(info=_build_assistant_read(assistant, session))
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Disable Computer Use before switching desktop mode.",
        )

    billing_entity = get_billing_entity(session, user_id, organization_id)
    charge_managed_desktop_first_month(
        session,
        assistant=assistant,
        desktop_mode=payload.desktop_mode,
        billing_entity=billing_entity,
        user_id=user_id,
        organization_id=organization_id,
    )
    ensure_pending_assistant_external_ip(session, assistant=assistant)
    session.commit()
    background_tasks.add_task(
        reconcile_assistant_external_ip,
        request.app.state.db_session_factory,
        assistant_id=assistant_id,
    )

    from orchestra.web.api.utils.assistant_infra import reawaken_assistant

    try:
        await reawaken_assistant(str(assistant_id))
    except Exception as exc:
        logging.warning(
            "Failed to reawaken assistant %s after enabling Computer Use: %s",
            assistant_id,
            exc,
        )

    assistant = assistant_dao.get_assistant_by_id(
        user_id=user_id,
        agent_id=assistant_id,
        organization_id=organization_id,
    )
    return InfoResponse(info=_build_assistant_read(assistant, session))


@router.delete(
    "/assistant/{assistant_id}/managed-desktop",
    response_model=InfoResponse[AssistantRead],
    tags=["Assistant Management"],
)
async def disable_managed_desktop_endpoint(
    assistant_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_db_session),
) -> InfoResponse[AssistantRead]:
    user_id = request.state.user_id
    organization_id = getattr(request.state, "organization_id", None)
    assistant_dao = AssistantDAO(session)
    assistant = assistant_dao.get_assistant_by_id(
        user_id=user_id,
        agent_id=assistant_id,
        organization_id=organization_id,
    )
    if not assistant or _is_hidden_workspace_coordinator_for_user(
        assistant,
        user_id=user_id,
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )
    _require_assistant_write_access(
        session,
        request=request,
        assistant=assistant,
        assistant_id=assistant_id,
    )
    if not assistant.managed_desktop_status and assistant.desktop_mode is None:
        return InfoResponse(info=_build_assistant_read(assistant, session))

    disable_managed_desktop(assistant)
    session.commit()

    from orchestra.web.api.utils.assistant_infra import stop_assistant_session_runtime

    try:
        stop_result = await stop_assistant_session_runtime(str(assistant_id))
        if not stop_result.get("success"):
            logging.warning(
                "Failed to stop assistant runtime %s after disabling Computer Use: %s",
                assistant_id,
                stop_result,
            )
    except Exception as exc:
        logging.warning(
            "Failed to stop assistant runtime %s after disabling Computer Use: %s",
            assistant_id,
            exc,
        )
    background_tasks.add_task(
        release_managed_desktop_external_ip,
        request.app.state.db_session_factory,
        assistant_id=assistant_id,
    )

    assistant = assistant_dao.get_assistant_by_id(
        user_id=user_id,
        agent_id=assistant_id,
        organization_id=organization_id,
    )
    return InfoResponse(info=_build_assistant_read(assistant, session))


@router.delete(
    "/assistant/{assistant_id}/contact",
    response_model=InfoResponse[AssistantRead],
    status_code=status.HTTP_200_OK,
    summary="Remove a contact method from an assistant",
    description="Removes a contact method (phone, email, or WhatsApp) from an assistant and deprovisions the associated infrastructure.",
    tags=["Assistant Management"],
    responses={
        200: {
            "description": "Contact method removed successfully.",
        },
        404: {
            "description": "Assistant not found.",
        },
        400: {
            "description": "Invalid contact type or other error.",
        },
    },
)
async def delete_assistant_contact(
    assistant_id: int,
    removal_payload: AssistantContactRemoval,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[AssistantRead]:
    """
    Remove a contact method from an assistant.

    This endpoint deprovisions the infrastructure for a specific contact method
    (e.g., deletes the Twilio phone number) and removes the information from the
    assistant's record.
    """
    user_id = request.state.user_id
    organization_id = getattr(request.state, "organization_id", None)
    assistant_dao = AssistantDAO(session)

    assistant = assistant_dao.get_assistant_by_id(
        user_id=user_id,
        agent_id=assistant_id,
        organization_id=organization_id,
    )

    if not assistant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )
    if _is_hidden_workspace_coordinator_for_user(assistant, user_id=user_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )

    # For org assistants, check assistant:write permission
    if organization_id is not None:
        resource_access_dao = ResourceAccessDAO(session)
        has_permission = resource_access_dao.check_user_permission(
            user_id,
            "assistant",
            assistant_id,
            "assistant:write",
        )
        if not has_permission:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to modify this assistant.",
            )

    contact_type = removal_payload.contact_type

    try:
        # Look up the contact from the AssistantContact table
        contact_dao = AssistantContactDAO(session)
        contact = contact_dao.get_contact_by_assistant_and_type(
            assistant_id,
            contact_type,
        )

        # Private coordinator contacts are platform-managed: the shared
        # universal email / phone / WhatsApp pools are owned by the repair
        # path (``ensure_coordinator_*``), so a deleted platform contact
        # would just be re-provisioned on the owner's next visit — leaving a
        # confusing gap in inbound routing meanwhile. Block deletion of
        # those. Legacy ``provisioned_by="user"`` rows that predate the
        # connect gating stay deletable so leftover BYOD contacts can
        # still be cleaned up (here and via the disconnect endpoint).
        # Multiplayer twins manage dedicated contacts like any hire, except
        # the alias email — that address is the twin's required outward
        # identity and cannot be removed.
        if (
            assistant.is_private_coordinator
            and contact is not None
            and contact.provisioned_by != "user"
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="coordinator_contacts_are_platform_managed",
            )
        if (
            assistant.is_multiplayer
            and contact is not None
            and (contact.metadata_ or {}).get("twin_alias")
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="twin_alias_email_is_permanent",
            )

        if contact:
            # BYOD contacts: skip external deprovisioning (we don't own the resource).
            # Email contacts are BYOD-only since platform mailboxes were retired,
            # so they always fall into the user-owned branch — nothing external
            # to deprovision. Stale platform email rows are handled by the
            # one-shot orchestra.workers.teardown_platform_mailboxes worker.
            if contact.provisioned_by != "user":
                if contact_type == "phone" and contact.contact_value:
                    await delete_phone_number(
                        contact.contact_value,
                    )
                elif contact_type == "whatsapp":
                    from orchestra.web.api.utils.assistant_infra import (
                        delete_whatsapp_routes,
                    )

                    await delete_whatsapp_routes(assistant_id, session)

                elif contact_type == "discord":
                    from orchestra.web.api.utils.assistant_infra import (
                        delete_discord_routes,
                    )

                    await delete_discord_routes(assistant_id, session)

            # Soft-delete the AssistantContact row
            contact_dao.soft_delete_assistant_contact(
                assistant_id=assistant_id,
                contact_type=contact_type,
            )

        session.commit()
        session.refresh(assistant)
        updated_assistant = assistant

        # After successfully updating, trigger a reawaken
        try:
            await reawaken_assistant(
                str(updated_assistant.agent_id),
            )
        except Exception as e:
            # Log the error but don't fail the request, as the main action succeeded
            logging.warning(
                f"Failed to reawaken assistant {updated_assistant.agent_id} after contact deletion: {e}",
            )

        return InfoResponse(
            info=_build_assistant_read(updated_assistant, session),
        )

    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logging.error(
            f"Failed to delete contact for assistant {assistant_id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to remove contact",
        )


@router.post(
    "/assistant/{assistant_id}/contact",
    response_model=InfoResponse[AssistantRead],
    status_code=status.HTTP_200_OK,
    summary="Create a contact detail for an assistant",
    description=(
        "Provisions external infrastructure (phone number, email, or WhatsApp sender) "
        "for the given assistant and creates a billing-tracked AssistantContact record. "
        "Deducts the one-time setup cost from credits."
    ),
    tags=["Assistant Management"],
    responses={
        200: {"description": "Contact created successfully."},
        402: {"description": "Insufficient credits."},
        404: {"description": "Assistant not found."},
        409: {"description": "Contact type already exists for this assistant."},
        410: {
            "description": (
                "Platform-issued email mailbox provisioning is no longer "
                "supported. Use BYOD (provisioned_by='user') instead."
            ),
        },
    },
)
async def create_assistant_contact(
    assistant_id: int,
    contact_request: AssistantContactCreate,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[AssistantRead]:
    """
    Create a new contact detail for an assistant.

    This endpoint:
    1. Checks that the assistant exists and the user has permission.
    2. Checks that no active contact of the same type already exists.
    3. Looks up the one-time and monthly costs from the AssistantContactCost table.
    4. Verifies the billing account has sufficient credits for the one-time cost.
    5. Provisions the external resource (Twilio number, Google Workspace email,
       WhatsApp sender).
    6. Creates an AssistantContact row and updates the backward-compat columns
       on the Assistant model.
    7. Deducts the one-time cost from credits.
    8. Triggers a reawaken so Unity picks up the new contact detail.

    If the database commit fails after provisioning, the external resource is
    rolled back (deprovisioned) to prevent resource leaks.
    """
    user_id = request.state.user_id
    organization_id = getattr(request.state, "organization_id", None)
    assistant_dao = AssistantDAO(session)

    # 1. Fetch and verify ownership
    assistant = assistant_dao.get_assistant_by_id(
        user_id=user_id,
        agent_id=assistant_id,
        organization_id=organization_id,
    )
    if not assistant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )
    if _is_hidden_workspace_coordinator_for_user(assistant, user_id=user_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )

    # Private coordinator contacts are platform-managed (shared universal
    # email / phone / WhatsApp pools provisioned by the
    # ``ensure_coordinator_*`` helpers). Manual contact creation — including
    # BYOD — is never valid for a single-player Coordinator, so reject it
    # here rather than letting a row land that the repair path would later
    # clobber. Multiplayer twins provision dedicated contacts through this
    # endpoint exactly like hired teammates.
    if assistant.is_private_coordinator:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="coordinator_contacts_are_platform_managed",
        )

    # Permission check for org assistants
    if organization_id is not None:
        resource_access_dao = ResourceAccessDAO(session)
        has_permission = resource_access_dao.check_user_permission(
            user_id,
            "assistant",
            assistant_id,
            "assistant:write",
        )
        if not has_permission:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to modify this assistant.",
            )

    contact_type = contact_request.contact_type
    is_byod = contact_request.provisioned_by == "user"

    # Platform-issued mailbox provisioning is retired. Direct API/SDK
    # callers can still create BYOD email contacts (provisioned_by="user");
    # everything else on the email tab must connect their own mailbox.
    if contact_type == "email" and not is_byod:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=(
                "Platform-issued assistant mailboxes (@unify.ai / Microsoft 365) "
                "are no longer offered. Connect your own email account by "
                "setting provisioned_by='user' and supplying contact_value + "
                "email_provider, or use the OAuth connect flow in the console."
            ),
        )

    # 2. Require verified profile identity for phone/whatsapp/discord contacts
    if contact_type in ("phone", "whatsapp", "discord"):
        user_dao = UserDAO(session)
        user_rows = user_dao.filter(id=user_id)
        if not user_rows:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found.",
            )
        user = user_rows[0][0]
        if contact_type == "phone" and not user.phone_number:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "A verified phone number is required on your profile "
                    "before creating a phone contact for an assistant."
                ),
            )
        if contact_type == "whatsapp" and not user.whatsapp_number:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "A verified WhatsApp number is required on your profile "
                    "before creating a WhatsApp contact for an assistant."
                ),
            )
        if contact_type == "discord" and not user.discord_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "A linked Discord account is required on your profile "
                    "before creating a Discord contact for an assistant."
                ),
            )

    contact_dao = AssistantContactDAO(session)

    # 3. Check for duplicate active contact
    existing = contact_dao.get_contact_by_assistant_and_type(assistant_id, contact_type)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"An active {contact_type} contact already exists for this assistant.",
        )

    # Determine provider
    provider = None
    country_code = None
    if contact_type == "phone":
        provider = "twilio"
        country_code = contact_request.phone_country or "US"
    elif contact_type == "email":
        provider = contact_request.email_provider
    elif contact_type == "whatsapp":
        provider = "twilio"
    elif contact_type == "discord":
        provider = "discord"

    if is_byod:
        # ── BYOD path: no external provisioning, no billing ──
        created_value = contact_request.contact_value

        refreshed_session = _open_request_session(request)
        try:
            assistant_dao = AssistantDAO(refreshed_session)
            contact_dao = AssistantContactDAO(refreshed_session)

            assistant = assistant_dao.get_assistant_by_id(
                user_id=user_id,
                agent_id=assistant_id,
                organization_id=organization_id,
            )
            if not assistant:
                raise Exception("Assistant not found.")

            contact = contact_dao.upsert_assistant_contact(
                assistant_id=assistant_id,
                contact_type=contact_type,
                contact_value=created_value,
                provider=provider,
                country_code=country_code,
                provisioned_by="user",
            )

            refreshed_session.commit()
        except Exception as db_error:
            refreshed_session.rollback()
            logging.error(
                "Failed to save BYOD %s contact for assistant %s: %s",
                contact_type,
                assistant_id,
                db_error,
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to save {contact_type} contact: {str(db_error)}",
            )

        try:
            await reawaken_assistant(
                str(assistant_id),
            )
        except Exception as e:
            logging.warning(
                "Failed to reawaken assistant %s after BYOD contact creation: %s",
                assistant_id,
                e,
            )

        assistant = assistant_dao.get_assistant_by_id(
            user_id=user_id,
            agent_id=assistant_id,
            organization_id=organization_id,
        )
        try:
            response = InfoResponse(
                info=_build_assistant_read(assistant, refreshed_session),
            )
            refreshed_session.commit()
            return response
        except Exception:
            refreshed_session.rollback()
            raise
        finally:
            refreshed_session.close()

    # ── Platform-provisioned path (original) ──

    monthly_cost = contact_dao.get_contact_monthly_cost(
        contact_type,
        provider=provider,
        country_code=country_code,
    )
    one_time_cost = contact_dao.get_contact_one_time_cost(
        contact_type,
        provider=provider,
        country_code=country_code,
    )

    # 4. Credit check (skip in staging)
    if settings.charges_billing:
        try:
            billing_entity = get_billing_entity(session, user_id, organization_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail="Billing is not set up. Please add a payment method first.",
            )
        if one_time_cost > 0 and not billing_entity.has_sufficient_credits(
            one_time_cost,
        ):
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=(
                    f"Insufficient credits. Creating a {contact_type} contact "
                    f"requires ${one_time_cost} (setup fee)."
                ),
            )

    # 5. Provision external resource
    created_value = None

    try:
        if contact_type == "phone":
            phone_country = contact_request.phone_country or "US"
            phone_response = await create_phone_number(
                phone_country=phone_country,
            )
            if "detail" in phone_response:
                raise Exception(
                    f"Phone number creation failed: {phone_response['detail']}",
                )
            created_value = phone_response.get("phoneNumber")

        elif contact_type == "whatsapp":
            from orchestra.web.api.utils.assistant_infra import (
                assign_whatsapp_pool_number,
                register_whatsapp_sender,
            )

            pool_result = await assign_whatsapp_pool_number(
                assistant_id,
                session,
            )
            created_value = pool_result["pool_number"]

            # Register the Twilio sender (idempotent if already registered)
            await register_whatsapp_sender(
                created_value,
            )

        elif contact_type == "discord":
            from orchestra.web.api.utils.assistant_infra import (
                assign_discord_pool_bot,
                register_discord_bot,
            )

            pool_result = await assign_discord_pool_bot(
                assistant_id,
                session,
            )
            created_value = pool_result["pool_number"]

            await register_discord_bot(
                created_value,
                assistant_id,
                bot_token=pool_result.get("auth_token"),
            )

        if not created_value:
            raise Exception(f"Failed to provision {contact_type}: no value returned.")

    except HTTPException:
        raise
    except Exception as e:
        logging.error(
            f"Failed to provision {contact_type} for assistant {assistant_id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to provision {contact_type}",
        )

    # 6. Create AssistantContact row + update Assistant columns + deduct cost
    #    Wrap in try/except to rollback the external provisioning if DB fails.
    refreshed_session = _open_request_session(request)
    try:
        assistant_dao = AssistantDAO(refreshed_session)
        contact_dao = AssistantContactDAO(refreshed_session)

        # Re-fetch assistant with fresh session
        assistant = assistant_dao.get_assistant_by_id(
            user_id=user_id,
            agent_id=assistant_id,
            organization_id=organization_id,
        )
        if not assistant:
            raise Exception("Assistant no longer exists after provisioning.")

        # Create AssistantContact row
        contact = contact_dao.upsert_assistant_contact(
            assistant_id=assistant_id,
            contact_type=contact_type,
            contact_value=created_value,
            provider=provider,
            country_code=country_code,
        )
        contact.monthly_cost = monthly_cost

        # 7. Deduct one-time cost
        if settings.charges_billing and one_time_cost > 0:
            from orchestra.db.dao.billing_account_dao import BillingAccountDAO

            billing_entity = get_billing_entity(session, user_id, organization_id)
            BillingAccountDAO(session).deduct_credits(
                billing_entity.billing_account_id,
                float(one_time_cost),
                category="resources",
                assistant_id=assistant_id,
                user_id=user_id,
                organization_id=organization_id,
                description=f"Contact setup ({contact_type})",
                detail={
                    "event": "contact_setup",
                    "contact_id": contact.id if contact else None,
                    "contact_type": contact_type,
                    "provider": provider,
                },
            )

        refreshed_session.commit()

    except Exception as db_error:
        refreshed_session.rollback()
        logging.error(
            f"DB commit failed after provisioning {contact_type} for assistant "
            f"{assistant_id}: {db_error}. Rolling back external resource.",
        )
        # Rollback the external resource
        try:
            if contact_type == "phone":
                await delete_phone_number(
                    created_value,
                )
        except Exception as rollback_error:
            logging.error(
                f"RESOURCE LEAK: Failed to rollback {contact_type} "
                f"'{created_value}' after DB failure: {rollback_error}",
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save {contact_type} contact: {str(db_error)}",
        )
    # 8. Trigger reawaken so Unity picks up the new contact
    try:
        await reawaken_assistant(
            str(assistant_id),
        )
    except Exception as e:
        logging.warning(
            f"Failed to reawaken assistant {assistant_id} after contact creation: {e}",
        )

    # Re-fetch for response
    assistant = assistant_dao.get_assistant_by_id(
        user_id=user_id,
        agent_id=assistant_id,
        organization_id=organization_id,
    )
    try:
        response = InfoResponse(
            info=_build_assistant_read(assistant, refreshed_session),
        )
        refreshed_session.commit()
        return response
    except Exception:
        refreshed_session.rollback()
        raise
    finally:
        refreshed_session.close()


@router.get(
    "/assistant/{assistant_id}/contacts",
    response_model=InfoResponse[list[AssistantContactRead]],
    status_code=status.HTTP_200_OK,
    summary="List active contact details for an assistant",
    description="Returns all active (non-deleted) contact details with billing metadata.",
    tags=["Assistant Management"],
    responses={
        200: {"description": "Contact details returned successfully."},
        404: {"description": "Assistant not found."},
    },
)
async def list_assistant_contacts(
    assistant_id: int,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[list[AssistantContactRead]]:
    """
    List all active contact details for an assistant.

    Returns each contact with its billing metadata (monthly cost,
    status, grace period info, etc.).
    """
    user_id = request.state.user_id
    organization_id = getattr(request.state, "organization_id", None)
    assistant_dao = AssistantDAO(session)

    assistant = assistant_dao.get_assistant_by_id(
        user_id=user_id,
        agent_id=assistant_id,
        organization_id=organization_id,
    )
    if not assistant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )
    if _is_hidden_workspace_coordinator_for_user(assistant, user_id=user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view this assistant's contacts.",
        )

    if organization_id is not None:
        ra_dao = ResourceAccessDAO(session)
        if not ra_dao.check_user_permission(
            user_id,
            "assistant",
            assistant_id,
            "assistant:read",
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to view this assistant's contacts.",
            )

    contact_dao = AssistantContactDAO(session)
    contacts = contact_dao.get_active_contacts_for_assistant(assistant_id)

    # Backfill missing platform-managed contacts for the owner's Coordinator on
    # this single-resource read too (same self-heal as the list endpoint), so a
    # Coordinator predating the universal-contact rollout repairs itself however
    # its contacts are fetched.
    if assistant.is_coordinator and assistant.user_id == user_id:
        contacts_by_assistant: dict[int, list] = {assistant_id: list(contacts)}
        if _self_heal_coordinator_contacts(
            session,
            coordinators=[assistant],
            contacts_by_assistant=contacts_by_assistant,
            contact_dao=contact_dao,
        ):
            await notify_comms_discord_sync()
        contacts = contacts_by_assistant.get(assistant_id, contacts)

    contact_reads = [
        AssistantContactRead(
            id=c.id,
            assistant_id=c.assistant_id,
            contact_type=c.contact_type,
            contact_value=c.contact_value,
            provider=c.provider,
            provisioned_by=c.provisioned_by,
            country_code=c.country_code,
            status=c.status,
            monthly_cost=float(c.monthly_cost) if c.monthly_cost is not None else None,
            created_at=c.created_at,
            updated_at=c.updated_at,
            grace_period_started_at=c.grace_period_started_at,
        )
        for c in contacts
    ]
    return InfoResponse(info=contact_reads)


@router.post(
    "/assistant/{assistant_id}/connect",
    response_model=InfoResponse[ConnectResponse],
    status_code=status.HTTP_200_OK,
    summary="Get an OAuth URL to connect a user's account",
    description=(
        "Returns an OAuth authorization URL that the user visits to grant "
        "delegated access to suite features (email, calendar, drive, etc.). "
        "Supports both Google and Microsoft, initial connect and scope edits."
    ),
    tags=["Assistant Management"],
    responses={
        200: {"description": "OAuth URL generated successfully."},
        404: {"description": "Assistant not found."},
        422: {"description": "Missing OAuth configuration or invalid features."},
    },
)
async def connect_assistant_account(
    assistant_id: int,
    body: ConnectRequest,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[ConnectResponse]:
    """Build an OAuth authorization URL for BYOD suite access."""
    import hashlib
    import hmac as hmac_mod
    import json
    from urllib.parse import urlencode

    from orchestra.web.api.assistant.scopes import build_scope_string

    user_id = request.state.user_id
    organization_id = getattr(request.state, "organization_id", None)
    assistant_dao = AssistantDAO(session)

    assistant = assistant_dao.get_assistant_by_id(
        user_id=user_id,
        agent_id=assistant_id,
        organization_id=organization_id,
    )
    if not assistant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )
    if _is_hidden_workspace_coordinator_for_user(assistant, user_id=user_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )

    if organization_id is not None:
        ra_dao = ResourceAccessDAO(session)
        if not ra_dao.check_user_permission(
            user_id,
            "assistant",
            assistant_id,
            "assistant:write",
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to modify this assistant.",
            )

    provider = body.provider
    features = body.features
    redirect_after = body.redirect_after
    adapters_url = os.environ.get("UNITY_ADAPTERS_URL", "")

    # Detect scope reduction at the scope-set level (not feature-set), so that
    # shrinking a bundle's contents also triggers revoke. Without this, Google's
    # `include_granted_scopes=true` would resurface the dropped scopes from the
    # user's prior grant on the consent screen.
    scope_string = build_scope_string(provider, features)
    secret_dao = AssistantSecretDAO(session)
    scope_key = (
        "GOOGLE_GRANTED_SCOPES" if provider == "google" else "MICROSOFT_GRANTED_SCOPES"
    )
    current_scopes = secret_dao.get(assistant_id, scope_key)
    current_scope_set = set(current_scopes.split()) if current_scopes else set()
    new_scope_set = set(scope_string.split())
    is_scope_reduction = bool(current_scope_set - new_scope_set)

    # Google scope reduction: revoke the entire token first
    if provider == "google" and is_scope_reduction and adapters_url:
        import httpx

        admin_key = os.environ.get("ORCHESTRA_ADMIN_KEY", "")
        access_token = secret_dao.get(assistant_id, "GOOGLE_ACCESS_TOKEN")
        if access_token:
            async with httpx.AsyncClient(timeout=10) as http:
                await http.post(
                    f"{adapters_url}/google/revoke",
                    json={
                        "assistant_id": assistant_id,
                        "token": access_token,
                    },
                    headers={"Authorization": f"Bearer {admin_key}"},
                )

    # A Coordinator connects a personal workspace for *outbound* access only:
    # the CodeActActor reads and acts on the user's mailbox / calendar / drive /
    # Teams through the stored OAuth tokens. Its inbound contacts stay on the
    # platform-managed universal pools, so we must not register the personal
    # mailbox as a contact or wire an email / Teams watch — that would route the
    # user's own inbox into the ConversationManager. Regular assistants get the
    # full inbound wiring.
    wire_inbound = not assistant.is_coordinator
    state_dict: dict = {
        "assistant_id": assistant_id,
        "provider": provider,
        "features": features,
        "actions": {
            "register_email_contact": wire_inbound and "email" in features,
            "setup_email_watch": wire_inbound and "email" in features,
            "setup_teams_watch": wire_inbound and "teams" in features,
        },
        "redirect_after": redirect_after,
        "byod": True,
    }
    if settings.oauth_state_signing_key:
        canonical = json.dumps(state_dict, sort_keys=True)
        state_dict["_sig"] = hmac_mod.new(
            settings.oauth_state_signing_key.encode(),
            canonical.encode(),
            hashlib.sha256,
        ).hexdigest()

    encoded_state = base64.urlsafe_b64encode(
        json.dumps(state_dict).encode(),
    ).decode()

    if provider == "google":
        client_id = settings.google_oauth_client_id
        if not client_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Google OAuth is not configured on this deployment.",
            )
        params: dict = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": f"{adapters_url}/google/auth/callback",
            "scope": scope_string,
            "access_type": "offline",
            "prompt": "consent",
            "state": encoded_state,
        }
        if not is_scope_reduction:
            params["include_granted_scopes"] = "true"
        oauth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"

    else:
        client_id = settings.microsoft_byod_client_id
        if not client_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Microsoft BYOD OAuth is not configured on this deployment.",
            )
        params = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": f"{adapters_url}/microsoft/auth/callback",
            "scope": scope_string,
            "response_mode": "query",
            "prompt": "select_account",
            "state": encoded_state,
        }
        oauth_url = (
            f"https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
            f"?{urlencode(params)}"
        )

    return InfoResponse(info=ConnectResponse(oauth_url=oauth_url))


@router.delete(
    "/assistant/{assistant_id}/connect",
    response_model=InfoResponse,
    status_code=status.HTTP_200_OK,
    summary="Disconnect a user's connected account",
    description=(
        "Fully disconnects the BYOD OAuth account: revokes tokens, "
        "stops watches, clears secrets, and soft-deletes the BYOD contact."
    ),
    tags=["Assistant Management"],
    responses={
        200: {"description": "Account disconnected successfully."},
        404: {"description": "Assistant not found or no account connected."},
    },
)
async def disconnect_assistant_account(
    assistant_id: int,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse:
    import httpx

    user_id = request.state.user_id
    organization_id = getattr(request.state, "organization_id", None)
    assistant_dao = AssistantDAO(session)

    assistant = assistant_dao.get_assistant_by_id(
        user_id=user_id,
        agent_id=assistant_id,
        organization_id=organization_id,
    )
    if not assistant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )
    if _is_hidden_workspace_coordinator_for_user(assistant, user_id=user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view this assistant.",
        )

    if organization_id is not None:
        ra_dao = ResourceAccessDAO(session)
        if not ra_dao.check_user_permission(
            user_id,
            "assistant",
            assistant_id,
            "assistant:write",
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to modify this assistant.",
            )

    secret_dao = AssistantSecretDAO(session)
    google_scopes = secret_dao.get(assistant_id, "GOOGLE_GRANTED_SCOPES")
    ms_scopes = secret_dao.get(assistant_id, "MICROSOFT_GRANTED_SCOPES")

    if not google_scopes and not ms_scopes:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No connected account found for this assistant.",
        )

    adapters_url = os.environ.get("UNITY_ADAPTERS_URL", "")
    comms_url = os.environ.get("UNITY_COMMS_URL", "")
    admin_key = os.environ.get("ORCHESTRA_ADMIN_KEY", "")
    auth_headers = {"Authorization": f"Bearer {admin_key}"}

    contact_dao = AssistantContactDAO(session)
    contacts = contact_dao.get_active_contacts_for_assistant(assistant_id)
    byod_email = next(
        (
            c.contact_value
            for c in contacts
            if c.contact_type == "email" and c.provisioned_by == "user"
        ),
        None,
    )

    if google_scopes:
        if byod_email and comms_url:
            async with httpx.AsyncClient(timeout=10) as http:
                await http.request(
                    "DELETE",
                    f"{comms_url}/gmail/watch",
                    json={"primary_email": byod_email},
                    headers=auth_headers,
                )

        access_token = secret_dao.get(assistant_id, "GOOGLE_ACCESS_TOKEN")
        if access_token and adapters_url:
            async with httpx.AsyncClient(timeout=10) as http:
                await http.post(
                    f"{adapters_url}/google/revoke",
                    json={"assistant_id": assistant_id, "token": access_token},
                    headers=auth_headers,
                )
        for key in (
            "GOOGLE_ACCESS_TOKEN",
            "GOOGLE_REFRESH_TOKEN",
            "GOOGLE_TOKEN_EXPIRES_AT",
            "GOOGLE_GRANTED_SCOPES",
            "GOOGLE_ACCOUNT_EMAIL",
        ):
            secret_dao.delete(assistant_id, key)

    if ms_scopes:
        if byod_email and comms_url:
            async with httpx.AsyncClient(timeout=10) as http:
                await http.request(
                    "DELETE",
                    f"{comms_url}/outlook/watch",
                    json={"primary_email": byod_email},
                    headers=auth_headers,
                )
                await http.request(
                    "DELETE",
                    f"{comms_url}/teams/watch",
                    json={"primary_email": byod_email},
                    headers=auth_headers,
                )

        for key in (
            "MICROSOFT_ACCESS_TOKEN",
            "MICROSOFT_REFRESH_TOKEN",
            "MICROSOFT_TOKEN_EXPIRES_AT",
            "MICROSOFT_GRANTED_SCOPES",
            "MICROSOFT_TOKEN_SOURCE",
            "MICROSOFT_ACCOUNT_EMAIL",
        ):
            secret_dao.delete(assistant_id, key)

    for c in contacts:
        if c.contact_type == "email" and c.provisioned_by == "user":
            contact_dao.soft_delete_assistant_contact(
                assistant_id=assistant_id,
                contact_type="email",
            )
            break

    from orchestra.provider_triggers.workspace_connection_facade import (
        deactivate_workspace_trigger_connections,
    )

    deactivate_workspace_trigger_connections(session, assistant_id=assistant_id)
    session.commit()

    try:
        await reawaken_assistant(
            str(assistant_id),
        )
    except Exception as e:
        logging.warning(
            f"Failed to reawaken assistant {assistant_id} after disconnect: {e}",
        )

    return InfoResponse(info={"status": "disconnected"})


@router.get(
    "/assistant/{assistant_id}/granted-features",
    response_model=InfoResponse[GrantedFeaturesResponse],
    status_code=status.HTTP_200_OK,
    summary="Get granted suite features for an assistant",
    description=(
        "Returns the OAuth provider and the suite features whose scopes "
        "have been fully granted for this assistant."
    ),
    tags=["Assistant Management"],
    responses={
        200: {"description": "Granted features retrieved."},
        404: {"description": "Assistant not found."},
    },
)
async def get_granted_features(
    assistant_id: int,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[GrantedFeaturesResponse]:
    from orchestra.web.api.assistant.scopes import (
        REQUIRED_FEATURES,
        map_scopes_to_features,
    )

    user_id = request.state.user_id
    organization_id = getattr(request.state, "organization_id", None)
    assistant_dao = AssistantDAO(session)

    assistant = assistant_dao.get_assistant_by_id(
        user_id=user_id,
        agent_id=assistant_id,
        organization_id=organization_id,
    )
    if not assistant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )
    if _is_hidden_workspace_coordinator_for_user(assistant, user_id=user_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )

    if organization_id is not None:
        ra_dao = ResourceAccessDAO(session)
        if not ra_dao.check_user_permission(
            user_id,
            "assistant",
            assistant_id,
            "assistant:read",
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to view this assistant.",
            )

    secret_dao = AssistantSecretDAO(session)
    google_scopes = secret_dao.get(assistant_id, "GOOGLE_GRANTED_SCOPES")
    ms_scopes = secret_dao.get(assistant_id, "MICROSOFT_GRANTED_SCOPES")

    if google_scopes:
        return InfoResponse(
            info=GrantedFeaturesResponse(
                provider="google",
                features=map_scopes_to_features("google", google_scopes),
                required_features=REQUIRED_FEATURES["google"],
                connected_account_email=secret_dao.get(
                    assistant_id,
                    "GOOGLE_ACCOUNT_EMAIL",
                )
                or None,
            ),
        )
    if ms_scopes:
        return InfoResponse(
            info=GrantedFeaturesResponse(
                provider="microsoft",
                features=map_scopes_to_features("microsoft", ms_scopes),
                required_features=REQUIRED_FEATURES["microsoft"],
                connected_account_email=secret_dao.get(
                    assistant_id,
                    "MICROSOFT_ACCOUNT_EMAIL",
                )
                or None,
            ),
        )

    return InfoResponse(info=GrantedFeaturesResponse())


# =========================================================================
# Workspace file access (Drive / SharePoint / OneDrive allowlist)
# =========================================================================


def _load_assistant_for_file_access(
    session: Session,
    request: Request,
    assistant_id: int,
    *,
    write: bool,
):
    """Load the assistant and enforce read/write RBAC for file-access ops."""
    user_id = request.state.user_id
    organization_id = getattr(request.state, "organization_id", None)
    assistant_dao = AssistantDAO(session)
    assistant = assistant_dao.get_assistant_by_id(
        user_id=user_id,
        agent_id=assistant_id,
        organization_id=organization_id,
    )
    if not assistant or _is_hidden_workspace_coordinator_for_user(
        assistant,
        user_id=user_id,
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )
    if organization_id is not None:
        ra_dao = ResourceAccessDAO(session)
        permission = "assistant:write" if write else "assistant:read"
        if not ra_dao.check_user_permission(
            user_id,
            "assistant",
            assistant_id,
            permission,
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to access this assistant.",
            )
    return assistant


# Drive/item identifiers are base64url-style tokens (Microsoft) or opaque ids
# (Google). They must never carry URL-structural characters, since they are
# interpolated into the gateway request path; anything outside this allowlist
# could redirect the request to a different gateway route.
_WORKSPACE_ID_RE = re.compile(r"^[A-Za-z0-9!$._~=+-]{1,1024}$")


def _validate_workspace_id(value: str, field: str) -> str:
    """Reject workspace identifiers that could escape the intended gateway path."""
    if not _WORKSPACE_ID_RE.fullmatch(value):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid {field}.",
        )
    return value


async def _gateway_browse(provider: str, path: str, params: dict) -> dict:
    """Proxy an unfiltered browse call to the Unity gateway channel."""
    import httpx

    comms_url = os.environ.get("UNITY_COMMS_URL", "")
    if not comms_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Workspace browsing is not available on this deployment.",
        )
    admin_key = os.environ.get("ORCHESTRA_ADMIN_KEY", "")
    base = "drive" if provider == "google" else "sharepoint"
    url = f"{comms_url}/{base}/{path}"
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            resp = await http.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {admin_key}"},
            )
    except httpx.HTTPError as exc:
        logging.error("Workspace gateway request to %s failed: %s", url, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Workspace gateway is unreachable.",
        )
    if resp.status_code >= 400:
        logging.error(
            "Workspace gateway %s returned %s: %s",
            url,
            resp.status_code,
            resp.text[:500],
        )
        raise HTTPException(
            status_code=resp.status_code,
            detail="Failed to browse workspace files.",
        )
    try:
        return resp.json()
    except ValueError as exc:
        logging.error(
            "Workspace gateway %s returned non-JSON body: %s",
            url,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Workspace gateway returned an invalid response.",
        )


def _ms_node(raw: dict, drive_id: str) -> WorkspaceFileNode:
    """Normalize a Microsoft Graph item dict into a WorkspaceFileNode."""
    return WorkspaceFileNode(
        drive_id=drive_id,
        item_id=str(raw.get("id") or ""),
        name=raw.get("name") or "",
        kind="folder" if raw.get("type") == "folder" else "file",
        mime_type=raw.get("mime_type"),
        web_url=raw.get("web_url"),
        parent_id=None,
    )


def _google_node(raw: dict) -> WorkspaceFileNode:
    """Build a WorkspaceFileNode from the already-normalized Drive channel dict."""
    return WorkspaceFileNode(
        drive_id=str(raw.get("drive_id") or ""),
        item_id=str(raw.get("item_id") or ""),
        name=raw.get("name") or "",
        kind=raw.get("kind") or "file",
        mime_type=raw.get("mime_type"),
        web_url=raw.get("web_url"),
        parent_id=raw.get("parent_id"),
    )


def _require_file_provider(provider: str) -> str:
    if provider not in ("google", "microsoft"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="provider must be 'google' or 'microsoft'.",
        )
    return provider


@router.get(
    "/assistant/{assistant_id}/workspace-files/roots",
    response_model=InfoResponse[WorkspaceFileListResponse],
    summary="List the connected account's top-level drives/corpora",
    tags=["Assistant Management"],
)
async def list_workspace_file_roots(
    assistant_id: int,
    provider: str,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[WorkspaceFileListResponse]:
    _require_file_provider(provider)
    _load_assistant_for_file_access(session, request, assistant_id, write=False)

    if provider == "google":
        data = await _gateway_browse(
            "google",
            "roots",
            {"assistant_id": assistant_id},
        )
        items = [_google_node(n) for n in data.get("roots", [])]
    else:
        data = await _gateway_browse(
            "microsoft",
            "drives",
            {"assistant_id": assistant_id},
        )
        items = [
            WorkspaceFileNode(
                drive_id=str(d.get("id") or ""),
                item_id="root",
                name=d.get("name") or "Drive",
                kind="drive",
                web_url=d.get("web_url"),
            )
            for d in data.get("drives", [])
        ]
    return InfoResponse(info=WorkspaceFileListResponse(items=items))


@router.get(
    "/assistant/{assistant_id}/workspace-files/children",
    response_model=InfoResponse[WorkspaceFileListResponse],
    summary="List the children of a folder in the connected account",
    tags=["Assistant Management"],
)
async def list_workspace_file_children(
    assistant_id: int,
    provider: str,
    drive_id: str,
    item_id: str,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[WorkspaceFileListResponse]:
    _require_file_provider(provider)
    _load_assistant_for_file_access(session, request, assistant_id, write=False)
    drive_id = _validate_workspace_id(drive_id, "drive_id")
    if item_id and item_id != "root":
        item_id = _validate_workspace_id(item_id, "item_id")

    if provider == "google":
        data = await _gateway_browse(
            "google",
            "children",
            {"assistant_id": assistant_id, "drive_id": drive_id, "item_id": item_id},
        )
        items = [_google_node(n) for n in data.get("items", [])]
    else:
        params: dict = {"assistant_id": assistant_id}
        if item_id and item_id != "root":
            params["item_id"] = item_id
        data = await _gateway_browse("microsoft", f"drives/{drive_id}/items", params)
        items = [_ms_node(n, drive_id) for n in data.get("items", [])]
    return InfoResponse(info=WorkspaceFileListResponse(items=items))


@router.get(
    "/assistant/{assistant_id}/workspace-files/policy",
    response_model=InfoResponse[WorkspaceFilePolicy],
    summary="Get the file-access allowlist for a provider",
    tags=["Assistant Management"],
)
async def get_workspace_file_policy(
    assistant_id: int,
    provider: str,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[WorkspaceFilePolicy]:
    _require_file_provider(provider)
    _load_assistant_for_file_access(session, request, assistant_id, write=False)

    dao = AssistantWorkspaceFileAccessDAO(session)
    row = dao.get(assistant_id, provider)
    if not row:
        return InfoResponse(
            info=WorkspaceFilePolicy(
                provider=provider,
                default_allow=False,
                decisions=[],
            ),
        )
    return InfoResponse(
        info=WorkspaceFilePolicy(
            provider=provider,
            default_allow=row.default_allow,
            decisions=row.decisions or [],
        ),
    )


@router.patch(
    "/assistant/{assistant_id}/workspace-files/policy",
    response_model=InfoResponse[WorkspaceFilePolicy],
    summary="Replace the file-access allowlist for a provider",
    tags=["Assistant Management"],
)
async def update_workspace_file_policy(
    assistant_id: int,
    provider: str,
    body: WorkspaceFilePolicyUpdate,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[WorkspaceFilePolicy]:
    _require_file_provider(provider)
    _load_assistant_for_file_access(session, request, assistant_id, write=True)

    dao = AssistantWorkspaceFileAccessDAO(session)
    decisions = [d.model_dump() for d in body.decisions]
    row = dao.upsert(
        agent_id=assistant_id,
        provider=provider,
        default_allow=body.default_allow,
        decisions=decisions,
    )
    session.commit()

    # Re-awaken so the runtime re-syncs the allowlist before its next file op.
    try:
        await reawaken_assistant(str(assistant_id))
    except Exception:
        logging.warning(
            "Failed to reawaken assistant %s after file-policy update",
            assistant_id,
            exc_info=True,
        )

    return InfoResponse(
        info=WorkspaceFilePolicy(
            provider=provider,
            default_allow=row.default_allow,
            decisions=row.decisions or [],
        ),
    )


def _collect_workspace_file_access(
    session: Session,
    assistant_id: int,
) -> InfoResponse[WorkspaceFileAccessAdminResponse]:
    """Aggregate every provider's file-access policy for one assistant."""
    dao = AssistantWorkspaceFileAccessDAO(session)
    policies: list[WorkspaceFilePolicy] = []
    for provider in ("google", "microsoft"):
        row = dao.get(assistant_id, provider)
        if row:
            policies.append(
                WorkspaceFilePolicy(
                    provider=provider,
                    default_allow=row.default_allow,
                    decisions=row.decisions or [],
                ),
            )
    return InfoResponse(info=WorkspaceFileAccessAdminResponse(policies=policies))


@admin_router.get(
    "/assistant/{assistant_id}/workspace-file-access",
    response_model=InfoResponse[WorkspaceFileAccessAdminResponse],
    summary="Admin read of all file-access policies for an assistant",
    description=(
        "Returns every configured per-provider file-access allowlist for the "
        "assistant. Used by the assistant runtime to mirror the allowlist into "
        "its enforcement layer."
    ),
    tags=["Assistant Management"],
)
async def admin_get_workspace_file_access(
    assistant_id: int,
    session: Session = Depends(get_db_session),
) -> InfoResponse[WorkspaceFileAccessAdminResponse]:
    return _collect_workspace_file_access(session, assistant_id)


@router.get(
    "/assistant/{assistant_id}/workspace-file-access",
    response_model=InfoResponse[WorkspaceFileAccessAdminResponse],
    summary="Read all file-access policies for an owned assistant",
    description=(
        "Ownership-scoped equivalent of the admin aggregate read. Returns "
        "every configured per-provider file-access allowlist for the "
        "assistant. Used by the assistant runtime to mirror the allowlist "
        "into its enforcement layer."
    ),
    tags=["Assistant Management"],
)
async def get_workspace_file_access(
    assistant_id: int,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[WorkspaceFileAccessAdminResponse]:
    require_owned_assistant(request, assistant_id, session)
    return _collect_workspace_file_access(session, assistant_id)


@router.get(
    "/assistant/{assistant_id}/secrets",
    summary="Read all secrets for an owned assistant",
    description=(
        "Ownership-scoped equivalent of the admin fleet read with "
        "``from_fields=secrets``: returns the assistant's full secrets map. "
        "Used by the assistant runtime to hydrate its secret manager."
    ),
    tags=["Assistant Management"],
    include_in_schema=False,
)
async def get_assistant_secrets(
    assistant_id: int,
    request: Request,
    session: Session = Depends(get_db_session),
) -> dict:
    require_owned_assistant(request, assistant_id, session)
    secret_dao = AssistantSecretDAO(session)
    return {"secrets": secret_dao.get_all(assistant_id)}


@router.get(
    "/assistant/{assistant_id}/desktop-filesync-key",
    summary="Read desktop file-sync SSH keys for an owned assistant",
    description=(
        "Ownership-scoped read of the assistant desktop file-sync private "
        "key plus the per-user desktop key map (keyed by owner_user_id). "
        "Used by the assistant runtime's file-sync layer instead of the "
        "admin fleet read."
    ),
    tags=["Assistant Management"],
    include_in_schema=False,
)
async def get_assistant_desktop_filesync_keys(
    assistant_id: int,
    request: Request,
    session: Session = Depends(get_db_session),
) -> dict:
    assistant = require_owned_assistant(request, assistant_id, session)
    desktop_dao = DesktopDAO(session)
    user_desktop_filesync_keys = {
        link.owner_user_id: link.filesync_sshkey
        for link, _desktop in desktop_dao.list_links_for_assistant(assistant.agent_id)
        if link.filesync_sshkey
    }
    return {
        "desktop_filesync_sshkey": assistant.desktop_filesync_sshkey,
        "user_desktop_filesync_keys": user_desktop_filesync_keys,
    }


# =========================================================================
# Secret CRUD (used by Communication to persist OAuth tokens)
# =========================================================================


@router.post(
    "/assistant/{assistant_id}/secret",
    response_model=InfoResponse,
    status_code=status.HTTP_200_OK,
    summary="Create a secret for an assistant",
    tags=["Assistant Management"],
    responses={
        200: {"description": "Secret created."},
        404: {"description": "Assistant not found."},
        409: {
            "description": "Secret with that name already exists (use PUT to update).",
        },
    },
)
async def create_assistant_secret(
    assistant_id: int,
    body: SecretCreate,
    request: Request,
    session: Session = Depends(get_db_session),
):
    user_id = request.state.user_id
    organization_id = getattr(request.state, "organization_id", None)
    assistant_dao = AssistantDAO(session)
    assistant = assistant_dao.get_assistant_by_id(
        user_id=user_id,
        agent_id=assistant_id,
        organization_id=organization_id,
    )
    if not assistant:
        raise HTTPException(status_code=404, detail="Assistant not found.")
    if _is_hidden_workspace_coordinator_for_user(assistant, user_id=user_id):
        raise HTTPException(status_code=404, detail="Assistant not found.")

    if organization_id is not None:
        ra_dao = ResourceAccessDAO(session)
        if not ra_dao.check_user_permission(
            user_id,
            "assistant",
            assistant_id,
            "assistant:write",
        ):
            raise HTTPException(
                status_code=403,
                detail="You do not have permission to modify this assistant.",
            )

    secret_dao = AssistantSecretDAO(session)
    existing = secret_dao.get(assistant_id, body.secret_name)
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Secret '{body.secret_name}' already exists. Use PUT to update.",
        )
    secret_dao.upsert(
        assistant.user_id,
        assistant_id,
        body.secret_name,
        body.secret_value,
    )
    if body.secret_name.startswith(("GOOGLE_", "MICROSOFT_")):
        from orchestra.provider_triggers.workspace_connection_facade import (
            ensure_workspace_trigger_connections,
        )

        ensure_workspace_trigger_connections(session, assistant_id=assistant_id)
    session.commit()
    # Reactive narration: fire-and-forget tell the Coordinator a
    # secret just landed so it can comment in-conversation. The
    # helper gates on the Coordinator's onboarding mode and resolves
    # workspace OAuth (GOOGLE_*/MICROSOFT_* prefixes) vs. generic
    # integration based on the secret name. Failures are swallowed
    # inside the helper so user-facing requests never regress.
    await emit_secret_landed_event(
        session,
        assistant=assistant,
        secret_name=body.secret_name,
        is_create=True,
    )
    return InfoResponse(info={"secret_name": body.secret_name, "status": "created"})


@router.put(
    "/assistant/{assistant_id}/secret/{secret_name}",
    response_model=InfoResponse,
    status_code=status.HTTP_200_OK,
    summary="Update an existing secret",
    tags=["Assistant Management"],
    responses={
        200: {"description": "Secret updated."},
        404: {"description": "Assistant or secret not found."},
    },
)
async def update_assistant_secret(
    assistant_id: int,
    secret_name: str,
    body: SecretUpdate,
    request: Request,
    session: Session = Depends(get_db_session),
):
    user_id = request.state.user_id
    organization_id = getattr(request.state, "organization_id", None)
    assistant_dao = AssistantDAO(session)
    assistant = assistant_dao.get_assistant_by_id(
        user_id=user_id,
        agent_id=assistant_id,
        organization_id=organization_id,
    )
    if not assistant:
        raise HTTPException(status_code=404, detail="Assistant not found.")
    if _is_hidden_workspace_coordinator_for_user(assistant, user_id=user_id):
        raise HTTPException(status_code=404, detail="Assistant not found.")

    if organization_id is not None:
        ra_dao = ResourceAccessDAO(session)
        if not ra_dao.check_user_permission(
            user_id,
            "assistant",
            assistant_id,
            "assistant:write",
        ):
            raise HTTPException(
                status_code=403,
                detail="You do not have permission to modify this assistant.",
            )

    secret_dao = AssistantSecretDAO(session)
    existing = secret_dao.get(assistant_id, secret_name)
    if existing is None:
        raise HTTPException(
            status_code=404,
            detail=f"Secret '{secret_name}' not found.",
        )
    secret_dao.upsert(
        assistant.user_id,
        assistant_id,
        secret_name,
        body.secret_value,
    )
    if secret_name.startswith(("GOOGLE_", "MICROSOFT_")):
        from orchestra.provider_triggers.workspace_connection_facade import (
            ensure_workspace_trigger_connections,
        )

        ensure_workspace_trigger_connections(session, assistant_id=assistant_id)
    session.commit()
    # See sibling note on the POST handler — same narration emit, same
    # gating semantics. Updates pass ``is_create=False`` so the workspace
    # OAuth refresh path (which overwrites the token row on a schedule) does
    # not re-narrate the connection; only the first-connect create does.
    await emit_secret_landed_event(
        session,
        assistant=assistant,
        secret_name=secret_name,
        is_create=False,
    )
    return InfoResponse(info={"secret_name": secret_name, "status": "updated"})


@router.delete(
    "/assistant/{assistant_id}/secret/{secret_name}",
    response_model=InfoResponse,
    status_code=status.HTTP_200_OK,
    summary="Delete a secret",
    tags=["Assistant Management"],
    responses={
        200: {"description": "Secret deleted."},
        404: {"description": "Assistant or secret not found."},
    },
)
async def delete_assistant_secret(
    assistant_id: int,
    secret_name: str,
    request: Request,
    session: Session = Depends(get_db_session),
):
    user_id = request.state.user_id
    organization_id = getattr(request.state, "organization_id", None)
    assistant_dao = AssistantDAO(session)
    assistant = assistant_dao.get_assistant_by_id(
        user_id=user_id,
        agent_id=assistant_id,
        organization_id=organization_id,
    )
    if not assistant:
        raise HTTPException(status_code=404, detail="Assistant not found.")
    if _is_hidden_workspace_coordinator_for_user(assistant, user_id=user_id):
        raise HTTPException(status_code=404, detail="Assistant not found.")

    if organization_id is not None:
        ra_dao = ResourceAccessDAO(session)
        if not ra_dao.check_user_permission(
            user_id,
            "assistant",
            assistant_id,
            "assistant:write",
        ):
            raise HTTPException(
                status_code=403,
                detail="You do not have permission to modify this assistant.",
            )

    secret_dao = AssistantSecretDAO(session)
    removed = secret_dao.delete(assistant_id, secret_name)
    if not removed:
        raise HTTPException(
            status_code=404,
            detail=f"Secret '{secret_name}' not found.",
        )
    session.commit()
    return InfoResponse(info={"secret_name": secret_name, "status": "deleted"})


@router.put(
    "/assistant/{assistant_id}/contact",
    response_model=InfoResponse[AssistantRead],
    status_code=status.HTTP_200_OK,
    summary="Update contact metadata",
    description=(
        "Updates metadata on an existing contact. "
        "Changing the actual provisioned resource requires delete + create."
    ),
    tags=["Assistant Management"],
    responses={
        200: {"description": "Contact updated successfully."},
        404: {"description": "Assistant or contact not found."},
    },
)
async def update_assistant_contact(
    assistant_id: int,
    contact_update: AssistantContactUpdate,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[AssistantRead]:
    """
    Update metadata on an existing contact.

    Only ``metadata`` can be changed via this endpoint. User-side contact
    info is managed on the user profile. Changing the actual provisioned
    resource (phone number, email address, etc.) requires delete + create.
    """
    user_id = request.state.user_id
    organization_id = getattr(request.state, "organization_id", None)
    assistant_dao = AssistantDAO(session)

    assistant = assistant_dao.get_assistant_by_id(
        user_id=user_id,
        agent_id=assistant_id,
        organization_id=organization_id,
    )
    if not assistant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )
    if _is_hidden_workspace_coordinator_for_user(assistant, user_id=user_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )

    # Permission check for org assistants
    if organization_id is not None:
        resource_access_dao = ResourceAccessDAO(session)
        has_permission = resource_access_dao.check_user_permission(
            user_id,
            "assistant",
            assistant_id,
            "assistant:write",
        )
        if not has_permission:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to modify this assistant.",
            )

    contact_type = contact_update.contact_type
    contact_dao = AssistantContactDAO(session)
    contact = contact_dao.get_contact_by_assistant_and_type(assistant_id, contact_type)
    if not contact:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No active {contact_type} contact found for this assistant.",
        )

    # Update metadata (merge with existing)
    if contact_update.metadata is not None:
        existing_meta = contact.metadata_ or {}
        contact.metadata_ = {**existing_meta, **contact_update.metadata}

    session.commit()
    session.refresh(assistant)

    # Trigger reawaken so Unity picks up the metadata change
    try:
        await reawaken_assistant(
            str(assistant_id),
        )
    except Exception as e:
        logging.warning(
            f"Failed to reawaken assistant {assistant_id} after contact update: {e}",
        )

    return InfoResponse(
        info=_build_assistant_read(assistant, session, requesting_user_id=user_id),
    )


async def _cleanup_after_assistant_delete(
    session_factory,
    cleanup_task_ids: list[int],
    assistant_id: int,
) -> None:
    """Run one immediate post-delete cleanup pass in the background.

    Runs as a FastAPI BackgroundTask so the user gets an immediate response.
    A single best-effort drain of the durable cleanup queue; unfinished work
    remains for the scheduled cleanup worker (GHA ``cleanup-assistant-runtime``).
    Holding the request concurrency slot for a multi-minute poll loop is unsafe
    under Cloud Run CPU throttling and wastes capacity after the client already
    received 200.
    """
    if not cleanup_task_ids:
        return

    bg_session = session_factory()
    try:
        result = await process_assistant_cleanup_tasks(
            bg_session,
            task_ids=cleanup_task_ids,
        )
        logging.info(
            "Assistant %s cleanup pass result=%s",
            assistant_id,
            result,
        )
        if result.get("errors"):
            logging.error(
                "Runtime cleanup task issues for deleted assistant %s: %s",
                assistant_id,
                result["errors"],
            )
    except Exception as exc:
        logging.error(
            "Background runtime cleanup failed for assistant %s: %s",
            assistant_id,
            exc,
        )
    finally:
        bg_session.close()


@router.delete(
    "/assistant/{assistant_id}",
    status_code=status.HTTP_200_OK,
    summary="Delete an assistant",
    description="Deletes a specific assistant by ID for the authenticated user.",
    tags=["Assistant Management"],
    responses={
        200: {
            "description": "Assistant deleted successfully",
            "content": {
                "application/json": {
                    "example": {"info": "Assistant deleted successfully"},
                },
            },
        },
        404: {
            "description": "Assistant Not Found",
            "content": {
                "application/json": {"example": {"detail": "Assistant not found."}},
            },
        },
    },
)
async def delete_assistant(
    assistant_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_db_session),
) -> InfoResponse[str]:
    """
    Delete an assistant: purge its database footprint inline, defer only the
    external (network-bound) teardown to the durable worker.

    The assistant's heavy-table rows and owned context tree are deleted
    synchronously via :func:`purge_assistant_owner`, a single owner-scoped purge
    that is proportional to *this* assistant's data (indexed on
    ``(project_id, owner_key)``) -- fast enough to run in-request. Only the
    runtime shutdown, contact deprovisioning, and assistant-scoped GCS cleanup
    are handed to the durable ``AssistantCleanupTask`` queue, which is retried
    out of band; a terminally failed task is visible via the admin cleanup
    endpoint.
    """
    dao = AssistantDAO(session)
    organization_id = getattr(request.state, "organization_id", None)
    cleanup_errors: list[str] = []

    try:
        assistant = dao.get_assistant_by_id(
            user_id=request.state.user_id,
            agent_id=assistant_id,
            organization_id=organization_id,
        )
        if not assistant:
            logging.warning(
                f"Assistant with ID {assistant_id} not found for user {request.state.user_id}.",
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Assistant not found.",
            )

        if assistant.is_coordinator:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="cannot_delete_coordinator",
            )

        if organization_id is not None:
            resource_access_dao = ResourceAccessDAO(session)
            has_permission = resource_access_dao.check_user_permission(
                request.state.user_id,
                "assistant",
                assistant_id,
                "assistant:delete",
            )
            if not has_permission:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You do not have permission to delete this assistant.",
                )

        await purge_assistant_memberships(session, assistant=assistant)

        # Deprovision contacts inline so successes can be soft-deleted in the
        # same transaction.  Failures are captured in cleanup_spec and persisted
        # in the durable task queue below for background retry. The assistant's
        # database footprint is purged synchronously below; the queued task
        # carries only the external (runtime / contacts / GCS) teardown.
        contact_dao = AssistantContactDAO(session)
        active_contacts = contact_dao.get_active_contacts_for_assistant(assistant_id)
        assistant_user_id = assistant.user_id
        cleanup_spec = build_cleanup_spec_from_assistant(
            assistant,
            active_contacts,
        )

        contact_result = await deprovision_assistant_contacts(
            session,
            [cleanup_spec],
            soft_delete_successes=True,
        )
        cleanup_errors.extend(contact_result["errors"])

        cleanup_task_ids = [
            task.id
            for task in enqueue_cleanup_tasks(
                session,
                [cleanup_spec],
                source_flow=CleanupSource.ASSISTANT_DELETE,
            )
        ]

        # Release provider accounts before the assistant row goes. Connection
        # rows have no cascade, so once the assistant is gone nothing can
        # reach them — no surface lists them, no disconnect can be issued —
        # and the accounts stay live at the provider forever.
        from orchestra.web.api.integrations.operations import (
            release_assistant_connections,
        )

        release_assistant_connections(session, assistant_id=assistant_id)

        dao.delete_assistant(
            user_id=request.state.user_id,
            agent_id=assistant_id,
            organization_id=organization_id,
        )

        # Purge the assistant's database footprint (heavy-table rows + owned
        # context tree) synchronously and indexed, in the same transaction as
        # the row delete. This is proportional to the assistant's own data, not
        # the shared Assistants project, so it stays fast even for data-heavy
        # assistants.
        purge_assistant_owner(
            session,
            assistant_id=assistant_id,
            user_id=assistant_user_id,
            organization_id=organization_id,
        )
        session.commit()

        # Schedule an immediate post-response drain of the durable cleanup task
        # queue (external runtime / contact / GCS teardown only).
        session_factory = request.app.state.db_session_factory
        background_tasks.add_task(
            _cleanup_after_assistant_delete,
            session_factory,
            cleanup_task_ids,
            assistant_id,
        )

        response_msg = "Assistant deleted successfully"
        if cleanup_errors:
            response_msg += f" (with some cleanup issues: {'; '.join(cleanup_errors)})"
        return InfoResponse(info=response_msg)
    except HTTPException:
        logging.warning(
            f"Rolling back transaction due to HTTPException during deletion of assistant {assistant_id}.",
        )
        session.rollback()
        raise
    except Exception as e:
        logging.error(
            f"An unexpected error occurred during deletion of assistant {assistant_id}. Rolling back.",
            exc_info=True,
        )
        session.rollback()
        final_error_detail = f"Error deleting assistant: {str(e)}"
        if cleanup_errors:
            final_error_detail += (
                f" | Cleanup issues prior to full rollback: {'; '.join(cleanup_errors)}"
            )

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=final_error_detail,
        )


@router.patch(
    "/assistant/{assistant_id}/config",
    response_model=InfoResponse[AssistantRead],
    status_code=status.HTTP_200_OK,
    summary="Update assistant configuration",
    description="Updates the configuration parameters of an existing assistant. Profile photo cannot be updated via this endpoint.",
    tags=["Assistant Management"],
    responses={
        200: {
            "description": "Assistant configuration updated successfully",
            "content": {
                "application/json": {
                    "example": {
                        "info": {
                            "agent_id": "123",
                            "first_name": "Alice",
                            "surname": "Smith",
                            "age": 25,
                            "weekly_limit": 45.0,
                            "max_parallel": 4,
                            "about": "Award-winning mathematician specializing in algorithm development",
                            "phone": "+1-555-987-6543",
                            "email": "alice.smith@example.com",
                            "nationality": "United States",
                            "profile_photo": "https://example.com/photos/alice.jpg",
                            "profile_video": "https://example.com/videos/alice.mp4",
                            "voice_id": "bf0a246a-8642-498a-9950-80c35e9276b5",
                            "voice_provider": "cartesia",
                            "phone_country": "US",
                            "timezone": "America/New_York",
                            "created_at": "2025-04-25T12:00:00Z",
                            "updated_at": "2025-04-25T14:30:00Z",
                        },
                    },
                },
            },
        },
        404: {
            "description": "Assistant Not Found",
            "content": {
                "application/json": {"example": {"detail": "Assistant not found."}},
            },
        },
        422: {
            "description": "Validation Error",
            "content": {
                "application/json": {
                    "example": {
                        "detail": [
                            {
                                "loc": ["body", "email"],
                                "msg": "value is not a valid email address",
                                "type": "value_error.email",
                            },
                        ],
                    },
                },
            },
        },
    },
)
async def update_assistant_config(
    assistant_id: int,
    update: AssistantUpdate,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[AssistantRead]:
    """
    Update about, phone, email, weekly_limit, and/or max_parallel for an existing assistant.

    Allows partial updates to an assistant's configuration. Only the fields
    provided in the request will be updated, while others remain unchanged.
    """
    user_id = request.state.user_id
    organization_id = getattr(request.state, "organization_id", None)
    user_dao = UserDAO(session)
    assistant_dao = AssistantDAO(session)
    bucket_service = create_bucket_service()

    # Store the old photo URL before the update
    old_photo_url = None
    is_photo_changing = False
    old_video_url = None
    is_video_changing = False

    # Check assistant existence before any updates
    existing_assistant = assistant_dao.get_assistant_by_id(
        user_id=request.state.user_id,
        agent_id=assistant_id,
        organization_id=organization_id,
    )
    if not existing_assistant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )
    if _is_hidden_workspace_coordinator_for_user(
        existing_assistant,
        user_id=user_id,
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )

    # For org assistants, check assistant:write permission
    if organization_id is not None:
        resource_access_dao = ResourceAccessDAO(session)
        has_permission = resource_access_dao.check_user_permission(
            user_id,
            "assistant",
            assistant_id,
            "assistant:write",
        )
        if not has_permission:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to modify this assistant.",
            )

    # Determine if the photo is being updated before making changes
    old_photo_url = existing_assistant.profile_photo
    is_photo_changing = (
        update.profile_photo is not None and update.profile_photo != old_photo_url
    )
    old_video_url = existing_assistant.profile_video
    is_video_changing = (
        update.profile_video is not None and update.profile_video != old_video_url
    )

    try:
        weekly_limit: Optional[Decimal] = None
        if update.weekly_limit is not None:
            weekly_limit = Decimal(update.weekly_limit)

        # NOTE: Contact provisioning (phone, email, WhatsApp) has been removed
        # from this endpoint.  Use POST /assistant/{id}/contact instead.
        # Deprecated contact fields in the request body are silently excluded.
        _DEPRECATED_CONTACT_FIELDS = {
            "email",
            "phone",
            "user_phone",
            "phone_country",
            "user_whatsapp_number",
            "create_infra",
        }

        update_data = update.model_dump(exclude_unset=True)
        # Remove deprecated contact fields
        for field_name in _DEPRECATED_CONTACT_FIELDS:
            update_data.pop(field_name, None)
        # A single-player coordinator's name is the fixed shared identity;
        # renaming happens only through the multiplayer flip. Multiplayer
        # twins rename freely like hired teammates — except back to the
        # reserved shared default, which would recreate the ambiguity the
        # flip ceremony exists to prevent.
        if existing_assistant.is_private_coordinator and any(
            update_data.get(field) not in (None, getattr(existing_assistant, field))
            for field in ("first_name", "surname")
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="coordinator_name_is_platform_managed",
            )
        if (
            not existing_assistant.is_private_coordinator
            and is_reserved_coordinator_name(
                update_data.get("first_name"),
            )
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="coordinator_name_is_reserved",
            )
        # Renames share the natural-name uniqueness creation enforces:
        # org-wide for organization assistants, per-user for personal ones.
        # Existing duplicates are grandfathered — only new name writes are
        # validated.
        if "first_name" in update_data or "surname" in update_data:
            conflict = display_name_conflict(
                session,
                user_id=existing_assistant.user_id,
                organization_id=existing_assistant.organization_id,
                first_name=update_data.get(
                    "first_name",
                    existing_assistant.first_name,
                ),
                surname=update_data.get("surname", existing_assistant.surname),
                exclude_agent_id=existing_assistant.agent_id,
            )
            if conflict is not None:
                taken = " ".join(
                    part for part in (conflict.first_name, conflict.surname) if part
                ).strip()
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"Another assistant in this workspace is already "
                        f"named {taken!r}; pick a name teammates can tell "
                        f"apart."
                    ),
                )
        if "weekly_limit" in update_data and update.weekly_limit is not None:
            update_data["weekly_limit"] = Decimal(update.weekly_limit)
        if (
            "monthly_spending_cap" in update_data
            and update.monthly_spending_cap is not None
        ):
            update_data["monthly_spending_cap"] = Decimal(
                str(update.monthly_spending_cap),
            )
        runtime_update_requires_reawaken = _runtime_update_requires_reawaken(
            existing_assistant,
            update_data,
        )

        updated = assistant_dao.update_assistant(
            user_id=request.state.user_id,
            agent_id=assistant_id,
            update_data=update_data,
            organization_id=organization_id,
        )
        if not updated:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Assistant not found.",
            )

        # If the photo was updated, delete the old one from GCS.
        if is_photo_changing and old_photo_url and old_photo_url.startswith("gs://"):
            try:
                bucket_service.delete_assistant_file(old_photo_url)
                logging.info(
                    f"Successfully deleted old profile photo {old_photo_url} for assistant {assistant_id}.",
                )
            except Exception as e:
                logging.error(
                    f"Failed to delete old profile photo {old_photo_url} for assistant {assistant_id} during update. Error: {str(e)}",
                )

        # If the video was updated, delete the old one from GCS.
        if is_video_changing and old_video_url and old_video_url.startswith("gs://"):
            try:
                bucket_service.delete_assistant_file(old_video_url)
                logging.info(
                    f"Successfully deleted old profile video {old_video_url} for assistant {assistant_id}.",
                )
            except Exception as e:
                logging.error(
                    f"Failed to delete old profile video {old_video_url} for assistant {assistant_id} during update. Error: {str(e)}",
                )

        session.commit()
        session.refresh(updated)

        if runtime_update_requires_reawaken:
            try:
                await reawaken_assistant(
                    str(assistant_id),
                )
            except Exception as e:
                logging.warning(
                    "Failed to reawaken assistant %s after runtime config update: %s",
                    assistant_id,
                    e,
                )

        return InfoResponse(
            info=_build_assistant_read(updated, session),
        )
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logging.error(f"Error updating assistant config: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error updating assistant config",
        )


@router.post(
    "/assistant/{assistant_id}/transfer/to-org",
    response_model=InfoResponse[AssistantTransferResponse],
    status_code=status.HTTP_200_OK,
    summary="Transfer assistant to organization",
    description="Transfers a personal assistant to an organizational workspace.",
    tags=["Assistant Management"],
    responses={
        200: {"description": "Assistant transferred successfully"},
        403: {"description": "Permission denied"},
        404: {"description": "Assistant not found"},
        400: {"description": "Invalid transfer request"},
    },
)
async def transfer_assistant_to_org(
    assistant_id: int,
    transfer_request: AssistantTransferToOrgRequest,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[AssistantTransferResponse]:
    """
    Transfer a personal assistant to an organization.

    This endpoint:
    1. Moves the assistant from personal workspace to organizational workspace
    2. Optionally transfers logs from personal "Assistants" project to org "Assistants" project
    3. Grants the transferring user Owner role on the assistant in the org
    4. Updates the assistant's associated API key to the org API key
    """
    user_id = request.state.user_id
    target_org_id = transfer_request.organization_id
    assistant_dao = AssistantDAO(session)
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    resource_access_dao = ResourceAccessDAO(session)
    role_dao = RoleDAO(session)

    # Verify this is a personal assistant (must use personal API key)
    current_org_id = getattr(request.state, "organization_id", None)
    if current_org_id is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Must use a personal API key to transfer personal assistants. Use an org API key for org assistants.",
        )

    # Get the personal assistant
    assistant = assistant_dao.get_assistant_by_id(
        user_id=user_id,
        agent_id=assistant_id,
        organization_id=None,
    )
    if not assistant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Personal assistant not found.",
        )

    # Block transfer if the assistant has contacts in grace_period
    # (unpaid billing must be resolved before transferring ownership)
    contact_dao = AssistantContactDAO(session)
    if contact_dao.has_grace_period_contacts(assistant_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Cannot transfer assistant: it has contact details in a billing "
                "grace period. Please add credits to resolve the outstanding "
                "balance before transferring."
            ),
        )

    # Check user has assistant:write permission in target org
    has_permission = resource_access_dao.check_org_member_permission(
        user_id,
        target_org_id,
        "assistant:write",
    )
    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to create assistants in the target organization.",
        )

    logs_transferred = False
    try:
        # Transfer logs if requested
        if transfer_request.transfer_logs:
            ASSISTANTS_PROJECT_NAME = "Assistants"
            # Get personal Assistants project
            personal_project = project_dao.get_by_user_and_name(
                user_id=user_id,
                name=ASSISTANTS_PROJECT_NAME,
                organization_id=None,
            )
            # Get or create org Assistants project
            # Use filter() instead of get_by_user_and_name() because we need to find
            # org projects directly without requiring user access checks
            org_projects = project_dao.filter(
                organization_id=target_org_id,
                name=ASSISTANTS_PROJECT_NAME,
            )
            org_project = org_projects[0][0] if org_projects else None
            project_created = False
            if not org_project:
                project_dao.create(
                    user_id=None,
                    organization_id=target_org_id,
                    name=ASSISTANTS_PROJECT_NAME,
                    description="Project to manage and track all organization assistants.",
                    is_versioned=False,
                )
                session.flush()  # Get project ID
                org_projects = project_dao.filter(
                    organization_id=target_org_id,
                    name=ASSISTANTS_PROJECT_NAME,
                )
                org_project = org_projects[0][0] if org_projects else None
                project_created = True

            # Grant access to the Assistants project for the transferring user
            if org_project:
                owner_role = role_dao.get_by_name("Owner", organization_id=None)
                if project_created:
                    # Creator gets Owner role
                    if owner_role:
                        resource_access_dao.grant_access(
                            resource_type="project",
                            resource_id=org_project.id,
                            role_id=owner_role.id,
                            grantee_type="user",
                            grantee_id=user_id,
                        )

                    # Grant Member access to all other existing org members
                    org_members = organization_member_dao.filter(
                        organization_id=target_org_id,
                    )
                    member_role = role_dao.get_by_name("Member", organization_id=None)
                    if member_role:
                        for member_row in org_members:
                            member = member_row[0]
                            if member.user_id != user_id:
                                resource_access_dao.grant_access(
                                    resource_type="project",
                                    resource_id=org_project.id,
                                    role_id=member_role.id,
                                    grantee_type="user",
                                    grantee_id=member.user_id,
                                )
                else:
                    # Project exists - check if user already has access
                    has_access = resource_access_dao.check_user_permission(
                        user_id,
                        "project",
                        org_project.id,
                        "project:read",
                    )
                    if not has_access:
                        # Grant Member role to user
                        member_role = role_dao.get_by_name(
                            "Member",
                            organization_id=None,
                        )
                        if member_role:
                            resource_access_dao.grant_access(
                                resource_type="project",
                                resource_id=org_project.id,
                                role_id=member_role.id,
                                grantee_type="user",
                                grantee_id=user_id,
                            )

            if personal_project and org_project:
                assistant_context_id = str(assistant_id)
                context_prefix = f"{user_id}/{assistant_context_id}"
                contexts_to_transfer = (
                    session.query(Context)
                    .filter(
                        Context.project_id == personal_project.id,
                        or_(
                            Context.name == context_prefix,
                            Context.name.like(f"{context_prefix}/%"),
                        ),
                    )
                    .all()
                )
                from orchestra.db.dao.log_event_dao import LogEventDAO

                le_dao = LogEventDAO(session)

                # Move the assistant's owner-homogeneous contexts (and every log
                # they hold) to the org project, keeping the denormalized
                # partition key (project_id) consistent across log_event and its
                # child tables (log_event_context / embedding / embedding_queue).
                # Each lookup carries a literal project_id so it prunes; discovery
                # finishes before reproject (which moves a log_event and all its
                # associations wholesale).
                log_ids: list[int] = []
                for ctx in contexts_to_transfer:
                    log_ids.extend(
                        row[0]
                        for row in session.query(LogEventContext.log_event_id)
                        .filter(
                            LogEventContext.project_id == personal_project.id,
                            LogEventContext.context_id == ctx.id,
                        )
                        .all()
                    )
                if log_ids:
                    le_dao.reproject_logs(log_ids, org_project.id)
                for ctx in contexts_to_transfer:
                    ctx.project_id = org_project.id

                logs_transferred = len(contexts_to_transfer) > 0

        # Transfer the assistant to org
        transferred = assistant_dao.transfer_to_organization(
            agent_id=assistant_id,
            user_id=user_id,
            organization_id=target_org_id,
        )
        if not transferred:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to transfer assistant.",
            )

        # Grant Owner role to the user on this assistant
        owner_role = role_dao.get_by_name("Owner", organization_id=None)
        if owner_role:
            resource_access_dao.grant_access(
                resource_type="assistant",
                resource_id=assistant_id,
                role_id=owner_role.id,
                grantee_type="user",
                grantee_id=user_id,
            )

        session.commit()

        # Refresh the moved assistant's Contacts so it picks up the destination
        # org's members and drops anything tied to the personal scope.
        await trigger_contact_sync_safe(assistant_id)

        return InfoResponse(
            info=AssistantTransferResponse(
                message="Assistant transferred to organization successfully.",
                agent_id=assistant_id,
                transferred_from="personal",
                transferred_to="organization",
                logs_transferred=logs_transferred,
            ),
        )
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logging.error(
            f"Failed to transfer assistant {assistant_id} to org: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to transfer assistant",
        )


@router.post(
    "/assistant/{assistant_id}/transfer/to-team-owned",
    response_model=InfoResponse[AssistantTransferToTeamOwnedResponse],
    status_code=status.HTTP_200_OK,
    summary="Convert assistant to team-owned scope",
    description=(
        "Moves an organizational assistant's memory from its personal "
        "{user}/{agent} root to Teams/{team}/... and records the team as "
        "the product-level owner."
    ),
    tags=["Assistant Management"],
    responses={
        200: {"description": "Assistant converted successfully"},
        403: {"description": "Permission denied"},
        404: {"description": "Assistant or team not found"},
        400: {"description": "Invalid transfer request"},
        409: {"description": "Assistant already team-owned or blocked"},
    },
)
async def transfer_assistant_to_team_owned_endpoint(
    assistant_id: int,
    transfer_request: AssistantTransferToTeamOwnedRequest,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[AssistantTransferToTeamOwnedResponse]:
    """Convert an org assistant to team-owned memory scope."""
    user_id = request.state.user_id
    organization_id = getattr(request.state, "organization_id", None)
    if organization_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "owner_team_id conversion requires an organization API key; "
                "team-owned assistants live inside an organization."
            ),
        )

    resource_access_dao = ResourceAccessDAO(session)
    has_permission = resource_access_dao.check_org_member_permission(
        user_id,
        organization_id,
        "assistant:write",
    )
    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to update assistants in this organization.",
        )

    assistant_dao = AssistantDAO(session)
    assistant = assistant_dao.get_assistant_by_id(
        user_id=user_id,
        agent_id=assistant_id,
        organization_id=organization_id,
    )
    if not assistant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found in this organization.",
        )

    try:
        result = await transfer_assistant_to_team_owned(
            session,
            assistant_id=assistant_id,
            owner_team_id=transfer_request.owner_team_id,
            actor_user_id=user_id,
            merge_memory=transfer_request.merge_memory,
        )
    except TeamOwnershipTransferError as exc:
        session.rollback()
        detail = str(exc)
        status_code = status.HTTP_404_NOT_FOUND
        if detail in {
            "assistant_already_team_owned",
            "coordinator_cannot_be_team_owned",
        } or detail.startswith(
            (
                "team_memory_collision_both_have_data",
                "team_memory_merge_schema_mismatch",
                "team_memory_merge_secret_conflict",
                "team_memory_merge_function_conflict",
                "team_memory_merge_unique_key_conflict",
                "team_memory_merge_versioned_context",
            ),
        ):
            status_code = status.HTTP_409_CONFLICT
        elif detail in {
            "assistant_not_in_organization",
            "team_not_in_organization",
        }:
            status_code = status.HTTP_400_BAD_REQUEST
        elif detail in {
            "team_memory_transfer_incomplete",
            "team_memory_collision_unresolved",
        }:
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        raise HTTPException(status_code=status_code, detail=detail) from exc
    except Exception as exc:
        session.rollback()
        logging.error(
            f"Failed to convert assistant {assistant_id} to team-owned: {exc}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to convert assistant to team-owned scope",
        ) from exc

    return InfoResponse(
        info=AssistantTransferToTeamOwnedResponse(
            message="Assistant converted to team-owned scope successfully.",
            agent_id=int(result["agent_id"]),
            owner_team_id=int(result["owner_team_id"]),
            contexts_renamed=int(result["contexts_renamed"]),
            contexts_merged=int(result["contexts_merged"]),
            duplicate_contacts=list(result["duplicate_contacts"]),
            memory_root=str(result["memory_root"]),
        ),
    )


@router.post(
    "/assistant/{assistant_id}/transfer/to-personal",
    response_model=InfoResponse[AssistantTransferResponse],
    status_code=status.HTTP_200_OK,
    summary="Transfer assistant to personal workspace",
    description="Transfers an organizational assistant to the user's personal workspace.",
    tags=["Assistant Management"],
    responses={
        200: {"description": "Assistant transferred successfully"},
        403: {"description": "Permission denied"},
        404: {"description": "Assistant not found"},
        400: {"description": "Invalid transfer request"},
    },
)
async def transfer_assistant_to_personal(
    assistant_id: int,
    transfer_request: AssistantTransferToPersonalRequest,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[AssistantTransferResponse]:
    """
    Transfer an organizational assistant to personal workspace.

    This endpoint:
    1. Moves the assistant from org workspace to user's personal workspace
    2. Deletes related logs from org "Assistants" project if requested
    3. Removes RBAC grants on the assistant
    4. Updates the assistant's owner to the requesting user
    """
    user_id = request.state.user_id
    organization_id = getattr(request.state, "organization_id", None)
    assistant_dao = AssistantDAO(session)
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    resource_access_dao = ResourceAccessDAO(session)

    # Verify this is an org assistant (must use org API key)
    if organization_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Must use an organization API key to transfer org assistants.",
        )
    if personal_workspace_is_disabled(session, user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Personal workspace is disabled for organization members.",
        )

    # Get the org assistant
    assistant = assistant_dao.get_assistant_by_id(
        user_id=user_id,
        agent_id=assistant_id,
        organization_id=organization_id,
    )
    if not assistant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization assistant not found.",
        )
    if _is_hidden_workspace_coordinator_for_user(assistant, user_id=user_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization assistant not found.",
        )

    # Block transfer if the assistant has contacts in grace_period
    # (unpaid billing must be resolved before transferring ownership)
    contact_dao = AssistantContactDAO(session)
    if contact_dao.has_grace_period_contacts(assistant_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Cannot transfer assistant: it has contact details in a billing "
                "grace period. Please add credits to resolve the outstanding "
                "balance before transferring."
            ),
        )

    # Check user has assistant:delete permission on this assistant
    has_permission = resource_access_dao.check_user_permission(
        user_id,
        "assistant",
        assistant_id,
        "assistant:delete",
    )
    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to transfer this assistant out of the organization.",
        )

    logs_deleted = False
    try:
        # Delete logs if requested
        if transfer_request.delete_logs:
            ASSISTANTS_PROJECT_NAME = "Assistants"
            # Use filter() instead of get_by_user_and_name() because we need to find
            # org projects directly without requiring user access checks
            org_projects = project_dao.filter(
                organization_id=organization_id,
                name=ASSISTANTS_PROJECT_NAME,
            )
            org_project = org_projects[0][0] if org_projects else None
            if org_project:
                assistant_context_id = str(assistant_id)
                contexts_to_delete = (
                    session.query(Context)
                    .filter(
                        Context.project_id == org_project.id,
                        or_(
                            Context.name == assistant_context_id,
                            Context.name.like(f"{assistant_context_id}/%"),
                            Context.name.like(f"%/{assistant_context_id}"),
                            Context.name.like(f"%/{assistant_context_id}/%"),
                        ),
                    )
                    .all()
                )
                for ctx in contexts_to_delete:
                    context_dao.delete(ctx.id)

                logs_deleted = len(contexts_to_delete) > 0

        # Remove all RBAC grants on this assistant
        existing_grants = resource_access_dao.get_resource_access(
            resource_type="assistant",
            resource_id=assistant_id,
        )
        for grant in existing_grants:
            resource_access_dao.revoke_access(
                resource_type="assistant",
                resource_id=assistant_id,
                grantee_type=grant.grantee_type,
                grantee_id=grant.grantee_id,
            )

        # Transfer the assistant to personal
        transferred = assistant_dao.transfer_to_personal(
            agent_id=assistant_id,
            organization_id=organization_id,
            new_owner_user_id=user_id,
        )
        if not transferred:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to transfer assistant.",
            )

        session.commit()

        # Refresh the moved assistant's Contacts so it drops org-scoped rows
        # and reseeds for its new personal owner.
        await trigger_contact_sync_safe(assistant_id)

        return InfoResponse(
            info=AssistantTransferResponse(
                message="Assistant transferred to personal workspace successfully.",
                agent_id=assistant_id,
                transferred_from="organization",
                transferred_to="personal",
                logs_deleted=logs_deleted,
            ),
        )
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logging.error(
            f"Failed to transfer assistant {assistant_id} to personal: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to transfer assistant",
        )


@router.post(
    "/assistant/voice",
    response_model=InfoResponse[VoiceRead],
    status_code=status.HTTP_201_CREATED,
    summary="Register voice",
    description="Register a preset assistant voice.",
    responses={
        200: {
            "description": "Voice created successfully",
            "content": {
                "application/json": {
                    "example": {
                        "info": {
                            "voice_id": "bf0a246a-8642-498a-9950-80c35e9276b5",
                            "name": "English Woman Calm 1",
                            "description": "Calm and relaxting voice of an english-speaking woman",
                            "gender": "female",
                            "language": "en",
                            "provider": "cartesia",
                            "is_preset": True,
                        },
                    },
                },
            },
        },
        422: {
            "description": "Validation Error",
            "content": {
                "application/json": {
                    "example": {
                        "detail": [
                            {
                                "loc": ["body", "name"],
                                "msg": "field required",
                                "type": "value_error.missing",
                            },
                        ],
                    },
                },
            },
        },
    },
    tags=["Voices"],
)
def register_voice(
    voice_in: VoiceCreate,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[VoiceRead]:
    dao = VoiceDAO(session)
    try:

        voice = dao.create_voice(
            user_id=request.state.user_id,
            voice_id=voice_in.voice_id,
            name=voice_in.name,
            description=voice_in.description,
            gender=voice_in.gender,
            language=voice_in.language,
            provider=voice_in.provider,
        )
        voice.is_preset = (
            voice_in.is_preset if voice_in.is_preset is not None else False
        )
        session.commit()
        return InfoResponse(
            info=VoiceRead(
                voice_id=voice.voice_id,
                name=voice.name,
                description=voice.description,
                gender=voice.gender,
                language=voice.language,
                provider=voice.provider,
                is_preset=voice.is_preset,
            ),
        )
    except IntegrityError as e:
        session.rollback()
        if (
            "violates unique constraint" in str(e).lower()
            and "voices_pkey" in str(e).lower()
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Voice with ID '{voice_in.voice_id}' already exists for this user.",
            )
        logging.error(f"Database error registering voice: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Database error registering voice",
        )
    except HTTPException as e:
        session.rollback()
        raise e
    except Exception as e:
        session.rollback()
        logging.error(f"Error registering voice: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Error registering voice",
        )


@router.post(
    "/assistant/voice/clone",
    response_model=InfoResponse[VoiceRead],
    status_code=status.HTTP_201_CREATED,
    summary="Clone voice",
    description="Create a new assistant voice by cloning a voice from an audio file.",
    tags=["Voices"],
    include_in_schema=False,
)
async def clone_voice(
    request: Request,
    session: Session = Depends(get_db_session),
    cartesia_service: CartesiaService = Depends(),
    elevenlabs_service: ElevenLabsService = Depends(),
    deepgram_service: DeepgramService = Depends(),
    name: str = Form(..., example="My Voice Clone"),
    language: Optional[str] = Form(None, example="en"),
    description: Optional[str] = Form(None, example="A cloned voice for my assistant"),
    gender: Optional[str] = Form(None, example="female"),
    provider: str = Form("cartesia"),
    file: UploadFile = File(..., example="voice_sample.wav"),
):
    user_id = request.state.user_id
    voice_dao = VoiceDAO(session)
    new_voice_id: Optional[str] = None
    voice_language: Optional[str] = language

    MAX_VOICE_CLONE_BYTES = 25 * 1024 * 1024  # 25 MB
    try:
        file_content = await file.read()
        if len(file_content) > MAX_VOICE_CLONE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"File size exceeds {MAX_VOICE_CLONE_BYTES // (1024 * 1024)}MB limit.",
            )
        if not voice_language:
            try:
                detected_language = deepgram_service.detect_language_from_audio(
                    file_content,
                    user_id,
                    file.content_type,
                )
                voice_language = detected_language or "en"
            except DeepgramAPIError as e:
                logging.error(
                    f"Deepgram API error during voice clone language detection: {e.detail}",
                )
                raise HTTPException(
                    status_code=e.status_code,
                    detail=f"Language detection failed: {e.detail}",
                )

        if provider == "cartesia":
            cartesia_response = cartesia_service.clone_voice(
                file_content=file_content,
                file_name=file.filename or "audio_clip_default_name",
                name=name,
                language=voice_language,
                description=description,
            )
            new_voice_id = cartesia_response.get("id")
        elif provider == "elevenlabs":
            elevenlabs_response = elevenlabs_service.clone_voice(
                file_content=file_content,
                file_name=file.filename or "audio_clip_default_name",
                name=name,
                description=description,
            )
            new_voice_id = elevenlabs_response.get("voice_id")
        else:
            raise HTTPException(status_code=400, detail="Invalid provider.")

        if not new_voice_id:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"{provider.capitalize()} did not return a voice ID after cloning.",
            )

        db_voice = voice_dao.create_voice(
            user_id=user_id,
            voice_id=new_voice_id,
            name=name,
            description=description or f"Cloned voice: {name}",
            gender=gender,
            language=voice_language,
            provider=provider,
        )
        if provider == "cartesia" and not gender:
            db_voice.gender = cartesia_response.get("gender")
        db_voice.is_preset = False
        session.commit()

        return InfoResponse(
            info=VoiceRead(
                voice_id=db_voice.voice_id,
                name=db_voice.name,
                description=db_voice.description,
                language=db_voice.language,
                gender=db_voice.gender,
                provider=db_voice.provider,
                is_preset=False,
            ),
        )

    except (CartesiaAPIError, ElevenLabsAPIError, DeepgramAPIError) as e:
        session.rollback()
        service_name = "External service"
        if isinstance(e, CartesiaAPIError):
            service_name = "Cartesia"
        elif isinstance(e, ElevenLabsAPIError):
            service_name = "ElevenLabs"
        elif isinstance(e, DeepgramAPIError):
            service_name = "Language Detection"
        raise HTTPException(
            status_code=e.status_code,
            detail=f"{service_name} API error: {e.detail}",
        )
    except IntegrityError as e_db_integrity:
        session.rollback()
        if new_voice_id:
            logging.warning(
                f"DB save failed for cloned voice {new_voice_id} due to integrity error. Attempting {provider} cleanup.",
            )
            if provider == "cartesia":
                provider_service = cartesia_service
            elif provider == "elevenlabs":
                provider_service = elevenlabs_service
            try:
                provider_service.delete_voice(new_voice_id)
            except Exception as e_voice_cleanup:
                logging.error(
                    f"Failed to cleanup {provider} voice {new_voice_id} after DB integrity error: {e_voice_cleanup}",
                )
        logging.error(
            f"Failed to save cloned voice to database: {e_db_integrity}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Failed to save cloned voice to database",
        )
    except Exception as e_generic:
        session.rollback()
        if new_voice_id:
            if provider == "cartesia":
                provider_service = cartesia_service
            elif provider == "elevenlabs":
                provider_service = elevenlabs_service
            try:
                provider_service.delete_voice(new_voice_id)
            except Exception:
                pass
        logging.error(f"Failed to clone and save voice: {e_generic}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to clone and save voice",
        )


@router.get(
    "/assistant/default-model-options",
    response_model=InfoResponse[List[DefaultModelOptionRead]],
    status_code=status.HTTP_200_OK,
    summary="List default model options",
    description=(
        "Returns the curated catalog of multimodal LLM options that can be "
        "set as an assistant's default (actor) or slow-brain model. Pass "
        "usage=slow_brain to label the system-default row for the platform "
        "slow-brain default. Use "
        "GET /assistant/default-model-options/search to browse the full "
        "OpenRouter catalog."
    ),
    tags=["Assistant Management"],
)
def list_default_model_options(
    usage: Literal["actor", "slow_brain"] = Query(
        "actor",
        description=(
            "Which runtime role the catalog is for. Affects only the "
            "system-default option's label and per-message credit display; "
            "the selectable model pairs are identical."
        ),
    ),
) -> InfoResponse[List[DefaultModelOptionRead]]:
    """List the selectable per-assistant LLM options."""
    return InfoResponse(
        info=[
            DefaultModelOptionRead(
                model=option.model,
                reasoning_effort=option.reasoning_effort,
                label=option.label,
                approx_credits_per_task=option.approx_credits_per_task,
                approx_credits_per_message=option.approx_credits_per_message,
                artificial_analysis_url=option.artificial_analysis_url,
                recommended=True,
                eligible=True,
                disabled_reason=None,
                supports_reasoning=True,
                input_cost_per_token=option.input_usd_per_m / 1_000_000,
                output_cost_per_token=option.output_usd_per_m / 1_000_000,
            )
            for option in list_model_options(usage)
        ],
    )


@router.get(
    "/assistant/default-model-options/search",
    response_model=InfoResponse[List[DefaultModelOptionRead]],
    status_code=status.HTTP_200_OK,
    summary="Search OpenRouter model options",
    description=(
        "Search the OpenRouter model catalog for assistant default / "
        "slow-brain selection. Results that fail the multimodal (and for "
        "actor usage, tools) policy are returned with eligible=false."
    ),
    tags=["Assistant Management"],
)
def search_default_model_options(
    q: str = Query("", description="Case-insensitive substring match on id/name."),
    usage: Literal["actor", "slow_brain"] = Query(
        "actor",
        description="Actor requires tools; slow_brain requires image input only.",
    ),
    limit: int = Query(50, ge=1, le=200),
) -> InfoResponse[List[DefaultModelOptionRead]]:
    from orchestra.services.openrouter_catalog import search_models
    from orchestra.web.api.assistant.default_models import (
        credits_per_message_from_token_costs,
    )

    rows = search_models(
        q,
        limit=limit,
        require_tools=(usage == "actor"),
    )
    return InfoResponse(
        info=[
            DefaultModelOptionRead(
                model=row["endpoint"],
                reasoning_effort=None,
                label=str(row.get("name") or row["id"]),
                approx_credits_per_task=None,
                approx_credits_per_message=credits_per_message_from_token_costs(
                    row.get("input_cost_per_token"),
                    row.get("output_cost_per_token"),
                ),
                artificial_analysis_url=None,
                recommended=False,
                eligible=bool(row.get("eligible")),
                disabled_reason=row.get("disabled_reason"),
                supports_reasoning=bool(row.get("supports_reasoning")),
                input_cost_per_token=row.get("input_cost_per_token"),
                output_cost_per_token=row.get("output_cost_per_token"),
                context_length=row.get("context_length"),
            )
            for row in rows
        ],
    )


@router.get(
    "/assistant/voice",
    response_model=InfoResponse[List[VoiceRead]],
    status_code=status.HTTP_200_OK,
    summary="List voices",
    description="Returns a list of all assistant voices created for the user.",
    responses={
        200: {
            "description": "List of voices retrieved successfully",
            "content": {
                "application/json": {
                    "example": {
                        "info": [
                            {
                                "voice_id": "bf0a246a-8642-498a-9950-80c35e9276b5",
                                "name": "English Woman Calm 1",
                                "description": "Calm and relaxting voice of an english-speaking woman",
                                "gender": "female",
                                "language": "en",
                                "provider": "cartesia",
                                "is_preset": True,
                            },
                            {
                                "voice_id": "c99d36f3-5ffd-4253-803a-535c1bc9c306",
                                "name": "English Male Deep 1",
                                "description": "A deep, smoooth British man's voice perfect for narration.",
                                "gender": "male",
                                "language": "en",
                                "provider": "elevenlabs",
                                "is_preset": False,
                            },
                        ],
                    },
                },
            },
        },
        404: {
            "description": "Voice Not Found",
            "content": {
                "application/json": {"example": {"detail": "Voice not found."}},
            },
        },
    },
    tags=["Voices"],
)
def list_voices(
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[List[VoiceRead]]:
    """
    List all voices saved by the authenticated user.
    """
    dao = VoiceDAO(session)
    try:
        voices = dao.list_voices_for_user(
            user_id=request.state.user_id,
        )

        return InfoResponse(
            info=[
                VoiceRead(
                    voice_id=voice.voice_id,
                    name=voice.name,
                    description=voice.description,
                    language=voice.language,
                    gender=voice.gender,
                    provider=voice.provider,
                    is_preset=voice.is_preset,
                )
                for voice in voices
            ],
        )
    except Exception as e:
        logging.error(f"Error fetching user voices: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Error fetching user voices",
        )


@router.delete(
    "/assistant/voice/{voice_id}",
    status_code=status.HTTP_200_OK,
    response_model=InfoResponse[str],
    summary="Delete voice",
    description="Deletes a specific assistant voice.",
    responses={
        200: {
            "description": "Voice deleted successfully",
            "content": {
                "application/json": {
                    "example": {"info": "Voice deleted successfully"},
                },
            },
        },
        404: {
            "description": "Voice not found",
            "content": {
                "application/json": {"example": {"detail": "Voice not found."}},
            },
        },
    },
    tags=["Voices"],
)
async def delete_voice(
    voice_id: str,
    request: Request,
    provider: str = Query(..., description="The provider of the voice to delete"),
    session: Session = Depends(get_db_session),
    cartesia_service: CartesiaService = Depends(),
    elevenlabs_service: ElevenLabsService = Depends(),
) -> InfoResponse[str]:
    user_id = request.state.user_id
    voice_dao = VoiceDAO(session)

    # First, get the voice to check its existence and preset status.
    voice_to_delete = voice_dao.get_voice_by_id(
        user_id=user_id,
        voice_id=voice_id,
        provider=provider,
    )
    if not voice_to_delete:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Voice not found for this user.",
        )

    try:
        # Attempt to delete from our DB first. The DAO method contains the
        # "in-use" validation and will raise a 409 Conflict if necessary.
        voice_dao.delete_voice(user_id=user_id, voice_id=voice_id, provider=provider)

        # If the voice is not a preset, also delete it from the provider.
        if not voice_to_delete.is_preset:
            provider_service = None
            if voice_to_delete.provider == "cartesia":
                provider_service = cartesia_service
            elif voice_to_delete.provider == "elevenlabs":
                provider_service = elevenlabs_service

            if provider_service:
                try:
                    provider_service.delete_voice(voice_id)
                except (CartesiaAPIError, ElevenLabsAPIError) as e_provider:
                    # If the provider says "not found," it's a non-critical error.
                    # We can proceed since our goal is to have it deleted.
                    if e_provider.status_code == 404:
                        logging.warning(
                            f"Voice {voice_id} not found on {voice_to_delete.provider} during deletion attempt. Continuing with DB deletion.",
                        )
                    else:
                        # For other provider errors, we must roll back our DB change.
                        raise e_provider  # This will be caught below.

        # If both DB and provider deletions were successful (or skippable), commit.
        session.commit()
        return InfoResponse(info="Voice deleted successfully.")

    except HTTPException as e:
        session.rollback()
        raise e
    except (CartesiaAPIError, ElevenLabsAPIError) as e_provider:
        session.rollback()
        logging.error(
            f"Critical provider error deleting voice {voice_id} from {provider}: {e_provider.detail}",
        )
        raise HTTPException(
            status_code=e_provider.status_code,
            detail=f"Failed to delete voice from {provider}: {e_provider.detail}",
        )
    except Exception as e_generic:
        session.rollback()
        logging.error(
            f"Generic error during voice deletion {voice_id}: {e_generic}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error deleting voice",
        )


@router.post(
    "/assistant/voice/generate",
    # response_model is not InfoResponse[bytes] because we return raw audio
    status_code=status.HTTP_200_OK,
    summary="Generate speech from text",
    description="Generates audio from text using the specified provider and voice.",
    tags=["Voices"],
    responses={
        200: {
            "description": "Audio generated successfully. Content-Type will be audio/mpeg, audio/wav, etc.",
            # "content" example not straightforward for raw bytes, will depend on format
        },
        400: {
            "description": "Bad Request (e.g., invalid provider, provider API error)",
            "content": {
                "application/json": {"example": {"detail": "Provider API error: ..."}},
            },
        },
        503: {
            "description": "Service unavailable (e.g. provider API down)",
            "content": {
                "application/json": {
                    "example": {"detail": "TTS provider unavailable."},
                },
            },
        },
    },
)
async def generate_speech(
    request_data: VoiceGenerateRequest,
    request: Request,
    session: Session = Depends(get_db_session),
    cartesia_service: CartesiaService = Depends(),
    elevenlabs_service: ElevenLabsService = Depends(),
) -> Response:
    user_id = request.state.user_id
    audio_bytes: bytes
    content_type: str

    try:
        if request_data.provider == "cartesia":
            audio_bytes, content_type = cartesia_service.generate_speech(
                transcript=request_data.text,
                voice_id=request_data.voice_id,
                model_id=request_data.model_id or "sonic-2",  # Default Cartesia model
                output_format_container=request_data.output_format,
                output_sample_rate=request_data.cartesia_sample_rate,
                output_bit_rate=request_data.cartesia_bit_rate,
                language=request_data.cartesia_language,
            )
        elif request_data.provider == "elevenlabs":
            audio_bytes, content_type = elevenlabs_service.generate_speech(
                text=request_data.text,
                voice_id=request_data.voice_id,
                model_id=request_data.model_id
                or "eleven_multilingual_v2",  # Default EL model
                output_format=request_data.output_format,
                optimize_streaming_latency=request_data.elevenlabs_optimize_streaming_latency,
                stability=request_data.elevenlabs_voice_settings_stability,
                similarity_boost=request_data.elevenlabs_voice_settings_similarity_boost,
            )
        else:
            # This case should be prevented by Pydantic's Literal validation
            raise HTTPException(
                status_code=400,
                detail="Invalid TTS provider specified.",
            )

        return Response(content=audio_bytes, media_type=content_type)

    except (CartesiaAPIError, ElevenLabsAPIError) as e:
        logging.error(
            f"TTS API error for user {user_id}, provider {request_data.provider}: {e.detail}",
        )
        raise HTTPException(
            status_code=e.status_code,
            detail=f"TTS provider error: {e.detail}",
        )
    except HTTPException:
        raise
    except Exception as e:
        logging.error(
            f"Unexpected error generating speech for user {user_id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate speech",
        )


@router.post(
    "/assistant/voice/design/preview",
    response_model=InfoResponse[VoiceDesignGeneratePreviewsAPIResponse],
    status_code=status.HTTP_200_OK,
    summary="Design Voice Previews",
    description="Generates voice design previews from a text description.",
    tags=["Voices", "TTS Design"],
    include_in_schema=False,
)
async def design_voice_generate_previews_endpoint(
    request_data: VoiceDesignGeneratePreviewsRequest,
    request: Request,
    session: Session = Depends(get_db_session),
    elevenlabs_service: ElevenLabsService = Depends(),
    openai_service: OpenAIService = Depends(),
) -> InfoResponse[VoiceDesignGeneratePreviewsAPIResponse]:
    user_id = request.state.user_id
    final_voice_description = request_data.voice_description

    try:
        # If a bio is provided, use OpenAI to generate a more detailed description
        if request_data.bio:
            try:
                final_voice_description = (
                    openai_service.generate_voice_description_from_bio(
                        bio=request_data.bio,
                        description_hint=request_data.voice_description,
                    )
                )
                if not (20 <= len(final_voice_description) <= 1000):
                    logging.error(
                        f"OpenAI-generated voice description has invalid length ({len(final_voice_description)} chars). Content: '{final_voice_description}'",
                    )
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail="Failed to generate a voice description with the required length (20-1000 characters). Please try again.",
                    )
            except OpenAIAPIError as e:
                logging.error(
                    f"OpenAI API error during voice description generation: {e.detail}",
                )
                raise HTTPException(
                    status_code=e.status_code,
                    detail=f"Failed to generate voice description from bio: {e.detail}",
                )

        if not final_voice_description:
            # This should be caught by the pydantic validator, but as a safeguard.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A voice description is required. Provide 'voice_description' or 'bio'.",
            )
        el_response_data = elevenlabs_service.design_voice_generate_previews(
            voice_description=final_voice_description,
            text_for_preview=request_data.text,
            auto_generate_text_flag=request_data.auto_generate_text,
            model_id_for_design=request_data.model_id,
        )

        # Pydantic will validate if el_response_data matches VoiceDesignGeneratePreviewsAPIResponse
        return InfoResponse(
            info=VoiceDesignGeneratePreviewsAPIResponse(**el_response_data),
        )

    except ElevenLabsAPIError as e:
        logging.error(
            f"ElevenLabs voice design preview error for user {user_id}: {e.detail}",
        )
        raise HTTPException(
            status_code=e.status_code,
            detail=f"ElevenLabs API error: {e.detail}",
        )
    except HTTPException:
        raise
    except Exception as e:
        logging.error(
            f"Unexpected error generating voice previews for user {user_id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate voice previews",
        )


@router.post(
    "/assistant/voice/design/create",
    response_model=InfoResponse[VoiceRead],
    status_code=status.HTTP_201_CREATED,
    summary="Create Voice from Design Preview",
    description="Creates a full voice from a generated preview voice id.",
    tags=["Voices", "TTS Design"],
    include_in_schema=False,
)
async def design_voice_create_from_preview_endpoint(
    request_data: VoiceDesignCreateFromPreviewRequest,
    request: Request,
    session: Session = Depends(get_db_session),
    elevenlabs_service: ElevenLabsService = Depends(),
    deepgram_service: DeepgramService = Depends(),
    openai_service: OpenAIService = Depends(),
) -> InfoResponse[VoiceRead]:
    user_id = request.state.user_id
    voice_dao = VoiceDAO(session)
    new_el_voice_id: Optional[str] = None
    voice_language: Optional[str] = request_data.language

    try:
        if not voice_language:
            # Prioritize language detection from audio if provided
            if request_data.audio_base_64:
                try:
                    audio_content = base64.b64decode(request_data.audio_base_64)
                    # Assume MP3 if media_type is not provided
                    media_type = request_data.media_type or "audio/mpeg"
                    detected_language = deepgram_service.detect_language_from_audio(
                        audio_content=audio_content,
                        user_id=user_id,
                        content_type=media_type,
                    )
                    voice_language = detected_language or "en"
                except DeepgramAPIError as e:
                    logging.error(
                        f"Deepgram API error during design/create language detection: {e.detail}",
                    )
                    raise HTTPException(
                        status_code=e.status_code,
                        detail=f"Language detection from audio failed: {e.detail}",
                    )
                except Exception as e_decode:
                    logging.error(
                        f"Failed to decode base64 audio for language detection: {str(e_decode)}",
                    )
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Invalid base64 audio data provided.",
                    )
            # Fallback to language detection from text description
            else:
                try:
                    detected_language = openai_service.detect_language_from_text(
                        request_data.voice_description,
                    )
                    voice_language = detected_language or "en"
                except OpenAIAPIError as e:
                    logging.error(
                        f"OpenAI API error during design/create language detection: {e.detail}",
                    )
                    raise HTTPException(
                        status_code=e.status_code,
                        detail=f"Language detection from text failed: {e.detail}",
                    )

        # Step 1: Call ElevenLabs to create the voice from the generated_voice_id
        el_created_voice_data = elevenlabs_service.create_voice_from_generated_id(
            voice_name=request_data.voice_name,
            generated_voice_id=request_data.generated_voice_id,
            description=request_data.voice_description,
            labels=request_data.labels,
        )

        new_el_voice_id = el_created_voice_data.get("voice_id")
        if not new_el_voice_id:
            raise ElevenLabsAPIError(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="ElevenLabs did not return a 'voice_id' after creating the voice from preview.",
            )

        # Step 2: Save the new voice to our database
        db_voice = voice_dao.create_voice(
            user_id=user_id,
            voice_id=new_el_voice_id,
            name=request_data.voice_name,
            description=request_data.voice_description
            or f"Designed voice: {request_data.voice_name}",
            language=voice_language,
            gender=request_data.gender,
            provider="elevenlabs",
        )
        db_voice.is_preset = False  # Designed voices are not presets
        session.flush()  # Ensure db_voice gets all attributes before commit
        session.commit()  # Commit DB voice creation

        return InfoResponse(
            info=VoiceRead(
                voice_id=db_voice.voice_id,
                name=db_voice.name,
                description=db_voice.description,
                language=db_voice.language,
                gender=db_voice.gender,
                provider=db_voice.provider,
                is_preset=db_voice.is_preset,
            ),
        )

    except (ElevenLabsAPIError, DeepgramAPIError, OpenAIAPIError) as e:
        session.rollback()
        service_name = "External service"
        should_cleanup_el = isinstance(e, ElevenLabsAPIError)

        if isinstance(e, ElevenLabsAPIError):
            service_name = "ElevenLabs"
        elif isinstance(e, (DeepgramAPIError, OpenAIAPIError)):
            service_name = "Language Detection"
            should_cleanup_el = False  # Don't cleanup if EL was never called

        if new_el_voice_id and should_cleanup_el:
            try:
                logging.warning(
                    f"Attempting to clean up orphaned ElevenLabs voice {new_el_voice_id} due to error: {e.detail}",
                )
                elevenlabs_service.delete_voice(new_el_voice_id)
            except Exception as e_cleanup:
                logging.error(
                    f"Failed to cleanup orphaned ElevenLabs voice {new_el_voice_id}: {e_cleanup}",
                )
        logging.error(
            f"{service_name} error during voice creation from preview for user {user_id}: {e.detail}",
        )
        raise HTTPException(
            status_code=e.status_code,
            detail=f"{service_name} API error: {e.detail}",
        )
    except IntegrityError as e_db:
        session.rollback()
        if (
            new_el_voice_id
        ):  # EL voice was created, but DB failed (e.g. voice_id already exists in our DB by chance)
            logging.warning(
                f"DB IntegrityError for EL voice {new_el_voice_id}. Attempting EL cleanup.",
            )
            try:
                elevenlabs_service.delete_voice(new_el_voice_id)
            except Exception as e_cleanup:
                logging.error(
                    f"Failed to cleanup EL voice {new_el_voice_id} after DB integrity error: {e_cleanup}",
                )
        logging.error(
            f"Database error creating voice from preview: {e_db}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Database error creating voice, it might already exist",
        )
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        if new_el_voice_id:
            logging.warning(
                f"Generic error after EL voice {new_el_voice_id} might have been created. Attempting EL cleanup.",
            )
            try:
                elevenlabs_service.delete_voice(new_el_voice_id)
            except Exception as e_cleanup:
                logging.error(
                    f"Failed to cleanup EL voice {new_el_voice_id} after generic error: {e_cleanup}",
                )
        logging.error(
            f"Unexpected error creating voice from preview for user {user_id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create voice from preview",
        )


@router.post(
    "/assistant/photo/upload",
    response_model=InfoResponse[AssistantPhotoUploadResponse],
    status_code=status.HTTP_201_CREATED,
    summary="Upload photo",
    description="Uploads a profile photo for an assistant and return the storage URL.",
    tags=["Media"],
)
async def upload_assistant_photo(
    request: Request,
    file: UploadFile = File(..., example="assistant_photo.jpg"),
    assistant_id: Optional[int] = Form(None),
    session: Session = Depends(get_db_session),
):
    bucket_service = create_bucket_service()
    user_id = request.state.user_id
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User not authenticated.",
        )

    if assistant_id is not None:
        organization_id = getattr(request.state, "organization_id", None)
        assistant_dao = AssistantDAO(session)
        assistant = assistant_dao.get_assistant_by_id(
            user_id=user_id,
            agent_id=assistant_id,
            organization_id=organization_id,
        )
        if not assistant:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Assistant not found.",
            )
        if _is_hidden_workspace_coordinator_for_user(assistant, user_id=user_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Assistant not found.",
            )

    ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
    if not file.content_type or file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid file type. Allowed: {', '.join(ALLOWED_IMAGE_TYPES)}",
        )

    MAX_SIZE_BYTES = 5 * 1024 * 1024
    if (
        file.size and file.size > MAX_SIZE_BYTES
    ):  # FastAPI's UploadFile might have size after spooling
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File size exceeds {MAX_SIZE_BYTES // (1024*1024)}MB limit.",
        )

    try:
        file_content = await file.read()
        if len(file_content) > MAX_SIZE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"File content size exceeds {MAX_SIZE_BYTES // (1024*1024)}MB limit.",
            )

        gcs_url = bucket_service.upload_assistant_photo_file(
            file_content=file_content,
            user_id=user_id,
            content_type=file.content_type,
            assistant_id=assistant_id,
        )
        return InfoResponse(info=AssistantPhotoUploadResponse(gcs_url=gcs_url))
    except HTTPException as e:
        raise e
    except Exception as e:
        logging.error(f"Error uploading assistant photo for user {user_id}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not upload photo",
        )


@router.post(
    "/assistant/video/upload",
    response_model=InfoResponse[AssistantVideoUploadResponse],
    status_code=status.HTTP_201_CREATED,
    summary="Upload video",
    description="Uploads a profile video for an assistant and returns the storage URL.",
    tags=["Media"],
)
async def upload_assistant_video(
    request: Request,
    file: UploadFile = File(..., example="assistant_video.mp4"),
    assistant_id: Optional[int] = Form(None),
    session: Session = Depends(get_db_session),
):
    bucket_service = create_bucket_service()
    user_id = request.state.user_id
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User not authenticated.",
        )

    if assistant_id is not None:
        organization_id = getattr(request.state, "organization_id", None)
        assistant_dao = AssistantDAO(session)
        assistant = assistant_dao.get_assistant_by_id(
            user_id=user_id,
            agent_id=assistant_id,
            organization_id=organization_id,
        )
        if not assistant:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Assistant not found.",
            )
        if _is_hidden_workspace_coordinator_for_user(assistant, user_id=user_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Assistant not found.",
            )

    ALLOWED_VIDEO_TYPES = {"video/mp4", "video/webm", "video/quicktime"}
    if not file.content_type or file.content_type not in ALLOWED_VIDEO_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid file type. Allowed: {', '.join(ALLOWED_VIDEO_TYPES)}",
        )

    MAX_SIZE_BYTES = 50 * 1024 * 1024  # 50MB limit for videos
    if (
        file.size and file.size > MAX_SIZE_BYTES
    ):  # FastAPI's UploadFile might have size after spooling
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File size exceeds {MAX_SIZE_BYTES // (1024*1024)}MB limit.",
        )

    try:
        file_content = await file.read()
        if len(file_content) > MAX_SIZE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"File content size exceeds {MAX_SIZE_BYTES // (1024*1024)}MB limit.",
            )

        gcs_url = bucket_service.upload_assistant_photo_file(
            file_content=file_content,
            user_id=user_id,
            content_type=file.content_type,
            assistant_id=assistant_id,
        )
        return InfoResponse(info=AssistantVideoUploadResponse(gcs_url=gcs_url))
    except HTTPException as e:
        raise e
    except Exception as e:
        logging.error(
            f"Error uploading assistant video for user {user_id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not upload video",
        )


@router.post(
    "/assistant/photo/generate",
    response_model=InfoResponse[str],
    status_code=status.HTTP_201_CREATED,
    summary="Generate photo",
    description="Generates a new photo using a text prompt and returns the image URL. This action costs credits.",
    tags=["Media"],
)
async def generate_assistant_photo(
    request: Request,
    payload: PhotoGenerateRequest,
    session: Session = Depends(get_db_session),
    replicate_service: ReplicateService = Depends(),
    openai_service: OpenAIService = Depends(),
) -> InfoResponse[str]:
    """
    Generate a new assistant profile photo from a text prompt.

    This endpoint uses an AI model to generate an image based on the provided
    text prompt. The user's account is charged for this operation.
    """
    user_id = request.state.user_id
    organization_id = getattr(request.state, "organization_id", None)

    # 1. Moderate the prompt
    try:
        moderation_result = openai_service.moderate_text(payload.prompt)
        if moderation_result.is_nsfw:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Prompt moderation failed: {moderation_result.reason}",
            )
    except OpenAIAPIError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail=f"Content moderation check failed: {e.detail}",
        )

    # 2. Pre-check credits if not in staging
    if settings.charges_billing:
        try:
            billing_entity = get_billing_entity(session, user_id, organization_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail="Billing is not set up. Please add a payment method first.",
            )
        if not billing_entity.has_sufficient_credits(
            Decimal(str(settings.photo_generation_cost)),
        ):
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail="Insufficient credits to generate a photo.",
            )

    # 3. Generate photo
    try:
        image_url = replicate_service.generate_photo(
            prompt=payload.prompt,
            aspect_ratio=payload.aspect_ratio,
            output_format=payload.output_format,
            output_quality=payload.output_quality,
            safety_tolerance=payload.safety_tolerance,
            prompt_upsampling=payload.prompt_upsampling,
        )

        # 4. Deduct credits after successful generation if not in staging
        if settings.charges_billing:
            from orchestra.db.dao.billing_account_dao import BillingAccountDAO

            billing_entity = get_billing_entity(session, user_id, organization_id)
            BillingAccountDAO(session).deduct_credits(
                billing_entity.billing_account_id,
                float(settings.photo_generation_cost),
                category="media",
                user_id=user_id,
                organization_id=organization_id,
                description="Photo generation",
                detail={"event": "photo_generate"},
            )
            session.commit()

        return InfoResponse(info=image_url)
    except ReplicateAPIError as e:
        session.rollback()
        raise HTTPException(
            status_code=e.status_code,
            detail=f"Replicate API error: {e.detail}",
        )
    except Exception as e:
        session.rollback()
        logging.error(f"Error generating photo for user {user_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not generate photo",
        )


@router.post(
    "/assistant/photo/edit",
    response_model=InfoResponse[str],
    status_code=status.HTTP_201_CREATED,
    summary="Edit photo",
    description="Edits a photo using a text prompt and an input image (URL or file), and returns the image URL. This action costs credits.",
    tags=["Media"],
)
async def edit_assistant_photo(
    request: Request,
    session: Session = Depends(get_db_session),
    replicate_service: ReplicateService = Depends(),
    bucket_service=Depends(create_bucket_service),
    openai_service: OpenAIService = Depends(),
    prompt: str = Form(
        ...,
        example="A photo of a young woman with long brown hair and blue eyes.",
    ),
    input_image_url: Optional[str] = Form(
        None,
        example="https://example.com/input_image.jpg",
    ),
    input_image_file: Optional[UploadFile] = File(None, example="input_image.jpg"),
    aspect_ratio: str = Form("match_input_image", example="1:1"),
    output_format: str = Form("jpg", example="jpg"),
    safety_tolerance: float = Form(2.0, example=2.0),
) -> InfoResponse[str]:
    """
    Edit an assistant profile photo using a text prompt and an input image.

    This endpoint uses an AI model to edit an existing image based on a
    text prompt. The input image can be provided as a public URL or a direct file upload.
    The user's account is charged for this operation.
    """
    user_id = request.state.user_id
    organization_id = getattr(request.state, "organization_id", None)

    temp_gcs_url_to_delete: Optional[str] = None
    input_image_for_replicate: Optional[str] = None

    is_file_provided = input_image_file and input_image_file.filename

    if (input_image_url and is_file_provided) or (
        not input_image_url and not is_file_provided
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide either 'input_image_url' or 'input_image_file', but not both.",
        )

    try:
        if is_file_provided:
            if (
                not input_image_file.content_type
                or not input_image_file.content_type.startswith(
                    "image/",
                )
            ):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid file type for 'input_image_file'. Only images are allowed.",
                )
            file_content = await input_image_file.read()
            (
                public_url,
                gcs_url_for_delete,
            ) = bucket_service.upload_temp_assistant_file(
                file_content,
                user_id,
                input_image_file.content_type,
            )
            input_image_for_replicate = public_url
            temp_gcs_url_to_delete = gcs_url_for_delete
        else:
            input_image_for_replicate = input_image_url

        if not input_image_for_replicate:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No valid image input provided.",
            )

        # 1. Moderate inputs
        try:
            # Moderate text prompt
            prompt_moderation = openai_service.moderate_text(prompt)
            if prompt_moderation.is_nsfw:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Prompt moderation failed: {prompt_moderation.reason}",
                )

            # Moderate input image
            image_analysis = openai_service.analyze_image(
                image_url=input_image_for_replicate,
            )
            if image_analysis.is_nsfw:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Image moderation failed: {image_analysis.reason}",
                )
        except OpenAIAPIError as e:
            raise HTTPException(
                status_code=e.status_code,
                detail=f"Content moderation check failed: {e.detail}",
            )
        except HTTPException:
            raise

        # 2. Pre-check credits if not in staging
        if settings.charges_billing:
            try:
                billing_entity = get_billing_entity(session, user_id, organization_id)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail="Billing is not set up. Please add a payment method first.",
                )
            if not billing_entity.has_sufficient_credits(
                Decimal(str(settings.photo_generation_cost)),
            ):
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail="Insufficient credits to edit a photo.",
                )

        # 3. Edit Photo
        image_url = replicate_service.edit_photo(
            prompt=prompt,
            input_image=input_image_for_replicate,
            aspect_ratio=aspect_ratio,
            output_format=output_format,
            safety_tolerance=safety_tolerance,
        )

        # 4. Deduct credits after successful edit if not in staging
        if settings.charges_billing:
            from orchestra.db.dao.billing_account_dao import BillingAccountDAO

            edit_entity = get_billing_entity(session, user_id, organization_id)
            BillingAccountDAO(session).deduct_credits(
                edit_entity.billing_account_id,
                float(settings.photo_generation_cost),
                category="media",
                user_id=user_id,
                organization_id=organization_id,
                description="Photo edit",
                detail={"event": "photo_edit"},
            )
            session.commit()

        return InfoResponse(info=image_url)

    except ReplicateAPIError as e:
        session.rollback()
        logging.error(f"Replicate API error: {e.detail}")
        raise HTTPException(
            status_code=e.status_code,
            detail=f"Replicate API error: {e.detail}",
        )
    except HTTPException as http_e:
        session.rollback()
        logging.error(f"Could not edit photo: {str(http_e)}")
        raise
    except Exception as e:
        session.rollback()
        logging.error(f"Error editing photo for user {user_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not edit photo",
        )
    finally:
        if temp_gcs_url_to_delete:
            try:
                bucket_service.delete_assistant_file(temp_gcs_url_to_delete)
                logging.info(
                    f"Successfully deleted temporary file {temp_gcs_url_to_delete} for photo edit.",
                )
            except Exception as e_cleanup:
                logging.error(
                    f"Failed to clean up temporary file {temp_gcs_url_to_delete}: {e_cleanup}",
                )


@router.post(
    "/assistant/photo/animate",
    response_model=InfoResponse[ReplicatePredictionResponse],
    status_code=status.HTTP_202_ACCEPTED,
    summary="Animate photo",
    description="Starts a job to generate an animated video of the assistant using an input image and audio. This action costs credits.",
    tags=["Media"],
)
async def animate_video_endpoint(
    request: Request,
    session: Session = Depends(get_db_session),
    replicate_service: ReplicateService = Depends(),
    bucket_service=Depends(create_bucket_service),
    openai_service: OpenAIService = Depends(),
    image_url: Optional[str] = Form(None),
    image_file: Optional[UploadFile] = File(None),
    audio_url: Optional[str] = Form(None),
    audio_file: Optional[UploadFile] = File(None),
    seed: Optional[int] = Form(None),
) -> InfoResponse[ReplicatePredictionResponse]:
    user_id = request.state.user_id
    organization_id = getattr(request.state, "organization_id", None)

    temp_image_gcs_url: Optional[str] = None
    final_image_url_for_replicate: Optional[str] = None
    temp_audio_gcs_url: Optional[str] = None
    final_audio_url_for_replicate: Optional[str] = None

    is_image_file_provided = image_file and image_file.filename
    is_audio_file_provided = audio_file and audio_file.filename

    # Validate image input
    if (image_url and is_image_file_provided) or (
        not image_url and not is_image_file_provided
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide either 'image_url' or 'image_file', but not both.",
        )

    # Validate audio input
    if (audio_url and is_audio_file_provided) or (
        not audio_url and not is_audio_file_provided
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide either 'audio_url' or 'audio_file', but not both.",
        )

    try:
        # Process image input
        if is_image_file_provided:
            if not image_file.content_type or not image_file.content_type.startswith(
                "image/",
            ):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid file type for 'image_file'. Only images are allowed.",
                )
            image_content = await image_file.read()
            public_img_url, gcs_img_url = bucket_service.upload_temp_assistant_file(
                image_content,
                user_id,
                image_file.content_type,
            )
            final_image_url_for_replicate = public_img_url
            temp_image_gcs_url = gcs_img_url
        else:
            final_image_url_for_replicate = image_url

        # Process audio input and capture raw bytes for duration computation
        audio_bytes_for_duration: Optional[bytes] = None

        if is_audio_file_provided:
            if not audio_file.content_type or not audio_file.content_type.startswith(
                "audio/",
            ):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid file type for 'audio_file'. Only audio files are allowed.",
                )
            audio_content = await audio_file.read()
            audio_bytes_for_duration = audio_content
            (
                public_audio_url,
                gcs_audio_url,
            ) = bucket_service.upload_temp_assistant_file(
                audio_content,
                user_id,
                audio_file.content_type,
            )
            final_audio_url_for_replicate = public_audio_url
            temp_audio_gcs_url = gcs_audio_url
        else:
            from orchestra.web.api.utils.url_validation import validate_url_for_ssrf

            validate_url_for_ssrf(audio_url)
            final_audio_url_for_replicate = audio_url
            try:
                with urllib.request.urlopen(audio_url, timeout=30) as resp:
                    audio_bytes_for_duration = resp.read()
            except Exception as e:
                logging.warning(
                    f"Could not download audio from URL to compute duration: {e}",
                )

        if not final_image_url_for_replicate or not final_audio_url_for_replicate:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing valid image or audio input for Replicate.",
            )

        # Derive billable duration from the actual audio
        audio_duration_seconds: float = float(settings.default_video_duration)
        if audio_bytes_for_duration:
            try:
                audio_file_obj = mutagen.File(io.BytesIO(audio_bytes_for_duration))
                if audio_file_obj is not None and audio_file_obj.info is not None:
                    audio_duration_seconds = audio_file_obj.info.length
            except Exception as e:
                logging.warning(
                    f"Could not compute audio duration, "
                    f"falling back to {settings.default_video_duration}s: {e}",
                )

        billable_duration = math.ceil(audio_duration_seconds)

        # OmniHuman 1.5 supports audio up to 35s
        if audio_duration_seconds > 35:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Audio duration exceeds the 35 second limit for photo animation.",
            )

        try:
            # Perform content moderation and analysis
            image_analysis = openai_service.analyze_image(
                image_url=final_image_url_for_replicate,
            )
            if not image_analysis.has_human_face:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Animation requires an image with a clear human face. Reason: {image_analysis.reason}",
                )
            if image_analysis.is_nsfw:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Image moderation failed: The image was flagged as inappropriate. Reason: {image_analysis.reason}",
                )

        except OpenAIAPIError as e:
            raise HTTPException(
                status_code=e.status_code,
                detail=f"Content moderation check failed: {e.detail}",
            )
        except HTTPException:
            raise
        except Exception as e:
            logging.error(
                f"An unexpected error occurred during content moderation for user {user_id}: {str(e)}",
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="An unexpected error occurred during content moderation.",
            )

        # Pre-check credits
        if settings.charges_billing:
            try:
                billing_entity = get_billing_entity(session, user_id, organization_id)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail="Billing is not set up. Please add a payment method first.",
                )
            video_cost = settings.video_generation_cost * billable_duration
            if not billing_entity.has_sufficient_credits(Decimal(str(video_cost))):
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail="Insufficient credits to generate video.",
                )

        prediction = replicate_service.create_video_animation(
            image_url=final_image_url_for_replicate,
            audio_url=final_audio_url_for_replicate,
            seed=seed,
        )

        _prediction_owners[prediction.id] = user_id

        # Deduct credits after successful prediction creation
        if settings.charges_billing:
            from orchestra.db.dao.billing_account_dao import BillingAccountDAO

            billing_entity = get_billing_entity(session, user_id, organization_id)
            video_cost = settings.video_generation_cost * billable_duration
            BillingAccountDAO(session).deduct_credits(
                billing_entity.billing_account_id,
                float(video_cost),
                category="media",
                user_id=user_id,
                organization_id=organization_id,
                description="Video animation",
                detail={
                    "event": "video_animate",
                    "duration_seconds": billable_duration,
                },
            )
            session.commit()

        response_data = ReplicatePredictionResponse.from_orm(prediction)
        return InfoResponse(info=response_data)

    except ReplicateAPIError as e:
        session.rollback()
        raise HTTPException(
            status_code=e.status_code,
            detail=f"Replicate API error: {e.detail}",
        )
    except (
        HTTPException
    ):  # Re-raise if it's already an HTTPException (e.g. from input validation)
        session.rollback()
        raise
    except Exception as e:
        session.rollback()
        logging.error(f"Error animating video for user {user_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not animate video",
        )
    finally:
        # NOTE: Do NOT delete temp files here. The prediction runs
        # asynchronously on Replicate and needs to download these files
        # after the endpoint returns. Temp files in the ``tmp/`` folder
        # are cleaned up by a scheduled job (see temp_file_cleanup routine).
        pass


@router.get(
    "/assistant/photo/animate/{prediction_id}",
    response_model=InfoResponse[ReplicatePredictionResponse],
    status_code=status.HTTP_200_OK,
    summary="Get animation prediction status",
    description="Retrieves the status and result of a video animation job.",
    tags=["Media"],
)
def get_animation_prediction(
    prediction_id: str,
    request: Request,
    replicate_service: ReplicateService = Depends(),
):
    user_id = request.state.user_id
    owner = _prediction_owners.get(prediction_id)
    if owner is not None and owner != user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Prediction not found.",
        )
    try:
        prediction = replicate_service.get_prediction(prediction_id)
        response_data = ReplicatePredictionResponse.from_orm(prediction)
        return InfoResponse(info=response_data)
    except ReplicateAPIError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail=f"Replicate API error: {e.detail}",
        )


@router.post(
    "/assistant/photo/animate/{prediction_id}/cancel",
    response_model=InfoResponse[ReplicatePredictionResponse],
    status_code=status.HTTP_200_OK,
    summary="Cancel animation prediction",
    description="Cancels a running video animation job.",
    tags=["Media"],
)
def cancel_animation_prediction(
    prediction_id: str,
    request: Request,
    replicate_service: ReplicateService = Depends(),
):
    user_id = request.state.user_id
    owner = _prediction_owners.get(prediction_id)
    if owner is not None and owner != user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Prediction not found.",
        )
    try:
        prediction = replicate_service.cancel_prediction(prediction_id)
        response_data = ReplicatePredictionResponse.from_orm(prediction)
        return InfoResponse(info=response_data)
    except ReplicateAPIError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail=f"Replicate API error: {e.detail}",
        )


##################
# Admin endpoints #
##################


@admin_router.get(
    "/assistant/{assistant_id}/status",
    response_model=InfoResponse[AssistantStatus],
    status_code=status.HTTP_200_OK,
    summary="Admin: Get assistant service status",
    description="Retrieves the live status of a specific assistant's running service. Prioritizes a configured admin key, but can fall back to the request's auth header.",
    tags=["Assistants", "Admin"],
    responses={
        200: {
            "description": "Assistant status retrieved successfully.",
        },
        404: {
            "description": "Assistant service not found or not responding.",
        },
        500: {
            "description": "Configuration or authorization error.",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "ASSISTANT_ADMIN_KEY is not configured, and a valid Bearer token was not provided in the request header as a fallback.",
                    },
                },
            },
        },
        503: {
            "description": "Could not connect to the assistant service.",
        },
    },
)
async def admin_get_assistant_status(
    assistant_id: str,
    request: Request,
) -> InfoResponse[AssistantStatus]:
    """
    Get the live status of an assistant's dedicated service.
    """
    try:
        runtime_status = await get_runtime_status(assistant_id)
        active_job_names = []
        if runtime_status is not None:
            active_job_names = list(runtime_status.get("active_job_names") or [])
        if active_job_names:
            return InfoResponse(
                info=AssistantStatus(running=True, job_name=active_job_names[0]),
            )
        else:
            return InfoResponse(info=AssistantStatus(running=False, job_name=None))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get assistant status: {str(e)}",
        )


def _contact_membership_read(row: ContactMembership) -> ContactMembershipRead:
    """Serialize a contact-membership ORM row for admin responses."""

    return ContactMembershipRead(
        id=int(row.id),
        assistant_id=int(row.assistant_id),
        authoring_assistant_id=(
            int(row.authoring_assistant_id)
            if row.authoring_assistant_id is not None
            else None
        ),
        contact_id=int(row.contact_id),
        target_scope=str(row.target_scope),
        target_team_id=(
            int(row.target_team_id) if row.target_team_id is not None else None
        ),
        relationship=str(row.relationship),
        should_respond=bool(row.should_respond),
        response_policy=str(row.response_policy),
        can_edit=bool(row.can_edit),
        created_at=row.created_at,
    )


def _select_contact_membership(
    session: Session,
    *,
    assistant_id: int,
    contact_id: int,
    target_scope: str,
    target_team_id: int | None,
) -> ContactMembership | None:
    query = session.query(ContactMembership).filter(
        ContactMembership.assistant_id == assistant_id,
        ContactMembership.contact_id == contact_id,
        ContactMembership.target_scope == target_scope,
    )
    if target_scope == CONTACT_MEMBERSHIP_SCOPE_PERSONAL:
        query = query.filter(ContactMembership.target_team_id.is_(None))
    else:
        query = query.filter(ContactMembership.target_team_id == target_team_id)
    return query.order_by(ContactMembership.id).first()


@admin_router.post(
    "/assistant/{assistant_id}/contact-memberships",
    response_model=InfoResponse[ContactMembershipUpsertResponse],
    status_code=status.HTTP_200_OK,
    summary="Admin: create contact membership",
    tags=["Assistants", "Admin"],
)
@router.post(
    "/assistant/{assistant_id}/contact-memberships",
    response_model=InfoResponse[ContactMembershipUpsertResponse],
    status_code=status.HTTP_200_OK,
    summary="Create contact membership",
    tags=["Assistants"],
)
def create_contact_membership(
    assistant_id: int,
    request_body: ContactMembershipCreate,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[ContactMembershipUpsertResponse]:
    """Create an assistant-owned contact relationship overlay idempotently."""

    is_admin_request = request.url.path.startswith("/v0/admin/")
    assistant = session.get(Assistant, assistant_id)
    if assistant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )
    if not is_admin_request and assistant.user_id != request.state.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to manage contact memberships for this assistant.",
        )

    if request_body.target_scope == CONTACT_MEMBERSHIP_SCOPE_TEAM:
        membership = (
            session.query(TeamAssistantMembership)
            .join(Team, Team.id == TeamAssistantMembership.team_id)
            .filter(
                TeamAssistantMembership.assistant_id == assistant_id,
                TeamAssistantMembership.team_id == request_body.target_team_id,
                Team.status == TEAM_STATUS_ACTIVE,
            )
            .first()
        )
        if membership is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Assistant is not a live member of the target team.",
            )

    values = {
        "assistant_id": assistant_id,
        "authoring_assistant_id": assistant_id,
        "contact_id": request_body.contact_id,
        "target_scope": request_body.target_scope,
        "target_team_id": request_body.target_team_id,
        "relationship": request_body.relationship,
        "should_respond": request_body.should_respond,
        "response_policy": request_body.response_policy,
        "can_edit": request_body.can_edit,
    }
    insert_stmt = postgres_insert(ContactMembership).values(**values)
    if request_body.target_scope == CONTACT_MEMBERSHIP_SCOPE_PERSONAL:
        insert_stmt = insert_stmt.on_conflict_do_nothing(
            index_elements=[
                ContactMembership.assistant_id,
                ContactMembership.contact_id,
            ],
            index_where=(
                ContactMembership.target_scope == CONTACT_MEMBERSHIP_SCOPE_PERSONAL
            ),
        )
    else:
        insert_stmt = insert_stmt.on_conflict_do_nothing(
            index_elements=[
                ContactMembership.assistant_id,
                ContactMembership.contact_id,
                ContactMembership.target_team_id,
            ],
            index_where=(
                ContactMembership.target_scope == CONTACT_MEMBERSHIP_SCOPE_TEAM
            ),
        )
    inserted_id = session.execute(
        insert_stmt.returning(ContactMembership.id),
    ).scalar_one_or_none()
    session.flush()

    row = None
    created = inserted_id is not None
    if inserted_id is not None:
        row = session.get(ContactMembership, inserted_id)
    if row is None:
        row = _select_contact_membership(
            session,
            assistant_id=assistant_id,
            contact_id=request_body.contact_id,
            target_scope=request_body.target_scope,
            target_team_id=request_body.target_team_id,
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Contact membership could not be resolved after insert.",
        )
    session.commit()
    return InfoResponse(
        info=ContactMembershipUpsertResponse(
            membership=_contact_membership_read(row),
            created=created,
        ),
    )


@admin_router.delete(
    "/assistant/{assistant_id}/contact-memberships/{contact_id}",
    response_model=InfoResponse[ContactMembershipDeleteResponse],
    status_code=status.HTTP_200_OK,
    summary="Admin: delete contact memberships",
    tags=["Assistants", "Admin"],
)
@router.delete(
    "/assistant/{assistant_id}/contact-memberships/{contact_id}",
    response_model=InfoResponse[ContactMembershipDeleteResponse],
    status_code=status.HTTP_200_OK,
    summary="Delete contact memberships",
    tags=["Assistants"],
)
def delete_contact_memberships(
    assistant_id: int,
    contact_id: int,
    request: Request,
    target_scope: Literal["personal", "team"] = Query(...),
    target_team_id: Optional[int] = Query(None),
    session: Session = Depends(get_db_session),
) -> InfoResponse[ContactMembershipDeleteResponse]:
    """Delete the relationship overlay for one assistant/contact target."""

    is_admin_request = request.url.path.startswith("/v0/admin/")
    assistant = session.get(Assistant, assistant_id)
    if assistant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )
    if not is_admin_request and assistant.user_id != request.state.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to manage contact memberships for this assistant.",
        )

    if target_scope == CONTACT_MEMBERSHIP_SCOPE_PERSONAL and target_team_id is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="personal contact memberships cannot include target_team_id",
        )
    if target_scope == CONTACT_MEMBERSHIP_SCOPE_TEAM and target_team_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="team contact memberships require target_team_id",
        )

    stmt = delete(ContactMembership).where(
        ContactMembership.assistant_id == assistant_id,
        ContactMembership.contact_id == contact_id,
        ContactMembership.target_scope == target_scope,
    )
    if target_scope == CONTACT_MEMBERSHIP_SCOPE_PERSONAL:
        stmt = stmt.where(ContactMembership.target_team_id.is_(None))
    else:
        stmt = stmt.where(ContactMembership.target_team_id == target_team_id)

    result = session.execute(
        stmt,
    )
    session.commit()
    return InfoResponse(
        info=ContactMembershipDeleteResponse(deleted=int(result.rowcount or 0)),
    )


@admin_router.post(
    "/assistant/update-user",
    response_model=AdminUpdateUserByAssistantResponse,
    status_code=status.HTTP_200_OK,
    summary="Admin: Update user details via assistant lookup",
    description="Updates a user's profile (timezone, bio) by looking up the assistant. "
    "For personal assistants, updates the owner. "
    "For org assistants, finds the member by email and updates them.",
    tags=["Assistants", "Admin"],
    responses={
        200: {
            "description": "User updated successfully.",
            "content": {
                "application/json": {
                    "example": {
                        "info": "User updated successfully",
                        "user_id": "abc123",
                        "email": "user@example.com",
                        "assistant_type": "personal",
                    },
                },
            },
        },
        404: {
            "description": "Assistant or user not found.",
            "content": {
                "application/json": {
                    "example": {"detail": "Assistant not found."},
                },
            },
        },
        422: {
            "description": "Validation error (e.g., invalid timezone).",
        },
    },
)
def admin_update_user_by_assistant(
    request_body: AdminUpdateUserByAssistant,
    session: Session = Depends(get_db_session),
) -> AdminUpdateUserByAssistantResponse:
    """
    Update a user's profile by looking up an assistant.

    For personal assistants: updates the owner's profile if email matches.
    For org assistants: finds the org member by email and updates their profile.
    """
    assistant_dao = AssistantDAO(session)

    # Get assistant without user/org context (admin bypass)
    assistant = assistant_dao.get_assistant_by_agent_id(request_body.assistant_id)
    if not assistant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Assistant with id {request_body.assistant_id} not found.",
        )

    return _update_user_via_assistant(session, assistant, request_body)


@router.post(
    "/assistant/update-user",
    response_model=AdminUpdateUserByAssistantResponse,
    status_code=status.HTTP_200_OK,
    summary="Update user details via an owned assistant",
    description="Ownership-scoped equivalent of the admin route: updates a "
    "user's profile (timezone, bio) by looking up an assistant the caller "
    "owns. For personal assistants, updates the owner. For org assistants, "
    "finds the member by email and updates them.",
    tags=["Assistants"],
    include_in_schema=False,
)
def update_user_by_owned_assistant(
    request_body: AdminUpdateUserByAssistant,
    request: Request,
    session: Session = Depends(get_db_session),
) -> AdminUpdateUserByAssistantResponse:
    assistant = require_owned_assistant(
        request,
        request_body.assistant_id,
        session,
        write=True,
    )
    return _update_user_via_assistant(session, assistant, request_body)


def _update_user_via_assistant(
    session: Session,
    assistant: Assistant,
    request_body: AdminUpdateUserByAssistant,
) -> AdminUpdateUserByAssistantResponse:
    """Resolve the target user through ``assistant`` and apply the update."""
    user_dao = UserDAO(session)
    org_member_dao = OrganizationMemberDAO(session)

    target_user_id = None
    assistant_type = "personal"

    if assistant.organization_id is None:
        # Personal assistant: check if target_user_email matches owner
        assistant_type = "personal"
        owner = user_dao.get_by_id(assistant.user_id)
        if not owner:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Assistant owner not found.",
            )
        # owner is a tuple (User,)
        owner_user = owner[0]
        if owner_user.email != request_body.target_user_email:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Target user '{request_body.target_user_email}' does not match "
                f"assistant owner.",
            )
        target_user_id = owner_user.id
    else:
        # Org assistant: find member by email
        assistant_type = "organization"
        members = org_member_dao.filter(organization_id=assistant.organization_id)

        # Find member whose email matches target_user_email
        for member_tuple in members:
            member = member_tuple[0]
            user_row = user_dao.get_by_id(member.user_id)
            if user_row:
                user = user_row[0]
                if user.email == request_body.target_user_email:
                    target_user_id = user.id
                    break

        if target_user_id is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Target user '{request_body.target_user_email}' not found "
                f"in organization.",
            )

    # Build update kwargs (only include non-None values)
    update_kwargs = {}
    if request_body.timezone is not None:
        update_kwargs["timezone"] = request_body.timezone
    if request_body.bio is not None:
        update_kwargs["bio"] = request_body.bio

    if not update_kwargs:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields to update. Provide at least 'timezone' or 'bio'.",
        )

    # Update the user
    try:
        user_dao.update(id=target_user_id, **update_kwargs)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        )

    return AdminUpdateUserByAssistantResponse(
        info="User updated successfully",
        user_id=target_user_id,
        email=request_body.target_user_email,
        assistant_type=assistant_type,
    )


@admin_router.patch(
    "/assistant/{assistant_id}",
    response_model=AdminUpdateAssistantResponse,
    status_code=status.HTTP_200_OK,
    summary="Admin: Update assistant details",
    description="Updates an assistant's details (timezone, about) directly, "
    "bypassing permission checks.",
    tags=["Assistants", "Admin"],
    responses={
        200: {
            "description": "Assistant updated successfully.",
            "content": {
                "application/json": {
                    "example": {
                        "info": "Assistant updated successfully",
                        "assistant_id": 123,
                        "updated_fields": ["timezone", "about"],
                    },
                },
            },
        },
        404: {
            "description": "Assistant not found.",
            "content": {
                "application/json": {
                    "example": {"detail": "Assistant not found."},
                },
            },
        },
        422: {
            "description": "Validation error (e.g., invalid timezone).",
        },
    },
)
def admin_update_assistant(
    assistant_id: int,
    request_body: AdminUpdateAssistant,
    session: Session = Depends(get_db_session),
) -> AdminUpdateAssistantResponse:
    """
    Update an assistant's details directly (admin bypass).

    Updates timezone and/or about fields without requiring user context.
    """
    assistant_dao = AssistantDAO(session)

    # Get assistant without user/org context (admin bypass)
    assistant = assistant_dao.get_assistant_by_agent_id(assistant_id)
    if not assistant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Assistant with id {assistant_id} not found.",
        )

    return _apply_assistant_runtime_update(session, assistant, request_body)


@router.patch(
    "/assistant/{assistant_id}/runtime-profile",
    response_model=AdminUpdateAssistantResponse,
    status_code=status.HTTP_200_OK,
    summary="Update runtime profile fields for an owned assistant",
    description="Ownership-scoped equivalent of the admin assistant PATCH: "
    "updates timezone, about, job_title, desktop_filesync_sshkey, and "
    "console_config for an assistant the caller owns.",
    tags=["Assistants"],
    include_in_schema=False,
)
def update_assistant_runtime_profile(
    assistant_id: int,
    request_body: AdminUpdateAssistant,
    request: Request,
    session: Session = Depends(get_db_session),
) -> AdminUpdateAssistantResponse:
    assistant = require_owned_assistant(request, assistant_id, session, write=True)
    return _apply_assistant_runtime_update(session, assistant, request_body)


def _apply_assistant_runtime_update(
    session: Session,
    assistant: Assistant,
    request_body: AdminUpdateAssistant,
) -> AdminUpdateAssistantResponse:
    """Apply the runtime-profile field updates shared by admin and user routes."""
    assistant_id = assistant.agent_id

    # Build update dict and track updated fields
    updated_fields = []

    if request_body.timezone is not None:
        assistant.timezone = request_body.timezone
        updated_fields.append("timezone")
        try:
            record_timezone_pool_location_intent(session, assistant=assistant)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(exc),
            ) from exc

    if request_body.about is not None:
        assistant.about = request_body.about
        updated_fields.append("about")

    # job_title is treated like ``about`` — we let an explicit ``None`` (sent
    # by the contact-sync helper when the user clears the field on the
    # assistant contact) clear the column. The schema validator already trims
    # whitespace and normalizes blanks to ``None``.
    if "job_title" in request_body.model_fields_set:
        assistant.job_title = request_body.job_title
        updated_fields.append("job_title")

    if request_body.desktop_filesync_sshkey is not None:
        assistant.desktop_filesync_sshkey = request_body.desktop_filesync_sshkey
        updated_fields.append("desktop_filesync_sshkey")

    if "console_config" in request_body.model_fields_set:
        if request_body.console_config is None:
            if assistant.console_config is not None:
                session.delete(assistant.console_config)
                assistant.console_config = None
            updated_fields.append("console_config")
        else:
            cc = request_body.console_config
            layout = cc.get("layout", {})
            tabs = cc.get("tabs") or {}
            theme = cc.get("theme") or {}
            if assistant.console_config is None:
                assistant.console_config = AssistantConsoleConfig(
                    assistant_id=assistant_id,
                )
            cfg = assistant.console_config
            cfg.version = cc.get("version", "1")
            cfg.layout_mode = layout.get("mode", "standard")
            cfg.layout_default_tab = layout.get("defaultTab")
            cfg.tabs_hidden = tabs.get("hidden")
            cfg.tabs_order = tabs.get("order")
            cfg.theme_brand_name = theme.get("brandName")
            cfg.theme_accent_color = theme.get("accentColor")
            updated_fields.append("console_config")

    if not updated_fields:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields to update. Provide at least one field.",
        )

    # Commit changes
    session.commit()

    return AdminUpdateAssistantResponse(
        info="Assistant updated successfully",
        assistant_id=assistant_id,
        updated_fields=updated_fields,
    )


def _admin_list_has_contact_filter(
    *,
    phone: Optional[str],
    user_phone: Optional[str],
    email: Optional[str],
    user_whatsapp_number: Optional[str],
    assistant_whatsapp_number: Optional[str],
) -> bool:
    return any(
        value is not None
        for value in (
            phone,
            user_phone,
            email,
            user_whatsapp_number,
            assistant_whatsapp_number,
        )
    )


def _raise_if_ambiguous_universal_admin_contact_lookup(
    *,
    agent_id: Optional[int],
    phone: Optional[str],
    email: Optional[str],
    assistant_whatsapp_number: Optional[str],
) -> None:
    if is_ambiguous_universal_admin_contact_lookup(
        agent_id=agent_id,
        email=email,
        phone=phone,
        assistant_whatsapp_number=assistant_whatsapp_number,
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=AMBIGUOUS_UNIVERSAL_ADMIN_LOOKUP_DETAIL,
        )


# Broad fleet listing without an explicit page size used to hydrate every
# assistant in one request (minutes under load). Cap the default page and
# skip expensive hydration unless the caller opts in via from_fields / a
# narrow filter (agent_id or contact identity).
_ADMIN_LIST_DEFAULT_LIMIT = 100


def _admin_list_uses_slim_hydration(
    *,
    requested_fields: Optional[set[str]],
    has_contact_filter: bool,
    is_narrow_lookup: bool,
) -> bool:
    """Skip expensive per-assistant hydration unless a full narrow read was requested.

    Narrow lookups (``agent_id`` or contact filter) keep full hydration when
    ``from_fields`` is omitted so single-assistant admin reads stay complete.
    Broad fleet lists always slim unless the caller names the fields they need.
    """
    if has_contact_filter:
        return True
    if requested_fields is not None:
        return True
    if not is_narrow_lookup:
        return True
    return False


@admin_router.get(
    "/assistant",
    summary="Admin: list all assistants",
    description="List assistants with optional filtering. Broad fleet lists default to "
    "a page size of 100 and slim hydration; pass limit/offset and from_fields to page "
    "or opt into expensive fields. Narrow lookups (agent_id / contact filter) keep "
    "full hydration when from_fields is omitted.",
    tags=["Assistants", "Admin"],
)
def admin_list_all_assistants(
    phone: Optional[str] = Query(
        None,
        description="Only return assistants whose phone number matches this E.164-style value (leading '+' is URL-encoded).",
    ),
    user_phone: Optional[str] = Query(
        None,
        description="Only return assistants whose user phone number matches this value.",
    ),
    email: Optional[str] = Query(
        None,
        description="Only return assistants whose email address matches this value.",
    ),
    user_whatsapp_number: Optional[str] = Query(
        None,
        description="Only return assistants whose user WhatsApp number matches this value.",
    ),
    assistant_whatsapp_number: Optional[str] = Query(
        None,
        description="Only return assistants whose assistant WhatsApp number matches this value.",
    ),
    agent_id: Optional[int] = Query(
        None,
        description="Only return assistants whose agent_id matches this value.",
    ),
    require_secret_names: Optional[str] = Query(
        None,
        description="Comma-separated secret names; only return assistants that have at "
        "least one of these secrets stored (e.g. 'MICROSOFT_REFRESH_TOKEN'). Lets "
        "provider-scoped callers (token-refresh cron) skip enumerating every assistant.",
        example="MICROSOFT_REFRESH_TOKEN",
    ),
    secret_names: Optional[str] = Query(
        None,
        description="Comma-separated secret names to include in the returned 'secrets' "
        "map; other secrets are omitted. Shrinks the payload for scoped callers.",
        example="MICROSOFT_ACCESS_TOKEN,MICROSOFT_REFRESH_TOKEN",
    ),
    limit: Optional[int] = Query(
        None,
        ge=1,
        le=1000,
        description="Maximum number of assistants to return (pagination over a stable "
        "agent_id ordering). Combine with 'offset' to page through results. Broad "
        f"fleet lists default to {_ADMIN_LIST_DEFAULT_LIMIT} when omitted; narrow "
        "lookups (agent_id / contact filter) stay uncapped.",
    ),
    offset: int = Query(
        0,
        ge=0,
        description="Pagination offset; use together with 'limit'.",
    ),
    from_fields: Optional[str] = Query(
        None,
        description="Comma-separated list of fields to return (e.g., 'email,agent_id,phone'). "
        "On broad fleet lists, omitting this skips expensive lookups (api_key, teams, "
        "contact identity roots, secrets). Narrow lookups (agent_id / contact filter) "
        "still return full AssistantRead objects when from_fields is omitted.",
        example="email,agent_id,first_name",
    ),
    session: Session = Depends(get_db_session),
):
    """
    List assistants with optional filtering and field selection.

    Broad fleet lists (no agent_id / contact filter) default to a page size of
    100 and slim hydration so callers cannot accidentally hydrate the whole
    fleet in one request. Pass ``limit`` + ``offset`` to page, and ``from_fields``
    to opt into specific expensive fields. Narrow lookups keep full hydration
    when ``from_fields`` is omitted.
    """
    # Normalize filter parameters to handle URL-decoded '+' characters
    phone = normalize_phone_parameter(phone)
    user_phone = normalize_phone_parameter(user_phone)
    user_whatsapp_number = normalize_phone_parameter(user_whatsapp_number)
    assistant_whatsapp_number = normalize_phone_parameter(assistant_whatsapp_number)
    assistant_dao = AssistantDAO(session)
    api_key_dao = ApiKeyDAO(session)
    user_dao = UserDAO(session)

    # Dynamically get all valid field names from AssistantRead model
    VALID_FIELDS = set(AssistantRead.model_fields.keys())

    # Parse and validate requested fields before any database operations
    requested_fields: Optional[set] = None
    if from_fields is not None and from_fields.strip():
        raw_fields = [f.strip() for f in from_fields.split(",") if f.strip()]

        if not raw_fields:
            raise HTTPException(
                status_code=422,
                detail="The 'from_fields' parameter cannot be empty. Provide comma-separated field names.",
            )

        invalid_fields = [f for f in raw_fields if f not in VALID_FIELDS]
        if invalid_fields:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid field name(s): {', '.join(sorted(invalid_fields))}. "
                f"Valid fields are: {', '.join(sorted(VALID_FIELDS))}",
            )

        requested_fields = set(raw_fields)

    require_secret_names_list: Optional[list[str]] = None
    if require_secret_names and require_secret_names.strip():
        require_secret_names_list = [
            s.strip() for s in require_secret_names.split(",") if s.strip()
        ] or None
    secret_names_list: Optional[list[str]] = None
    if secret_names and secret_names.strip():
        secret_names_list = [
            s.strip() for s in secret_names.split(",") if s.strip()
        ] or None

    has_contact_filter = _admin_list_has_contact_filter(
        phone=phone,
        user_phone=user_phone,
        email=email,
        user_whatsapp_number=user_whatsapp_number,
        assistant_whatsapp_number=assistant_whatsapp_number,
    )
    is_narrow_lookup = agent_id is not None or has_contact_filter
    if not is_narrow_lookup and limit is None:
        limit = _ADMIN_LIST_DEFAULT_LIMIT
    _raise_if_ambiguous_universal_admin_contact_lookup(
        agent_id=agent_id,
        phone=phone,
        email=email,
        assistant_whatsapp_number=assistant_whatsapp_number,
    )
    use_slim_hydration = _admin_list_uses_slim_hydration(
        requested_fields=requested_fields,
        has_contact_filter=has_contact_filter,
        is_narrow_lookup=is_narrow_lookup,
    )
    # Full default hydrate only for narrow lookups without from_fields.
    load_expensive_defaults = requested_fields is None and not use_slim_hydration

    try:
        assistants = assistant_dao.list_all_assistants(
            phone=phone,
            user_phone=user_phone,
            email=email,
            user_whatsapp_number=user_whatsapp_number,
            assistant_whatsapp_number=assistant_whatsapp_number,
            agent_id=agent_id,
            require_secret_names=require_secret_names_list,
            limit=limit,
            offset=offset,
        )

        # Get API key based on assistant type (personal vs organizational)
        def get_api_key_for_assistant(assistant):
            if assistant.organization_id is None:
                keys = api_key_dao.get_personal_keys(assistant.user_id)
            else:
                keys = api_key_dao.get_organization_keys(
                    assistant.user_id,
                    assistant.organization_id,
                )
            return keys[0][0].key if keys else None

        # Perform expensive lookups only if needed
        api_keys = (
            [get_api_key_for_assistant(a) for a in assistants]
            if (
                load_expensive_defaults
                or (requested_fields is not None and "api_key" in requested_fields)
            )
            else None
        )
        users = (
            [user_dao.get_by_id(a.user_id)[0] for a in assistants]
            if (
                load_expensive_defaults
                or (
                    requested_fields is not None
                    and bool(
                        requested_fields
                        & {
                            "user_email",
                            "user_first_name",
                            "user_last_name",
                            "user_image",
                            "user_whatsapp_number",
                        },
                    )
                )
            )
            else None
        )

        skip_teams = (
            requested_fields is not None and "team_ids" not in requested_fields
        ) or (requested_fields is None and use_slim_hydration)
        skip_team_summaries = (
            requested_fields is not None and "team_summaries" not in requested_fields
        ) or use_slim_hydration
        skip_contact_ids = (
            requested_fields is not None
            and not ({"self_contact_id", "boss_contact_id"} & requested_fields)
        ) or (requested_fields is None and use_slim_hydration)
        skip_contact_identity_roots = use_slim_hydration and (
            requested_fields is None or "contact_identity_roots" not in requested_fields
        )
        resolve_slack_install = not use_slim_hydration or (
            requested_fields is not None
            and bool(
                {"assistant_slack_bot_user_id", "assistant_slack_team_id"}
                & requested_fields,
            )
        )
        resolve_ms_teams_install = not use_slim_hydration or (
            requested_fields is not None
            and bool(
                {"assistant_has_ms_teams_bot", "assistant_ms_teams_tenant_id"}
                & requested_fields,
            )
        )
        include_internal = not use_slim_hydration or (
            requested_fields is not None
            and ({"user_desktops", "user_desktop_filesync_keys"} & requested_fields)
        )
        if has_contact_filter and requested_fields is None:
            skip_contact_ids = False
            skip_teams = False

        # Batch-fetch contacts for all assistants (avoids N+1 queries)
        contact_dao = AssistantContactDAO(session)
        all_contacts = contact_dao.get_active_contacts_for_assistants(
            [a.agent_id for a in assistants],
        )
        contacts_by_assistant: dict[int, list] = {}
        for c in all_contacts:
            contacts_by_assistant.setdefault(c.assistant_id, []).append(c)

        # Batch-fetch secrets for all assistants
        from orchestra.db.models.orchestra_models import AssistantSecret

        skip_secrets = (
            requested_fields is not None and "secrets" not in requested_fields
        ) or (requested_fields is None and use_slim_hydration)
        secrets_by_assistant: dict[int, dict[str, str]] = {}
        if not skip_secrets:
            agent_ids = [a.agent_id for a in assistants]
            if agent_ids:
                secret_query = session.query(AssistantSecret).filter(
                    AssistantSecret.agent_id.in_(agent_ids),
                )
                # Scoped callers only need a few secret keys; filtering here
                # shrinks both the query result and the serialized payload.
                if secret_names_list:
                    secret_query = secret_query.filter(
                        AssistantSecret.secret_name.in_(secret_names_list),
                    )
                all_secret_rows = secret_query.all()
                for s in all_secret_rows:
                    secrets_by_assistant.setdefault(s.agent_id, {})[
                        s.secret_name
                    ] = s.secret_value

        team_ids_by_assistant = {}
        team_summaries_by_assistant = {}
        team_dao = TeamDAO(session)
        agent_ids = [a.agent_id for a in assistants]
        if not skip_teams:
            team_ids_by_assistant = team_dao.team_ids_for_assistants(agent_ids)
        if not skip_team_summaries:
            team_summaries_by_assistant = team_dao.team_summaries_for_assistants(
                agent_ids,
            )
        contact_ids_by_assistant = {}
        if not skip_contact_ids:
            contact_ids_by_assistant = _resolved_contact_ids_for_assistants(
                session,
                agent_ids,
                repair_missing_personal_overlays=True,
            )
        contact_identity_roots_by_assistant = {}
        if not skip_contact_identity_roots:
            identity_team_ids_by_assistant = team_ids_by_assistant
            if skip_teams:
                identity_team_ids_by_assistant = team_dao.team_ids_for_assistants(
                    agent_ids,
                )
            contact_identity_roots_by_assistant = (
                _resolved_contact_identity_roots_for_assistants(
                    session,
                    agent_ids,
                    team_ids_by_assistant=identity_team_ids_by_assistant,
                    personal_ids_by_assistant=(
                        contact_ids_by_assistant if not skip_contact_ids else None
                    ),
                )
            )

        # Build AssistantRead objects
        assistant_reads = [
            _build_assistant_read(
                a,
                session,
                api_key=api_keys[i] if api_keys else None,
                user_first_name=users[i].name if users else None,
                user_last_name=users[i].last_name if users else None,
                user_email=users[i].email if users else None,
                user_image=users[i].image if users else None,
                user_whatsapp_number=(users[i].whatsapp_number if users else None),
                team_ids=(
                    [] if skip_teams else team_ids_by_assistant.get(a.agent_id, [])
                ),
                team_summaries=(
                    []
                    if skip_team_summaries
                    else team_summaries_by_assistant.get(a.agent_id, [])
                ),
                self_contact_id=(
                    PERSONAL_SELF_CONTACT_ID
                    if skip_contact_ids
                    else _contact_id_pair(
                        contact_ids_by_assistant,
                        a.agent_id,
                    ).self_contact_id
                ),
                boss_contact_id=(
                    PERSONAL_BOSS_CONTACT_ID
                    if skip_contact_ids
                    else _contact_id_pair(
                        contact_ids_by_assistant,
                        a.agent_id,
                    ).boss_contact_id
                ),
                contact_identity_roots=(
                    []
                    if skip_contact_identity_roots
                    else contact_identity_roots_by_assistant.get(a.agent_id, [])
                ),
                contacts=contacts_by_assistant.get(a.agent_id, []),
                secrets=(
                    secrets_by_assistant.get(a.agent_id, {})
                    if not skip_secrets
                    else None
                ),
                resolve_workspace_secrets=not skip_secrets,
                resolve_slack_install=resolve_slack_install,
                resolve_ms_teams_install=resolve_ms_teams_install,
                include_internal=include_internal,
            )
            for i, a in enumerate(assistants)
        ]

        # If from_fields were requested, filter using Pydantic's model_dump
        if requested_fields is not None:
            result = [ar.model_dump(include=requested_fields) for ar in assistant_reads]
            return InfoResponse(info=result)

        # No from_fields parameter - return full AssistantRead objects
        return InfoResponse(info=assistant_reads)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Error fetching assistants: {str(e)}",
        )


@admin_router.patch(
    "/assistant",
    response_model=InfoResponse[AssistantRead],
    summary="Admin: update assistant by filter",
    description="Update a single assistant based on unique filter parameters.",
    tags=["Assistants", "Admin"],
)
def admin_update_assistant_by_filter(
    phone: Optional[str] = Query(
        None,
        description="Filter: assistant phone number",
    ),
    user_phone: Optional[str] = Query(
        None,
        description="Filter: user phone number",
    ),
    email: Optional[str] = Query(
        None,
        description="Filter: assistant email address",
    ),
    user_whatsapp_number: Optional[str] = Query(
        None,
        description="Filter: user WhatsApp number",
    ),
    assistant_whatsapp_number: Optional[str] = Query(
        None,
        description="Filter: assistant WhatsApp number",
    ),
    new_assistant_whatsapp_number: Optional[str] = Query(
        None,
        description="New WhatsApp number for the assistant",
    ),
    new_user_whatsapp_number: Optional[str] = Query(
        None,
        description="New WhatsApp number for the user",
    ),
    session: Session = Depends(get_db_session),
) -> InfoResponse[AssistantRead]:
    """
    Update a single assistant based on filter parameters.
    """
    # Normalize filter parameters and the new WhatsApp number to handle URL-decoded '+' characters
    phone = normalize_phone_parameter(phone)
    user_phone = normalize_phone_parameter(user_phone)
    user_whatsapp_number = normalize_phone_parameter(user_whatsapp_number)
    assistant_whatsapp_number = normalize_phone_parameter(assistant_whatsapp_number)
    new_assistant_whatsapp_number = normalize_phone_parameter(
        new_assistant_whatsapp_number,
    )
    new_user_whatsapp_number = normalize_phone_parameter(
        new_user_whatsapp_number,
    )

    _raise_if_ambiguous_universal_admin_contact_lookup(
        agent_id=None,
        phone=phone,
        email=email,
        assistant_whatsapp_number=assistant_whatsapp_number,
    )

    # Find the assistant to update
    dao = AssistantDAO(session)
    api_key_dao = ApiKeyDAO(session)
    assistants = dao.list_all_assistants(
        phone=phone,
        user_phone=user_phone,
        email=email,
        user_whatsapp_number=user_whatsapp_number,
        assistant_whatsapp_number=assistant_whatsapp_number,
    )
    if not assistants:
        raise HTTPException(status_code=404, detail="Assistant not found.")
    if len(assistants) > 1:
        raise HTTPException(
            status_code=400,
            detail="Multiple assistants found for filters.",
        )
    a = assistants[0]

    contact_dao = AssistantContactDAO(session)
    if new_assistant_whatsapp_number:
        whatsapp_contact = contact_dao.get_contact_by_assistant_and_type(
            a.agent_id,
            "whatsapp",
        )
        if whatsapp_contact:
            whatsapp_contact.contact_value = new_assistant_whatsapp_number
        else:
            contact_dao.upsert_assistant_contact(
                assistant_id=a.agent_id,
                contact_type="whatsapp",
                contact_value=new_assistant_whatsapp_number,
            )

    if new_user_whatsapp_number:
        user = session.get(User, a.user_id)
        if user:
            user.whatsapp_number = new_user_whatsapp_number

    session.commit()

    # Get API key based on assistant type (personal vs organizational)
    if a.organization_id is None:
        keys = api_key_dao.get_personal_keys(a.user_id)
    else:
        keys = api_key_dao.get_organization_keys(
            a.user_id,
            a.organization_id,
        )
    api_key = keys[0][0].key if keys else None

    # Return updated assistant
    return InfoResponse(
        info=_build_assistant_read(
            a,
            session,
            api_key=api_key,
            include_internal=True,
        ),
    )


@admin_router.get(
    "/assistant/user/{user_id}",
    response_model=InfoResponse[List[AssistantRead]],
    summary="Admin: list all assistants for a user",
    description="Retrieve all assistants for the specified user_id, optionally filtered by phone, email, or WhatsApp numbers.",
    tags=["Assistants", "Admin"],
)
def admin_list_assistants_for_user(
    user_id: str,
    phone: Optional[str] = Query(
        None,
        description="Only return assistants whose phone number matches this value.",
    ),
    user_phone: Optional[str] = Query(
        None,
        description="Only return assistants whose user phone number matches this value.",
    ),
    email: Optional[str] = Query(
        None,
        description="Only return assistants whose email address matches this value.",
    ),
    user_whatsapp_number: Optional[str] = Query(
        None,
        description="Only return assistants whose user WhatsApp number matches this value.",
    ),
    assistant_whatsapp_number: Optional[str] = Query(
        None,
        description="Only return assistants whose assistant WhatsApp number matches this value.",
    ),
    session: Session = Depends(get_db_session),
) -> InfoResponse[List[AssistantRead]]:
    """List all assistants belonging to a given user, with optional filtering."""
    # Normalize phone parameter to handle URL-decoded '+' characters
    phone = normalize_phone_parameter(phone)
    user_whatsapp_number = normalize_phone_parameter(user_whatsapp_number)
    assistant_whatsapp_number = normalize_phone_parameter(assistant_whatsapp_number)
    dao = AssistantDAO(session)
    api_key_dao = ApiKeyDAO(session)
    try:
        assistants = dao.list_assistants_for_user(
            user_id=user_id,
            phone=phone,
            user_phone=user_phone,
            email=email,
            user_whatsapp_number=user_whatsapp_number,
            assistant_whatsapp_number=assistant_whatsapp_number,
        )

        # Get API key based on assistant type (personal vs organizational)
        def get_api_key_for_assistant(assistant):
            if assistant.organization_id is None:
                keys = api_key_dao.get_personal_keys(assistant.user_id)
            else:
                keys = api_key_dao.get_organization_keys(
                    assistant.user_id,
                    assistant.organization_id,
                )
            return keys[0][0].key if keys else None

        api_keys = [get_api_key_for_assistant(a) for a in assistants]

        # Batch-fetch contacts for all assistants (avoids N+1 queries)
        contact_dao = AssistantContactDAO(session)
        all_contacts = contact_dao.get_active_contacts_for_assistants(
            [a.agent_id for a in assistants],
        )
        contacts_by_assistant: dict[int, list] = {}
        for c in all_contacts:
            contacts_by_assistant.setdefault(c.assistant_id, []).append(c)

        team_dao = TeamDAO(session)
        assistant_ids = [a.agent_id for a in assistants]
        team_ids_by_assistant = team_dao.team_ids_for_assistants(assistant_ids)
        team_summaries_by_assistant = team_dao.team_summaries_for_assistants(
            assistant_ids,
        )
        contact_ids_by_assistant = _resolved_contact_ids_for_assistants(
            session,
            assistant_ids,
            repair_missing_personal_overlays=True,
        )
        contact_identity_roots_by_assistant = (
            _resolved_contact_identity_roots_for_assistants(
                session,
                assistant_ids,
                team_ids_by_assistant=team_ids_by_assistant,
                personal_ids_by_assistant=contact_ids_by_assistant,
            )
        )

        return InfoResponse(
            info=[
                _build_assistant_read(
                    a,
                    session,
                    api_key=api_keys[i],
                    contacts=contacts_by_assistant.get(a.agent_id, []),
                    team_ids=team_ids_by_assistant.get(a.agent_id, []),
                    team_summaries=team_summaries_by_assistant.get(
                        a.agent_id,
                        [],
                    ),
                    self_contact_id=_contact_id_pair(
                        contact_ids_by_assistant,
                        a.agent_id,
                    ).self_contact_id,
                    boss_contact_id=_contact_id_pair(
                        contact_ids_by_assistant,
                        a.agent_id,
                    ).boss_contact_id,
                    contact_identity_roots=contact_identity_roots_by_assistant.get(
                        a.agent_id,
                        [],
                    ),
                    resolve_workspace_secrets=False,
                    include_internal=True,
                )
                for i, a in enumerate(assistants)
            ],
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Error fetching assistants for user {user_id}: {str(e)}",
        )


@admin_router.get(
    "/contacts",
    response_model=List[Contact],
    summary="Admin: list all contacts",
    description="List all contact-context logs, optionally filtered by email, phone, or WhatsApp number",
    tags=["Assistants", "Admin"],
)
def admin_list_contacts(
    email_address: Optional[str] = Query(None, description="Filter by email_address"),
    phone_number: Optional[str] = Query(None, description="Filter by phone_number"),
    whatsapp_number: Optional[str] = Query(
        None,
        description="Filter by whatsapp_number",
    ),
    limit: int = Query(1000, ge=1, le=1000, description="Max contacts to return"),
    session: Session = Depends(get_db_session),
) -> List[Contact]:
    """
    Retrieve contact-context logs matching email / phone / WhatsApp filters.

    At least one filter is required — unbounded cross-tenant Contacts scans are
    refused (partition prune + memory safety).
    """
    from typing import Any, Dict

    filters: Dict[str, Any] = {}
    if email_address is not None:
        filters["email_address"] = email_address
    if phone_number is not None:
        filters["phone_number"] = normalize_phone_parameter(phone_number)
    if whatsapp_number is not None:
        filters["whatsapp_number"] = normalize_phone_parameter(whatsapp_number)
    if not filters:
        raise HTTPException(
            status_code=400,
            detail=(
                "At least one of email_address, phone_number, or "
                "whatsapp_number is required"
            ),
        )

    # Contexts named …Contacts… with their project_id (needed for prune).
    ctx_rows = session.execute(
        select(Context.id, Context.project_id).where(Context.name.like("%Contacts%")),
    ).all()
    if not ctx_rows:
        return []

    by_project: Dict[int, List[int]] = {}
    for ctx_id, project_id in ctx_rows:
        by_project.setdefault(int(project_id), []).append(int(ctx_id))

    log_event_dao = LogEventDAO(session)
    event_ids: List[int] = []
    for project_id, ctx_ids in by_project.items():
        if len(event_ids) >= limit:
            break
        remaining = limit - len(event_ids)
        ids = log_event_dao.get_ids_by_filter(
            project_id=project_id,
            filters=filters,
            context_ids=ctx_ids,
        )
        event_ids.extend(ids[:remaining])
    if not event_ids:
        return []

    grouped: Dict[int, Dict[str, Any]] = {}
    rows = session.execute(
        select(LogEvent.id, LogEvent.data, LogEvent.project_id).where(
            LogEvent.id.in_(event_ids),
            LogEvent.project_id.in_(list(by_project.keys())),
        ),
    ).all()
    for event_id, data, _pid in rows:
        grouped[event_id] = dict(data) if data else {}

    user_rows = session.execute(
        select(LogEvent.id, Project.user_id)
        .join(Project, LogEvent.project_id == Project.id)
        .where(
            LogEvent.id.in_(event_ids),
            LogEvent.project_id.in_(list(by_project.keys())),
        ),
    )
    user_map = {evt: uid for evt, uid in user_rows}

    results = []
    for eid, data in grouped.items():
        contact: Dict[str, Any] = {}
        custom: Dict[str, Any] = {}
        for k, v in data.items():
            if k in (
                "first_name",
                "surname",
                "email_address",
                "phone_number",
                "whatsapp_number",
                "description",
            ):
                contact[k] = v
            else:
                custom[k] = v
        contact["custom_fields"] = custom
        contact["user_id"] = user_map.get(eid)
        results.append(contact)
    return results


# ============================================================================
# Spending Limit Endpoints
# ============================================================================


@router.get(
    "/assistant/{agent_id}/spending-limit",
    response_model=AssistantSpendingLimitResponse,
    tags=["Assistant Management"],
    summary="Get assistant spending limit",
    description="Get the monthly spending limit for an assistant.",
    responses={
        200: {
            "description": "Spending limit retrieved successfully",
        },
        404: {
            "description": "Assistant not found",
        },
    },
)
async def get_assistant_spending_limit(
    request: Request,
    agent_id: int,
    session: Session = Depends(get_db_session),
):
    """
    Get the monthly spending limit for an assistant.

    Returns the assistant's limit and effective limit (considering parent limits).
    """
    user_id = request.state.user_id

    # Get the assistant and verify access
    assistant_dao = AssistantDAO(session)
    assistant = assistant_dao.get_assistant_by_agent_id(agent_id)
    if not assistant:
        raise HTTPException(status_code=404, detail="Assistant not found.")
    if _is_hidden_workspace_coordinator_for_user(assistant, user_id=user_id):
        raise HTTPException(status_code=404, detail="Assistant not found.")

    # Allow org members to view limits for any assistant in their org.
    if assistant.user_id != user_id:
        if assistant.organization_id is not None:
            org_member_dao = OrganizationMemberDAO(session)
            member = org_member_dao.get_member(user_id, assistant.organization_id)
            if member is None:
                raise HTTPException(status_code=404, detail="Assistant not found.")
        else:
            raise HTTPException(status_code=404, detail="Assistant not found.")

    # Get the limit
    monthly_cap = assistant_dao.get_spending_cap(agent_id)

    # Calculate effective limit based on context
    effective_limit = monthly_cap
    if assistant.organization_id is not None:
        # Org assistant - check member and org limits
        from orchestra.db.dao.organization_dao import OrganizationDAO

        org_dao = OrganizationDAO(session)
        org_member_dao = OrganizationMemberDAO(session)

        org = org_dao.get(assistant.organization_id)
        owner_member = (
            org_member_dao.get_member(
                assistant.user_id,
                assistant.organization_id,
            )
            # Team-owned assistants bill the organization; the hiring
            # member's personal cap does not bound them.
            if assistant.owner_team_id is None
            else None
        )

        parent_limits = []
        if owner_member and owner_member.monthly_spending_cap is not None:
            parent_limits.append(float(owner_member.monthly_spending_cap))
        if org and org.monthly_spending_cap is not None:
            parent_limits.append(float(org.monthly_spending_cap))

        if parent_limits:
            parent_limit = min(parent_limits)
            if effective_limit is None:
                effective_limit = parent_limit
            else:
                effective_limit = min(effective_limit, parent_limit)
    else:
        # Personal assistant - check user limit
        user_row = UserDAO(session).get_by_id(user_id)
        if user_row:
            user = user_row[0]
            if user.monthly_spending_cap is not None:
                parent_limit = float(user.monthly_spending_cap)
                if effective_limit is None:
                    effective_limit = parent_limit
                else:
                    effective_limit = min(effective_limit, parent_limit)

    return AssistantSpendingLimitResponse(
        agent_id=agent_id,
        monthly_spending_cap=monthly_cap,
        effective_limit=effective_limit,
    )


@router.get("/assistant/{agent_id}/spend", response_model=AssistantSpendResponse)
async def get_assistant_spend(
    request: Request,
    agent_id: int,
    month: str = Query(
        ...,
        description="Month in YYYY-MM format",
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
        examples=["2026-01"],
    ),
    session: Session = Depends(get_db_session),
):
    """Get an assistant's cumulative spend for a given month."""
    user_id = request.state.user_id

    assistant_dao = AssistantDAO(session)
    assistant = assistant_dao.get_assistant_by_agent_id(agent_id)
    if not assistant:
        raise HTTPException(status_code=404, detail="Assistant not found.")
    if _is_hidden_workspace_coordinator_for_user(assistant, user_id=user_id):
        raise HTTPException(status_code=404, detail="Assistant not found.")

    if assistant.user_id != user_id:
        # Allow org members to view spend for any assistant in their org.
        if assistant.organization_id is not None:
            org_member_dao = OrganizationMemberDAO(session)
            member = org_member_dao.get_member(user_id, assistant.organization_id)
            if member is None:
                raise HTTPException(status_code=404, detail="Assistant not found.")
        else:
            raise HTTPException(status_code=404, detail="Assistant not found.")

    cumulative_spend = assistant_dao.get_cumulative_spend(agent_id, month)
    limit = assistant_dao.get_spending_cap(agent_id)

    percent_used = None
    if limit is not None and limit > 0:
        percent_used = round((cumulative_spend / limit) * 100, 2)

    credit_balance = None
    billing_account = None
    if assistant.organization_id is not None:
        org = (
            session.query(Organization)
            .filter(Organization.id == assistant.organization_id)
            .first()
        )
        if org and org.billing_account:
            credit_balance = float(org.billing_account.credits)
            billing_account = org.billing_account
    else:
        user = session.query(User).filter(User.id == assistant.user_id).first()
        if user and user.billing_account:
            credit_balance = float(user.billing_account.credits)
            billing_account = user.billing_account

    billing_mode = "CREDITS"
    if billing_account is not None:
        from orchestra.db.dao.billing_account_dao import BillingAccountDAO

        billing_mode = (
            BillingAccountDAO(session).resolve_billing_mode(billing_account).value
        )

    from orchestra.lib.trial_subscription import trial_gate_fields

    return AssistantSpendResponse(
        agent_id=agent_id,
        month=month,
        cumulative_spend=cumulative_spend,
        limit=limit,
        limit_set_at=assistant.monthly_spending_cap_set_at,
        percent_used=percent_used,
        credit_balance=credit_balance,
        billing_mode=billing_mode,
        **trial_gate_fields(session, billing_account),
    )


@router.put(
    "/assistant/{agent_id}/spending-limit",
    response_model=AssistantSpendingLimitResponse,
    tags=["Assistant Management"],
    summary="Set assistant spending limit",
    description="Set or update the monthly spending limit for an assistant.",
    responses={
        200: {
            "description": "Spending limit set successfully",
            "content": {
                "application/json": {
                    "example": {
                        "agent_id": 123,
                        "monthly_spending_cap": 100.00,
                        "effective_limit": 100.00,
                    },
                },
            },
        },
        400: {
            "description": "Invalid limit",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Assistant limit cannot exceed user limit ($50.00)",
                    },
                },
            },
        },
        404: {
            "description": "Assistant not found",
            "content": {
                "application/json": {
                    "example": {"detail": "Assistant not found."},
                },
            },
        },
    },
)
async def set_assistant_spending_limit(
    request: Request,
    agent_id: int,
    body: SpendingLimitRequest,
    session: Session = Depends(get_db_session),
):
    """
    Set the monthly spending limit for an assistant.

    For personal assistants (no organization):
    - Limit cannot exceed the user's personal spending limit

    For organizational assistants:
    - Limit cannot exceed the member's org spending limit
    - Limit cannot exceed the organization's spending limit

    Setting to null removes the limit.
    """
    user_id = request.state.user_id
    assistant_dao = AssistantDAO(session)

    try:
        result = assistant_dao.set_spending_cap(
            agent_id=agent_id,
            user_id=user_id,
            monthly_spending_cap=body.monthly_spending_cap,
        )
        session.commit()

        return AssistantSpendingLimitResponse(
            agent_id=agent_id,
            monthly_spending_cap=result.monthly_spending_cap,
            effective_limit=result.effective_limit,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------------------
# Inactivity follow-up endpoints (admin + ownership-scoped runtime routes)
# ---------------------------------------------------------------------------


from pydantic import BaseModel, Field


class TouchActivityRequest(BaseModel):
    """Optional payload for ``POST …/touch-activity``."""

    thread_id: Optional[str] = Field(
        default=None,
        description=(
            "Gmail/Outlook thread id for the message. When it matches a "
            "stored inactivity check-in thread, activity is not stamped."
        ),
    )


def _touch_assistant_activity(
    session: Session,
    assistant_id: int,
    *,
    thread_id: str | None = None,
) -> dict:
    """Stamp fresh correspondence activity (unless this is a check-in reply)."""
    from datetime import datetime, timezone

    from orchestra.settings import settings

    dao = AssistantDAO(session)
    outcome = dao.touch_last_correspondence_at(
        assistant_id,
        datetime.now(timezone.utc),
        thread_id=thread_id,
        max_series=settings.inactivity_followup_max_series,
    )
    if outcome.get("rows_updated", 0) == 0 and not outcome.get("skipped"):
        # Distinguish unknown assistant from skipped follow-up-thread reply.
        exists = (
            session.execute(
                select(Assistant.agent_id).where(Assistant.agent_id == assistant_id),
            ).scalar_one_or_none()
            is not None
        )
        if not exists:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Assistant with id {assistant_id} not found.",
            )
    session.commit()
    return {
        "status": "success",
        "assistant_id": assistant_id,
        "rows_updated": outcome.get("rows_updated", 0),
        "skipped": bool(outcome.get("skipped")),
        "reason": outcome.get("reason"),
    }


def _set_assistant_followup_opt_out(
    session: Session,
    assistant_id: int,
    opted_out: bool,
) -> dict:
    """Toggle the inactivity follow-up opt-out flag for one assistant."""
    dao = AssistantDAO(session)
    rows = dao.set_inactivity_followup_opt_out(assistant_id, opted_out)
    if rows == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Assistant with id {assistant_id} not found.",
        )
    session.commit()
    return {"status": "success", "assistant_id": assistant_id, "rows_updated": rows}


@admin_router.post(
    "/assistant/{assistant_id}/touch-activity",
    status_code=status.HTTP_200_OK,
    summary="Admin: record correspondence activity for an assistant",
    description=(
        "Stamps ``last_correspondence_at = now()`` and re-arms the "
        "inactivity follow-up series for a future silence. Called by the "
        "Unity transcript hook on inbound/outbound messages. Optional "
        "``thread_id`` skips the stamp when the message is a reply on an "
        "inactivity check-in Gmail thread."
    ),
    tags=["Assistants", "Admin"],
)
def admin_touch_assistant_activity(
    assistant_id: int,
    session: Session = Depends(get_db_session),
    body: TouchActivityRequest = Body(default_factory=TouchActivityRequest),
) -> dict:
    return _touch_assistant_activity(
        session,
        assistant_id,
        thread_id=body.thread_id,
    )


@router.post(
    "/assistant/{assistant_id}/touch-activity",
    status_code=status.HTTP_200_OK,
    summary="Record correspondence activity for an owned assistant",
    description=(
        "Ownership-scoped equivalent of the admin route: stamps "
        "``last_correspondence_at = now()`` and re-arms follow-up cadence "
        "for an assistant the caller owns. Optional ``thread_id`` skips "
        "stamping for replies to programmatic check-in emails."
    ),
    tags=["Assistants"],
    include_in_schema=False,
)
def touch_assistant_activity(
    assistant_id: int,
    request: Request,
    session: Session = Depends(get_db_session),
    body: TouchActivityRequest = Body(default_factory=TouchActivityRequest),
) -> dict:
    require_owned_assistant(request, assistant_id, session, write=True)
    return _touch_assistant_activity(
        session,
        assistant_id,
        thread_id=body.thread_id,
    )


@admin_router.post(
    "/assistant/{assistant_id}/opt-out-followups",
    status_code=status.HTTP_200_OK,
    summary="Admin: opt an assistant out of inactivity follow-ups",
    description=(
        "Sets ``inactivity_followup_opted_out = true`` so the inactivity "
        "re-engagement routine never follows up via this Coordinator "
        "again. The Unity brain calls this when the boss explicitly asks "
        "not to be contacted further. Nothing is deleted — this only "
        "silences future follow-ups until the boss opts back in."
    ),
    tags=["Assistants", "Admin"],
)
def admin_opt_out_assistant_followups(
    assistant_id: int,
    session: Session = Depends(get_db_session),
) -> dict:
    return _set_assistant_followup_opt_out(session, assistant_id, True)


@router.post(
    "/assistant/{assistant_id}/opt-out-followups",
    status_code=status.HTTP_200_OK,
    summary="Opt an owned assistant out of inactivity follow-ups",
    description=(
        "Ownership-scoped equivalent of the admin route: sets "
        "``inactivity_followup_opted_out = true`` for an assistant the "
        "caller owns."
    ),
    tags=["Assistants"],
    include_in_schema=False,
)
def opt_out_assistant_followups(
    assistant_id: int,
    request: Request,
    session: Session = Depends(get_db_session),
) -> dict:
    require_owned_assistant(request, assistant_id, session, write=True)
    return _set_assistant_followup_opt_out(session, assistant_id, True)


@admin_router.post(
    "/assistant/{assistant_id}/opt-in-followups",
    status_code=status.HTTP_200_OK,
    summary="Admin: re-enable inactivity follow-ups for an assistant",
    description=(
        "Clears ``inactivity_followup_opted_out`` so the inactivity "
        "re-engagement routine can follow up via this Coordinator again. "
        "The Unity brain calls this when the boss re-engages after having "
        "previously opted out."
    ),
    tags=["Assistants", "Admin"],
)
def admin_opt_in_assistant_followups(
    assistant_id: int,
    session: Session = Depends(get_db_session),
) -> dict:
    return _set_assistant_followup_opt_out(session, assistant_id, False)


@router.post(
    "/assistant/{assistant_id}/opt-in-followups",
    status_code=status.HTTP_200_OK,
    summary="Re-enable inactivity follow-ups for an owned assistant",
    description=(
        "Ownership-scoped equivalent of the admin route: clears "
        "``inactivity_followup_opted_out`` for an assistant the caller owns."
    ),
    tags=["Assistants"],
    include_in_schema=False,
)
def opt_in_assistant_followups(
    assistant_id: int,
    request: Request,
    session: Session = Depends(get_db_session),
) -> dict:
    require_owned_assistant(request, assistant_id, session, write=True)
    return _set_assistant_followup_opt_out(session, assistant_id, False)
