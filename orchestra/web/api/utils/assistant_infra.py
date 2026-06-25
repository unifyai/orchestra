import asyncio
import logging
import os
import time
from typing import Any, List

import httpx
from sqlalchemy.orm import Session

from orchestra.lib.deploy_env import env_suffix
from orchestra.settings import settings
from orchestra.web.api.utils.http_client import get_async_client

COMMS_URL = os.environ.get("UNITY_COMMS_URL")
COMMUNICATION_URL = os.environ.get("COMMUNICATION_URL")
COMMS_URL_LEGACY = os.environ.get("COMMS_URL")
ADAPTERS_URL = os.environ.get("UNITY_ADAPTERS_URL")
LOCAL_ADAPTERS_URL = os.environ.get("LOCAL_ADAPTERS_URL")
UNITY_GATEWAY_URL = os.environ.get("UNITY_GATEWAY_URL")
ADMIN_KEY = os.environ.get("ORCHESTRA_ADMIN_KEY")

PERMANENT_CLEANUP_TIMEOUT_SECONDS = 10.0
RUNTIME_CLEANUP_WAIT_TIMEOUT_SECONDS = 90.0
RUNTIME_CLEANUP_POLL_INTERVAL_SECONDS = 3.0


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    """Return response JSON when present, otherwise an empty object."""
    try:
        return response.json()
    except ValueError:
        return {}


def _cleanup_step_result(
    name: str,
    *,
    success: bool,
    response: dict[str, Any] | None = None,
    error: str | None = None,
    skipped: bool = False,
    timed_out: bool = False,
    reason: str | None = None,
) -> dict[str, Any]:
    """Normalize one cleanup step into a small serializable status payload."""
    step = {"name": name, "success": success}
    if response is not None:
        step["response"] = response
    if error is not None:
        step["error"] = error
    if skipped:
        step["skipped"] = True
    if timed_out:
        step["timed_out"] = True
    if reason is not None:
        step["reason"] = reason
    return step


def _cleanup_errors_from_steps(steps: dict[str, dict[str, Any]]) -> list[str]:
    """Flatten failed step payloads into human-readable error strings."""
    errors: list[str] = []
    for step_name, step in steps.items():
        if not step.get("success"):
            errors.append(f"{step_name}: {_cleanup_step_message(step)}")
    return errors


def _cleanup_step_message(step: dict[str, Any]) -> str:
    """Return a human-readable summary for one cleanup step."""

    return str(step.get("error") or step.get("reason") or "cleanup incomplete")


def _step_response(step: dict[str, Any]) -> dict[str, Any]:
    """Return a cleanup step's JSON body when present."""

    response = step.get("response")
    return response if isinstance(response, dict) else {}


def _stop_step_reason(step: dict[str, Any]) -> str | None:
    """Return the semantic stop reason from a cleanup step."""

    reason = step.get("reason")
    if reason:
        return str(reason)
    response_reason = _step_response(step).get("reason")
    return str(response_reason) if response_reason else None


def _stop_requires_sessionless_fallback(step: dict[str, Any]) -> bool:
    """Return whether stop semantics say no AssistantSession existed."""

    if not step.get("success"):
        return False
    return _stop_step_reason(step) == "not_found"


def _is_missing_comms_config_step(step: dict[str, Any]) -> bool:
    """Return whether a cleanup step was skipped because comms is unavailable."""

    return bool(step.get("skipped")) and step.get("reason") == "missing_comms_config"


def _runtime_vm_refs(runtime_status: dict[str, Any]) -> list[dict[str, str]]:
    """Return de-duplicated VM refs from a runtime status payload."""

    refs: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for key in ("owned_vms", "other_owned_vms"):
        for raw_vm in runtime_status.get(key) or []:
            if not isinstance(raw_vm, dict):
                continue
            binding_id = str(raw_vm.get("binding_id", "") or "")
            vm_name = str(raw_vm.get("vm_name", "") or "")
            ref_key = (binding_id, vm_name)
            if not binding_id or not vm_name or ref_key in seen:
                continue
            seen.add(ref_key)
            refs.append({"binding_id": binding_id, "vm_name": vm_name})
    return refs


def _runtime_has_live_resources(runtime_status: dict[str, Any]) -> bool:
    """Return whether the assistant still has live runtime resources."""

    return bool(
        runtime_status.get("active_job_names")
        or runtime_status.get("owned_vms")
        or runtime_status.get("other_owned_vms")
        or runtime_status.get("disk_vm_name"),
    )


def _comms_url() -> str:
    for url in (COMMS_URL, COMMUNICATION_URL, COMMS_URL_LEGACY):
        if url:
            return url.rstrip("/")
    if LOCAL_ADAPTERS_URL:
        return LOCAL_ADAPTERS_URL.rstrip("/")
    if UNITY_GATEWAY_URL:
        return UNITY_GATEWAY_URL.rstrip("/")
    orchestra_url = os.environ.get("ORCHESTRA_URL", "")
    if "localhost" in orchestra_url or "127.0.0.1" in orchestra_url:
        return "http://127.0.0.1:8001"
    return ""


def _adapters_url() -> str:
    if ADAPTERS_URL:
        return ADAPTERS_URL.rstrip("/")
    return _comms_url()


_COMMS_FEATURES_TTL_SECONDS = 60.0
_comms_features_cache: dict[str, Any] = {"expires_at": 0.0, "value": None}


async def fetch_comms_features() -> dict[str, bool]:
    """Per-channel availability reported by the communication gateway.

    Orchestra owns the authoritative view of what the deployment can do and
    re-exposes it via ``/v0/features``; channel credentials (Twilio, Discord,
    Slack, …) live in the comms layer, so we probe its ``/features`` endpoint
    rather than re-deriving from Orchestra's partial env.

    Cached briefly to avoid a round-trip on every features read. On any error
    (comms unreachable, malformed payload) the last good value is returned, or an
    empty dict on a cold failure — callers treat absent keys as "off" so a
    deployment without a comms layer simply shows no channel UI.
    """
    now = time.monotonic()
    cached = _comms_features_cache
    if cached["value"] is not None and now < cached["expires_at"]:
        return cached["value"]

    comms_url = _comms_url()
    if not comms_url:
        return cached["value"] or {}

    try:
        client = get_async_client()
        response = await client.get(f"{comms_url}/features", timeout=2.0)
        if response.status_code != 200:
            return cached["value"] or {}
        data = response.json()
    except Exception:  # noqa: BLE001 - features probe must never break /features
        logging.debug("comms features probe failed", exc_info=True)
        return cached["value"] or {}

    if not isinstance(data, dict):
        return cached["value"] or {}

    value = {key: bool(val) for key, val in data.items() if isinstance(val, bool)}
    cached["value"] = value
    cached["expires_at"] = now + _COMMS_FEATURES_TTL_SECONDS
    return value


async def create_phone_number(
    phone_country: str = "US",
):
    """
    Create a phone number for the user by making a POST request to the comms endpoint.

    Args:
        phone_country (str): The country code for phone number provisioning (e.g., "US", "GB").

    Returns:
        JSON response from the phone creation endpoint
    """
    comms_url = _comms_url()
    adapters_url = _adapters_url()
    voice_url = adapters_url + "/twilio/call"
    sms_url = adapters_url + "/twilio/sms"
    status_callback = adapters_url + "/twilio/call-status"
    client = get_async_client()
    try:
        response = await client.post(
            f"{comms_url}/phone/create",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={
                "voice_url": voice_url,
                "sms_url": sms_url,
                "status_callback": status_callback,
                "phone_country": phone_country,
            },
            timeout=90.0,
        )
        return response.json()
    except httpx.TimeoutException:
        raise Exception(
            "Phone creation timed out - comms service may be cold starting",
        )


async def assign_whatsapp_pool_number(
    assistant_id: int,
    session,
) -> dict:
    """Assign a WhatsApp pool number to an assistant via the local DAO.

    Returns a dict with ``pool_number`` and ``assistant_id``.
    """
    from orchestra.db.dao.shared_pool_dao import SharedPoolDAO
    from orchestra.db.models.orchestra_models import Assistant, OrganizationMember

    assistant = (
        session.query(Assistant).filter(Assistant.agent_id == assistant_id).first()
    )
    if not assistant:
        raise ValueError(f"Assistant {assistant_id} not found.")

    user_ids = [assistant.user_id]
    if assistant.organization_id is not None:
        members = (
            session.query(OrganizationMember.user_id)
            .filter(
                OrganizationMember.organization_id == assistant.organization_id,
            )
            .all()
        )
        for (uid,) in members:
            if uid not in user_ids:
                user_ids.append(uid)

    dao = SharedPoolDAO(session)
    pool = dao.assign_pool_number(assistant_id, user_ids)
    return {"pool_number": pool.number, "assistant_id": assistant_id}


async def register_whatsapp_sender(
    phone_number: str,
) -> dict:
    """Register a WhatsApp sender with Twilio via the Communication service.

    This calls the existing ``POST /whatsapp/create`` on the Communication
    service to set up the Twilio Messaging Channel Sender and webhook.
    """
    comms_url = _comms_url()
    callback_url = _adapters_url() + "/twilio/whatsapp"
    client = get_async_client()
    response = await client.post(
        f"{comms_url}/whatsapp/create",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        json={
            "phone_number": phone_number,
            "callback_url": callback_url,
        },
        timeout=20,
    )
    return response.json()


async def delete_whatsapp_routes(
    assistant_id: int,
    session,
) -> int:
    """Delete all WhatsApp routes for an assistant.

    Returns the number of routes deleted.
    """
    from orchestra.db.dao.shared_pool_dao import SharedPoolDAO

    dao = SharedPoolDAO(session)
    return dao.delete_routes_for_assistant(assistant_id)


async def notify_pool_reassignment(
    conflict_event_id: int,
    old_number: str,
    new_number: str,
    recipients: list[dict],
    session,
) -> dict:
    """Send template-based WhatsApp notifications for a pool number change.

    Each recipient dict must contain: ``to``, ``user_name``, ``agent_name``.
    Returns per-recipient message SIDs for delivery tracking.
    """
    comms_url = _comms_url()
    client = get_async_client()
    response = await client.post(
        f"{comms_url}/whatsapp/notify",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        json={
            "from_number": old_number,
            "recipients": recipients,
            "old_contact": old_number,
            "new_contact": new_number,
            "callback_id": str(conflict_event_id),
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


async def assign_discord_pool_bot(
    assistant_id: int,
    session,
) -> dict:
    """Assign a Discord pool bot to an assistant via the local DAO.

    Returns a dict with ``pool_number`` (bot ID) and ``assistant_id``.
    """
    from orchestra.db.dao.shared_pool_dao import SharedPoolDAO
    from orchestra.db.models.orchestra_models import Assistant, OrganizationMember

    assistant = (
        session.query(Assistant).filter(Assistant.agent_id == assistant_id).first()
    )
    if not assistant:
        raise ValueError(f"Assistant {assistant_id} not found.")

    user_ids = [assistant.user_id]
    if assistant.organization_id is not None:
        members = (
            session.query(OrganizationMember.user_id)
            .filter(
                OrganizationMember.organization_id == assistant.organization_id,
            )
            .all()
        )
        for (uid,) in members:
            if uid not in user_ids:
                user_ids.append(uid)

    dao = SharedPoolDAO(session, "discord")
    pool = dao.assign_pool_number(assistant_id, user_ids)
    return {
        "pool_number": pool.number,
        "assistant_id": assistant_id,
        "auth_token": pool.auth_token,
    }


async def register_discord_bot(
    bot_id: str,
    assistant_id: int,
    bot_token: str | None = None,
) -> dict:
    """Register a Discord bot-to-assistant mapping with the Communication service."""
    comms_url = _comms_url()
    payload: dict = {
        "bot_id": bot_id,
        "assistant_id": assistant_id,
    }
    if bot_token is not None:
        payload["bot_token"] = bot_token
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{comms_url}/discord/create",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json=payload,
            timeout=20,
        )
        return response.json()


async def delete_discord_routes(
    assistant_id: int,
    session,
) -> int:
    """Delete all Discord routes for an assistant.

    Returns the number of routes deleted.
    """
    from orchestra.db.dao.shared_pool_dao import SharedPoolDAO

    dao = SharedPoolDAO(session, "discord")
    return dao.delete_routes_for_assistant(assistant_id)


async def delete_phone_number(phone_number: str):
    """
    Delete a phone number by making a DELETE request to the comms endpoint.

    Args:
        phone_number (str): The phone number to delete

    Returns:
        JSON response from the phone deletion endpoint
    """
    comms_url = _comms_url()
    client = get_async_client()
    response = await client.request(
        "DELETE",
        f"{comms_url}/phone/delete",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        json={"PhoneNumber": phone_number},
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


# Platform-issued mailbox provisioning is retired. The corresponding
# `create_email` / `create_outlook_email` and `watch_email` /
# `watch_outlook_email` helpers have been removed.
#
# `delete_email` / `delete_outlook_email` are kept below because they
# are still invoked by `orchestra.workers.teardown_platform_mailboxes`
# to deprovision the lingering platform mailboxes that existed before
# the feature was retired (and they remain idempotent for any future
# stragglers).


async def delete_email(email: str):
    """Delete a Google Workspace user via the Communication service.

    Used only by the one-shot platform-mailbox teardown worker
    (``orchestra.workers.teardown_platform_mailboxes``).  Idempotent:
    Communication returns ``{"already_absent": true}`` for users that
    have already been removed.
    """
    comms_url = _comms_url()
    client = get_async_client()
    response = await client.request(
        "DELETE",
        f"{comms_url}/gmail/delete",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        json={"primary_email": email},
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


async def delete_outlook_email(email: str):
    """Delete an MS365 user/mailbox via the Communication service.

    Used only by the one-shot platform-mailbox teardown worker
    (``orchestra.workers.teardown_platform_mailboxes``).  Idempotent.
    """
    comms_url = _comms_url()
    client = get_async_client()
    response = await client.request(
        "DELETE",
        f"{comms_url}/outlook/delete",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        json={"primary_email": email},
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


async def create_pubsub_topic(assistant_id: str):
    """
    Create a pubsub topic for the assistant by making a POST request to the comms endpoint.

    Args:
        assistant_id (str): The ID of the assistant

    Returns:
        JSON response from the pubsub topic creation endpoint
    """
    topic_name = f"unity-{assistant_id}{env_suffix()}"
    if settings.is_self_host:
        return {
            "success": True,
            "skipped": True,
            "reason": "self_host_local_provisioning",
            "topic_name": topic_name,
        }

    comms_url = _comms_url()
    client = get_async_client()
    try:
        response = await client.post(
            f"{comms_url}/infra/pubsub/topic",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            data={"topic_name": topic_name},
            timeout=30,
        )
        return response.json()
    except httpx.TimeoutException:
        raise Exception(
            "Pubsub topic creation timed out - comms service may be cold starting",
        )


async def _request_cleanup_step(
    *,
    name: str,
    method: str,
    path: str,
    data: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    timeout: float = PERMANENT_CLEANUP_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Execute one async cleanup request and capture timeout/error state."""
    comms_url = _comms_url()
    if not comms_url or not ADMIN_KEY:
        return _cleanup_step_result(
            name,
            success=True,
            skipped=True,
            reason="missing_comms_config",
        )

    try:
        client = get_async_client()
        response = await client.request(
            method,
            f"{comms_url}{path}",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            data=data,
            json=json_body,
            timeout=timeout,
        )
        if response.status_code == 404:
            return _cleanup_step_result(
                name,
                success=True,
                skipped=True,
                reason="not_found",
            )
        response.raise_for_status()
        return _cleanup_step_result(
            name,
            success=True,
            response=_safe_json(response),
        )
    except httpx.TimeoutException:
        logging.warning("%s timed out", name)
        return _cleanup_step_result(
            name,
            success=False,
            timed_out=True,
            error="request timed out",
        )
    except Exception as exc:
        logging.error("%s failed: %s", name, exc)
        return _cleanup_step_result(name, success=False, error=str(exc))


def _request_cleanup_step_sync(
    *,
    name: str,
    method: str,
    path: str,
    data: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    timeout: float = PERMANENT_CLEANUP_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Synchronous variant of ``_request_cleanup_step`` for blocking callers."""
    comms_url = _comms_url()
    if not comms_url or not ADMIN_KEY:
        return _cleanup_step_result(
            name,
            success=True,
            skipped=True,
            reason="missing_comms_config",
        )

    try:
        with httpx.Client() as client:
            response = client.request(
                method,
                f"{comms_url}{path}",
                headers={"Authorization": f"Bearer {ADMIN_KEY}"},
                data=data,
                json=json_body,
                timeout=timeout,
            )
            if response.status_code == 404:
                # Resource already gone — idempotent cleanup success.
                return _cleanup_step_result(
                    name,
                    success=True,
                    skipped=True,
                    reason="not_found",
                )
            response.raise_for_status()
            return _cleanup_step_result(
                name,
                success=True,
                response=_safe_json(response),
            )
    except httpx.TimeoutException:
        logging.warning("%s timed out", name)
        return _cleanup_step_result(
            name,
            success=False,
            timed_out=True,
            error="request timed out",
        )
    except Exception as exc:
        logging.error("%s failed: %s", name, exc)
        return _cleanup_step_result(name, success=False, error=str(exc))


async def delete_pubsub_topic(assistant_id: str):
    """
    Delete a pubsub topic for the assistant by making a DELETE request to the comms endpoint.

    Args:
        assistant_id (str): The ID of the assistant

    Returns:
        JSON response from the pubsub topic deletion endpoint
    """
    if settings.is_self_host:
        return _cleanup_step_result(
            name="delete_pubsub_topic",
            success=True,
            skipped=True,
            reason="self_host_local_provisioning",
        )

    topic_name = f"unity-{assistant_id}{env_suffix()}"
    return await _request_cleanup_step(
        name="delete_pubsub_topic",
        method="DELETE",
        path="/infra/pubsub/topic",
        data={"topic_name": topic_name},
    )


async def release_pool_vm(
    assistant_id: str,
    binding_id: str,
    *,
    vm_name: str | None = None,
    job_name: str | None = None,
    release_generation: int | None = None,
):
    """Release a pool VM using the binding-scoped comms contract."""

    if bool(vm_name) == bool(job_name):
        raise ValueError("Provide exactly one of vm_name or job_name for VM release")

    payload: dict[str, Any] = {
        "assistant_id": assistant_id,
        "binding_id": binding_id,
    }
    if vm_name:
        payload["vm_name"] = vm_name
    if job_name:
        payload["job_name"] = job_name
    if release_generation is not None:
        payload["release_generation"] = release_generation
    return await _request_cleanup_step(
        name="release_pool_vm",
        method="POST",
        path="/infra/vm/pool/release",
        json_body=payload,
        timeout=30.0,
    )


def release_pool_vm_sync(
    assistant_id: str,
    binding_id: str,
    *,
    vm_name: str | None = None,
    job_name: str | None = None,
    release_generation: int | None = None,
) -> dict[str, Any]:
    """Synchronous binding-scoped pool release helper."""

    if bool(vm_name) == bool(job_name):
        raise ValueError("Provide exactly one of vm_name or job_name for VM release")

    payload: dict[str, Any] = {
        "assistant_id": assistant_id,
        "binding_id": binding_id,
    }
    if vm_name:
        payload["vm_name"] = vm_name
    if job_name:
        payload["job_name"] = job_name
    if release_generation is not None:
        payload["release_generation"] = release_generation
    return _request_cleanup_step_sync(
        name="release_pool_vm",
        method="POST",
        path="/infra/vm/pool/release",
        json_body=payload,
        timeout=30.0,
    )


async def stop_assistant_session_runtime(
    assistant_id: str,
):
    """Patch the AssistantSession desired state to ``Stopped``."""

    return await _request_cleanup_step(
        name="stop_assistant_session_runtime",
        method="POST",
        path=f"/infra/session/{assistant_id}/stop",
        timeout=20.0,
    )


async def delete_assistant_disk(assistant_id: str):
    """Delete an assistant's persistent disk (permanent unhire cleanup)."""
    return await _request_cleanup_step(
        name="delete_assistant_disk",
        method="DELETE",
        path=f"/infra/vm/pool/disk/{assistant_id}",
    )


async def delete_assistant_pool_archive(
    assistant_id: str,
):
    """Delete an assistant's GCS workspace archive (permanent unhire cleanup).

    Pool VMs persist ``/Unity/Local`` to
    ``gs://bucket/{assistant_id}.tar.gz`` between
    sessions so a fresh PD can be rehydrated on the next assignment.
    On permanent delete this archive is orphan state and must be
    removed in the same teardown flow as the per-assistant PD; otherwise
    the archive bucket accumulates state for assistants that no longer
    exist.
    """
    return await _request_cleanup_step(
        name="delete_assistant_pool_archive",
        method="DELETE",
        path=f"/infra/vm/pool/archive/{assistant_id}",
    )


async def get_social_platforms_costs():
    """
    Fetch available social platforms and their costs.
    """
    client = get_async_client()
    response = await client.get(
        f"{_comms_url()}/social/available-platforms",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        timeout=20,
    )
    return response.json()


RUNTIME_JOB_LOOKBACK_HOURS = 36


async def get_running_jobs(
    assistant_id: str,
) -> List[str]:
    """
    Get running jobs for the assistant by querying K8s via the comms service.

    Args:
        assistant_id: The assistant ID to find running jobs for

    Returns:
        List of job names that are currently running for this assistant
    """
    comms_url = _comms_url()
    if not comms_url or not ADMIN_KEY:
        return []

    try:
        label = str(assistant_id).lower().replace("_", "-")
        client = get_async_client()
        response = await client.get(
            f"{comms_url}/infra/jobs",
            params={
                "label_selector": f"app=unity,assistant-id={label}",
                "hours": RUNTIME_JOB_LOOKBACK_HOURS,
            },
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            timeout=10,
        )
        if response.status_code != 200:
            logging.warning(
                "get_running_jobs: comms returned %d for assistant %s",
                response.status_code,
                assistant_id,
            )
            return []
        data = response.json()
    except Exception:
        logging.exception("get_running_jobs failed for assistant %s", assistant_id)
        return []

    return [
        job["job_name"]
        for job in data.get("jobs", [])
        if job.get("status") == "Running"
    ]


async def get_runtime_status(
    assistant_id: str,
) -> dict[str, Any] | None:
    """Read the Comms runtime aggregate for one assistant."""

    comms_url = _comms_url()
    if not comms_url or not ADMIN_KEY:
        return None

    try:
        client = get_async_client()
        response = await client.get(
            f"{comms_url}/infra/runtime/{assistant_id}",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            timeout=10,
        )
        if response.status_code != 200:
            logging.warning(
                "get_runtime_status: comms returned %d for assistant %s",
                response.status_code,
                assistant_id,
            )
            return None
        data = _safe_json(response)
        return data if isinstance(data, dict) else {}
    except Exception:
        logging.exception("get_runtime_status failed for assistant %s", assistant_id)
        return None


async def stop_jobs(
    assistant_id: str,
):
    """
    Stop any running Unity job for the assistant.

    Returns structured step results so permanent-delete callers can distinguish
    "nothing was running" from "cleanup timed out".
    """
    assistant_id = str(assistant_id)
    steps: dict[str, dict[str, Any]] = {}
    job_names: list[str] = []
    comms_url = _comms_url()

    if not comms_url or not ADMIN_KEY:
        skipped = _cleanup_step_result(
            "discover_jobs",
            success=True,
            skipped=True,
            reason="missing_comms_config",
        )
        steps["discover_jobs"] = skipped
        steps["stop_job"] = _cleanup_step_result(
            "stop_job",
            success=True,
            skipped=True,
            reason="missing_comms_config",
        )
        return {"success": True, "job_names": [], "steps": steps, "errors": []}

    label = assistant_id.lower().replace("_", "-")
    try:
        client = get_async_client()
        response = await client.get(
            f"{comms_url}/infra/jobs",
            params={
                "label_selector": f"app=unity,assistant-id={label}",
                "hours": RUNTIME_JOB_LOOKBACK_HOURS,
            },
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        job_names = [
            job["job_name"]
            for job in data.get("jobs", [])
            if job.get("status") == "Running"
        ]
        steps["discover_jobs"] = _cleanup_step_result(
            "discover_jobs",
            success=True,
            response={"job_names": job_names},
        )

        if job_names:
            stop_results: list[dict[str, Any]] = []
            stop_errors: list[str] = []
            timed_out = False
            for job_name in job_names:
                try:
                    stop_response = await client.post(
                        f"{comms_url}/infra/job/stop",
                        data={"job_name": job_name},
                        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
                        timeout=20,
                    )
                    stop_response.raise_for_status()
                    stop_results.append(
                        {
                            "job_name": job_name,
                            "response": _safe_json(stop_response),
                        },
                    )
                except httpx.TimeoutException:
                    timed_out = True
                    stop_errors.append(f"{job_name}: request timed out")
                except Exception as exc:
                    stop_errors.append(f"{job_name}: {exc}")
            steps["stop_job"] = _cleanup_step_result(
                "stop_job",
                success=not stop_errors,
                response={
                    "job_names": job_names,
                    "results": stop_results,
                },
                error="; ".join(stop_errors) if stop_errors else None,
                timed_out=timed_out,
            )
        else:
            steps["stop_job"] = _cleanup_step_result(
                "stop_job",
                success=True,
                skipped=True,
                reason="no_running_jobs",
            )
    except httpx.TimeoutException:
        steps["discover_jobs"] = _cleanup_step_result(
            "discover_jobs",
            success=False,
            timed_out=True,
            error="request timed out",
        )
        steps["stop_job"] = _cleanup_step_result(
            "stop_job",
            success=True,
            skipped=True,
            reason="job_discovery_incomplete",
        )
    except Exception as exc:
        steps["discover_jobs"] = _cleanup_step_result(
            "discover_jobs",
            success=False,
            error=str(exc),
        )
        steps["stop_job"] = _cleanup_step_result(
            "stop_job",
            success=True,
            skipped=True,
            reason="job_discovery_failed",
        )

    errors = _cleanup_errors_from_steps(steps)
    return {
        "success": not errors,
        "job_names": job_names,
        "steps": steps,
        "errors": errors,
    }


def _requires_assistant_disk_cleanup(desktop_mode: str | None) -> bool:
    return desktop_mode in ("windows", "ubuntu")


async def delete_assistant_session(
    assistant_id: str,
):
    """Delete the AssistantSession CR for an assistant as a tracked step."""
    return await _request_cleanup_step(
        name="delete_assistant_session",
        method="DELETE",
        path=f"/infra/session/{assistant_id}",
        timeout=20,
    )


async def _cleanup_sessionless_runtime(
    assistant_id: str,
) -> dict[str, Any]:
    """Best-effort fallback when runtime exists without a current session."""

    runtime_status_step = await _request_cleanup_step(
        name="runtime_status",
        method="GET",
        path=f"/infra/runtime/{assistant_id}",
        timeout=20,
    )
    runtime_status = _step_response(runtime_status_step)
    if not runtime_status_step.get("success"):
        return _cleanup_step_result(
            "sessionless_runtime_fallback",
            success=True,
            skipped=True,
            reason="runtime_status_unavailable",
            response={
                "runtime_status_step": runtime_status_step,
                "stop_jobs": _cleanup_step_result(
                    "stop_job",
                    success=True,
                    skipped=True,
                    reason="runtime_status_unavailable",
                ),
                "released_vms": [],
                "errors": [_cleanup_step_message(runtime_status_step)],
            },
        )
    if not _runtime_has_live_resources(runtime_status):
        return _cleanup_step_result(
            "sessionless_runtime_fallback",
            success=True,
            skipped=True,
            reason="runtime_already_clean",
            response={
                "runtime_status": runtime_status,
                "stop_jobs": _cleanup_step_result(
                    "stop_job",
                    success=True,
                    skipped=True,
                    reason="runtime_already_clean",
                ),
                "released_vms": [],
                "errors": [],
            },
        )

    stop_jobs_result = await stop_jobs(assistant_id)
    release_steps: list[dict[str, Any]] = []
    fallback_errors = list(stop_jobs_result.get("errors", []))
    for vm_ref in _runtime_vm_refs(runtime_status):
        release_step = await release_pool_vm(
            assistant_id,
            vm_ref["binding_id"],
            vm_name=vm_ref["vm_name"],
        )
        release_steps.append(
            {
                "binding_id": vm_ref["binding_id"],
                "vm_name": vm_ref["vm_name"],
                "step": release_step,
            },
        )
        if not release_step.get("success"):
            fallback_errors.append(
                f"{vm_ref['vm_name']}: {_cleanup_step_message(release_step)}",
            )

    return _cleanup_step_result(
        "sessionless_runtime_fallback",
        success=True,
        response={
            "runtime_status": runtime_status,
            "stop_jobs": stop_jobs_result,
            "released_vms": release_steps,
            "errors": fallback_errors,
        },
    )


def _stop_jobs_sync(
    assistant_id: str,
) -> dict[str, Any]:
    """Synchronous variant of ``stop_jobs`` used by blocking cleanup callers."""

    assistant_id = str(assistant_id)
    steps: dict[str, dict[str, Any]] = {}
    job_names: list[str] = []
    comms_url = _comms_url()

    if not comms_url or not ADMIN_KEY:
        skipped = _cleanup_step_result(
            "discover_jobs",
            success=True,
            skipped=True,
            reason="missing_comms_config",
        )
        steps["discover_jobs"] = skipped
        steps["stop_job"] = _cleanup_step_result(
            "stop_job",
            success=True,
            skipped=True,
            reason="missing_comms_config",
        )
        return {"success": True, "job_names": [], "steps": steps, "errors": []}

    label = assistant_id.lower().replace("_", "-")
    try:
        with httpx.Client() as client:
            response = client.get(
                f"{comms_url}/infra/jobs",
                params={
                    "label_selector": f"app=unity,assistant-id={label}",
                    "hours": RUNTIME_JOB_LOOKBACK_HOURS,
                },
                headers={"Authorization": f"Bearer {ADMIN_KEY}"},
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()
            job_names = [
                job["job_name"]
                for job in data.get("jobs", [])
                if job.get("status") == "Running"
            ]
            steps["discover_jobs"] = _cleanup_step_result(
                "discover_jobs",
                success=True,
                response={"job_names": job_names},
            )

            if job_names:
                stop_results: list[dict[str, Any]] = []
                stop_errors: list[str] = []
                timed_out = False
                for job_name in job_names:
                    try:
                        stop_response = client.post(
                            f"{comms_url}/infra/job/stop",
                            data={"job_name": job_name},
                            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
                            timeout=20,
                        )
                        stop_response.raise_for_status()
                        stop_results.append(
                            {
                                "job_name": job_name,
                                "response": _safe_json(stop_response),
                            },
                        )
                    except httpx.TimeoutException:
                        timed_out = True
                        stop_errors.append(f"{job_name}: request timed out")
                    except Exception as exc:
                        stop_errors.append(f"{job_name}: {exc}")
                steps["stop_job"] = _cleanup_step_result(
                    "stop_job",
                    success=not stop_errors,
                    response={
                        "job_names": job_names,
                        "results": stop_results,
                    },
                    error="; ".join(stop_errors) if stop_errors else None,
                    timed_out=timed_out,
                )
            else:
                steps["stop_job"] = _cleanup_step_result(
                    "stop_job",
                    success=True,
                    skipped=True,
                    reason="no_running_jobs",
                )
    except httpx.TimeoutException:
        steps["discover_jobs"] = _cleanup_step_result(
            "discover_jobs",
            success=False,
            timed_out=True,
            error="request timed out",
        )
        steps["stop_job"] = _cleanup_step_result(
            "stop_job",
            success=True,
            skipped=True,
            reason="job_discovery_incomplete",
        )
    except Exception as exc:
        steps["discover_jobs"] = _cleanup_step_result(
            "discover_jobs",
            success=False,
            error=str(exc),
        )
        steps["stop_job"] = _cleanup_step_result(
            "stop_job",
            success=True,
            skipped=True,
            reason="job_discovery_failed",
        )

    errors = _cleanup_errors_from_steps(steps)
    return {
        "success": not errors,
        "job_names": job_names,
        "steps": steps,
        "errors": errors,
    }


def _cleanup_sessionless_runtime_sync(
    assistant_id: str,
) -> dict[str, Any]:
    """Blocking fallback when runtime exists without a current session."""

    runtime_status_step = _request_cleanup_step_sync(
        name="runtime_status",
        method="GET",
        path=f"/infra/runtime/{assistant_id}",
        timeout=20,
    )
    runtime_status = _step_response(runtime_status_step)
    if not runtime_status_step.get("success"):
        return _cleanup_step_result(
            "sessionless_runtime_fallback",
            success=True,
            skipped=True,
            reason="runtime_status_unavailable",
            response={
                "runtime_status_step": runtime_status_step,
                "stop_jobs": _cleanup_step_result(
                    "stop_job",
                    success=True,
                    skipped=True,
                    reason="runtime_status_unavailable",
                ),
                "released_vms": [],
                "errors": [_cleanup_step_message(runtime_status_step)],
            },
        )
    if not _runtime_has_live_resources(runtime_status):
        return _cleanup_step_result(
            "sessionless_runtime_fallback",
            success=True,
            skipped=True,
            reason="runtime_already_clean",
            response={
                "runtime_status": runtime_status,
                "stop_jobs": _cleanup_step_result(
                    "stop_job",
                    success=True,
                    skipped=True,
                    reason="runtime_already_clean",
                ),
                "released_vms": [],
                "errors": [],
            },
        )

    stop_jobs_result = _stop_jobs_sync(assistant_id)
    release_steps: list[dict[str, Any]] = []
    fallback_errors = list(stop_jobs_result.get("errors", []))
    for vm_ref in _runtime_vm_refs(runtime_status):
        release_step = release_pool_vm_sync(
            assistant_id,
            vm_ref["binding_id"],
            vm_name=vm_ref["vm_name"],
        )
        release_steps.append(
            {
                "binding_id": vm_ref["binding_id"],
                "vm_name": vm_ref["vm_name"],
                "step": release_step,
            },
        )
        if not release_step.get("success"):
            fallback_errors.append(
                f"{vm_ref['vm_name']}: {_cleanup_step_message(release_step)}",
            )

    return _cleanup_step_result(
        "sessionless_runtime_fallback",
        success=True,
        response={
            "runtime_status": runtime_status,
            "stop_jobs": stop_jobs_result,
            "released_vms": release_steps,
            "errors": fallback_errors,
        },
    )


async def wait_for_runtime_cleanup(
    assistant_id: str,
    *,
    timeout: float = RUNTIME_CLEANUP_WAIT_TIMEOUT_SECONDS,
    poll_interval: float = RUNTIME_CLEANUP_POLL_INTERVAL_SECONDS,
) -> dict[str, Any]:
    """Poll Comms until the assistant no longer has live runtime resources."""
    deadline = time.monotonic() + timeout
    last_status: dict[str, Any] = {}

    while True:
        status_step = await _request_cleanup_step(
            name="runtime_status",
            method="GET",
            path=f"/infra/runtime/{assistant_id}",
            timeout=20,
        )
        if not status_step.get("success"):
            return _cleanup_step_result(
                "wait_for_runtime_cleanup",
                success=False,
                error=status_step.get("error") or "runtime status request failed",
            )
        if _is_missing_comms_config_step(status_step):
            return _cleanup_step_result(
                "wait_for_runtime_cleanup",
                success=True,
                skipped=True,
                reason="missing_comms_config",
            )

        last_status = status_step.get("response", {})
        if last_status.get("runtime_cleanup_complete"):
            return _cleanup_step_result(
                "wait_for_runtime_cleanup",
                success=True,
                response=last_status,
            )

        if time.monotonic() >= deadline:
            return _cleanup_step_result(
                "wait_for_runtime_cleanup",
                success=False,
                response=last_status,
                reason="runtime_cleanup_in_progress",
            )

        await asyncio.sleep(poll_interval)


async def teardown_assistant_runtime(
    assistant_id: str | int,
    desktop_mode: str | None = None,
) -> dict:
    """Runtime teardown with explicit step-level incomplete states."""
    assistant_id = str(assistant_id)
    stop_session_step = await stop_assistant_session_runtime(
        assistant_id,
    )
    fallback_step = _cleanup_step_result(
        "sessionless_runtime_fallback",
        success=True,
        skipped=True,
        reason="assistant_session_present",
    )
    if _is_missing_comms_config_step(stop_session_step):
        fallback_step = _cleanup_step_result(
            "sessionless_runtime_fallback",
            success=True,
            skipped=True,
            reason="missing_comms_config",
        )
        wait_step = _cleanup_step_result(
            "wait_for_runtime_cleanup",
            success=True,
            skipped=True,
            reason="missing_comms_config",
        )
        session_step = _cleanup_step_result(
            "delete_assistant_session",
            success=True,
            skipped=True,
            reason="missing_comms_config",
        )
    elif not stop_session_step.get("success"):
        fallback_step = _cleanup_step_result(
            "sessionless_runtime_fallback",
            success=True,
            skipped=True,
            reason="assistant_session_stop_incomplete",
        )
        wait_step = _cleanup_step_result(
            "wait_for_runtime_cleanup",
            success=True,
            skipped=True,
            reason="assistant_session_stop_incomplete",
        )
        session_step = _cleanup_step_result(
            "delete_assistant_session",
            success=True,
            skipped=True,
            reason="assistant_session_stop_incomplete",
        )
    else:
        if _stop_requires_sessionless_fallback(stop_session_step):
            fallback_step = await _cleanup_sessionless_runtime(
                assistant_id,
            )
        wait_step = await wait_for_runtime_cleanup(assistant_id)
        if wait_step.get("success"):
            session_step = await delete_assistant_session(
                assistant_id,
            )
        else:
            session_step = _cleanup_step_result(
                "delete_assistant_session",
                success=True,
                skipped=True,
                reason="runtime_cleanup_incomplete",
            )

    if wait_step.get("success") and session_step.get("success"):
        topic_step = await delete_pubsub_topic(assistant_id)
        if _requires_assistant_disk_cleanup(desktop_mode):
            disk_step = await delete_assistant_disk(assistant_id)
            archive_step = await delete_assistant_pool_archive(
                assistant_id,
            )
        else:
            disk_step = _cleanup_step_result(
                "delete_assistant_disk",
                success=True,
                skipped=True,
                reason="desktop_mode_does_not_require_disk_cleanup",
            )
            archive_step = _cleanup_step_result(
                "delete_assistant_pool_archive",
                success=True,
                skipped=True,
                reason="desktop_mode_does_not_require_disk_cleanup",
            )
    else:
        topic_step = _cleanup_step_result(
            "delete_pubsub_topic",
            success=True,
            skipped=True,
            reason="runtime_cleanup_incomplete",
        )
        disk_step = _cleanup_step_result(
            "delete_assistant_disk",
            success=True,
            skipped=True,
            reason="runtime_cleanup_incomplete",
        )
        archive_step = _cleanup_step_result(
            "delete_assistant_pool_archive",
            success=True,
            skipped=True,
            reason="runtime_cleanup_incomplete",
        )

    steps = {
        "stop_assistant_session_runtime": stop_session_step,
        "sessionless_runtime_fallback": fallback_step,
        "delete_assistant_session": session_step,
        "wait_for_runtime_cleanup": wait_step,
        "delete_pubsub_topic": topic_step,
        "delete_assistant_disk": disk_step,
        "delete_assistant_pool_archive": archive_step,
    }
    errors = _cleanup_errors_from_steps(steps)
    return {
        "success": not errors,
        "assistant_id": assistant_id,
        "steps": steps,
        "errors": errors,
    }


def _wait_for_runtime_cleanup_sync(
    assistant_id: str,
    *,
    timeout: float = RUNTIME_CLEANUP_WAIT_TIMEOUT_SECONDS,
    poll_interval: float = RUNTIME_CLEANUP_POLL_INTERVAL_SECONDS,
) -> dict[str, Any]:
    """Synchronous variant of ``wait_for_runtime_cleanup``."""
    deadline = time.monotonic() + timeout
    last_status: dict[str, Any] = {}

    while True:
        status_step = _request_cleanup_step_sync(
            name="runtime_status",
            method="GET",
            path=f"/infra/runtime/{assistant_id}",
            timeout=20,
        )
        if not status_step.get("success"):
            return _cleanup_step_result(
                "wait_for_runtime_cleanup",
                success=False,
                error=status_step.get("error") or "runtime status request failed",
            )
        if _is_missing_comms_config_step(status_step):
            return _cleanup_step_result(
                "wait_for_runtime_cleanup",
                success=True,
                skipped=True,
                reason="missing_comms_config",
            )

        last_status = status_step.get("response", {})
        if last_status.get("runtime_cleanup_complete"):
            return _cleanup_step_result(
                "wait_for_runtime_cleanup",
                success=True,
                response=last_status,
            )

        if time.monotonic() >= deadline:
            return _cleanup_step_result(
                "wait_for_runtime_cleanup",
                success=False,
                response=last_status,
                reason="runtime_cleanup_in_progress",
            )

        time.sleep(poll_interval)


def teardown_assistant_runtime_sync(
    assistant_id: str | int,
    desktop_mode: str | None = None,
) -> dict:
    """Blocking version of runtime teardown with explicit step states."""
    assistant_id = str(assistant_id)
    comms_url = _comms_url()
    if not comms_url or not ADMIN_KEY:
        return {
            "success": True,
            "assistant_id": assistant_id,
            "skipped": True,
            "reason": "missing_comms_config",
            "errors": [],
            "steps": {},
        }

    steps: dict[str, dict[str, Any]] = {}
    steps["stop_assistant_session_runtime"] = _request_cleanup_step_sync(
        name="stop_assistant_session_runtime",
        method="POST",
        path=f"/infra/session/{assistant_id}/stop",
        timeout=20,
    )
    steps["sessionless_runtime_fallback"] = _cleanup_step_result(
        "sessionless_runtime_fallback",
        success=True,
        skipped=True,
        reason="assistant_session_present",
    )
    if not steps["stop_assistant_session_runtime"].get("success"):
        steps["sessionless_runtime_fallback"] = _cleanup_step_result(
            "sessionless_runtime_fallback",
            success=True,
            skipped=True,
            reason="assistant_session_stop_incomplete",
        )
        steps["wait_for_runtime_cleanup"] = _cleanup_step_result(
            "wait_for_runtime_cleanup",
            success=True,
            skipped=True,
            reason="assistant_session_stop_incomplete",
        )
        steps["delete_assistant_session"] = _cleanup_step_result(
            "delete_assistant_session",
            success=True,
            skipped=True,
            reason="assistant_session_stop_incomplete",
        )
    else:
        if _stop_requires_sessionless_fallback(
            steps["stop_assistant_session_runtime"],
        ):
            steps["sessionless_runtime_fallback"] = _cleanup_sessionless_runtime_sync(
                assistant_id,
            )
        steps["wait_for_runtime_cleanup"] = _wait_for_runtime_cleanup_sync(
            assistant_id,
        )
        if steps["wait_for_runtime_cleanup"].get("success"):
            steps["delete_assistant_session"] = _request_cleanup_step_sync(
                name="delete_assistant_session",
                method="DELETE",
                path=f"/infra/session/{assistant_id}",
                timeout=20,
            )
        else:
            steps["delete_assistant_session"] = _cleanup_step_result(
                "delete_assistant_session",
                success=True,
                skipped=True,
                reason="runtime_cleanup_incomplete",
            )

    if steps["wait_for_runtime_cleanup"].get("success") and steps[
        "delete_assistant_session"
    ].get("success"):
        steps["delete_pubsub_topic"] = _request_cleanup_step_sync(
            name="delete_pubsub_topic",
            method="DELETE",
            path="/infra/pubsub/topic",
            data={"topic_name": f"unity-{assistant_id}{env_suffix()}"},
        )
        if _requires_assistant_disk_cleanup(desktop_mode):
            steps["delete_assistant_disk"] = _request_cleanup_step_sync(
                name="delete_assistant_disk",
                method="DELETE",
                path=f"/infra/vm/pool/disk/{assistant_id}",
            )
            steps["delete_assistant_pool_archive"] = _request_cleanup_step_sync(
                name="delete_assistant_pool_archive",
                method="DELETE",
                path=f"/infra/vm/pool/archive/{assistant_id}",
            )
        else:
            steps["delete_assistant_disk"] = _cleanup_step_result(
                "delete_assistant_disk",
                success=True,
                skipped=True,
                reason="desktop_mode_does_not_require_disk_cleanup",
            )
            steps["delete_assistant_pool_archive"] = _cleanup_step_result(
                "delete_assistant_pool_archive",
                success=True,
                skipped=True,
                reason="desktop_mode_does_not_require_disk_cleanup",
            )
    else:
        steps["delete_pubsub_topic"] = _cleanup_step_result(
            "delete_pubsub_topic",
            success=True,
            skipped=True,
            reason="runtime_cleanup_incomplete",
        )
        steps["delete_assistant_disk"] = _cleanup_step_result(
            "delete_assistant_disk",
            success=True,
            skipped=True,
            reason="runtime_cleanup_incomplete",
        )
        steps["delete_assistant_pool_archive"] = _cleanup_step_result(
            "delete_assistant_pool_archive",
            success=True,
            skipped=True,
            reason="runtime_cleanup_incomplete",
        )

    errors = _cleanup_errors_from_steps(steps)
    return {
        "success": not errors,
        "assistant_id": assistant_id,
        "steps": steps,
        "errors": errors,
    }


async def wake_up_assistant(assistant_id: str):
    """Post the wakeup webhook and return the adapter-edge response.

    A ``200`` from adapters only means the wakeup request was accepted there.
    AssistantSession creation and runtime convergence continue asynchronously in
    communication after this call returns.
    """
    wake_up_url = _adapters_url() + "/assistant/wakeup"
    client = get_async_client()
    return await client.post(
        wake_up_url,
        data={"assistant_id": assistant_id},
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        timeout=20,
    )


async def reawaken_assistant(
    assistant_id: str,
    *,
    data: dict | None = None,
):
    """
    Trigger the assistant update webhook to reawaken or sync the assistant.

    This only waits for adapters to accept the update request. Runtime
    convergence still happens asynchronously downstream in communication.

    Args:
        assistant_id (str): The ID of the assistant to reawaken.
        data: Optional form payload for specialized update requests.
    Returns:
        The JSON response from the webhook.
    """
    reawaken_url = _adapters_url() + "/assistant/update"
    client = get_async_client()
    payload = data or {"assistant_id": assistant_id}
    response = await client.post(
        reawaken_url,
        data=payload,
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


async def delegate_to_colleague_runtime(
    *,
    assistant_id: int | str,
    requested_by_assistant_id: int | str,
    instruction: str,
    intent: str = "general",
    dedupe_key: str | None = None,
    related_context: dict | None = None,
) -> dict:
    """Ask Adapters to deliver a Coordinator delegation wake reason."""

    url = f"{_adapters_url()}/assistant/coordinator-delegate"
    payload: dict[str, Any] = {
        "assistant_id": str(assistant_id),
        "requested_by_assistant_id": str(requested_by_assistant_id),
        "instruction": instruction,
        "intent": intent,
    }
    if dedupe_key is not None:
        payload["dedupe_key"] = dedupe_key
    if related_context is not None:
        payload["related_context"] = related_context

    client = get_async_client()
    response = await client.post(
        url,
        headers={
            "Authorization": f"Bearer {ADMIN_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=40,
    )
    response.raise_for_status()
    return response.json()


async def log_pre_hire_chat(
    assistant_id: str,
    messages: list,
):
    """
    Logs pre-hire chat messages for an assistant using the webhook.
    Args:
        assistant_id (str): The ID of the assistant.
        messages (list): A list of chat message dictionaries.
    Returns:
        The JSON response from the webhook.
    """
    log_pre_hire_chat_url = _adapters_url() + "/unity/pre-hire"
    payload = {"assistant_id": assistant_id, "body": messages}
    client = get_async_client()
    response = await client.post(
        log_pre_hire_chat_url,
        headers={
            "Authorization": f"Bearer {ADMIN_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=20,
    )
    response.raise_for_status()
    return {"status": "success"}


async def _trigger_contact_sync(
    assistant_id: int,
) -> dict:
    """Hit the Adapters ``sync_contacts`` system-event webhook.

    Internal helper for the public :func:`trigger_contact_sync_safe` and
    :func:`fan_out_contact_sync_for_org` entry points: callers that hold
    user-facing responses must always go through the safe wrappers so a
    transient Adapters failure does not surface as a 500.
    """
    url = f"{_adapters_url()}/unity/system-event"
    client = get_async_client()
    response = await client.post(
        url,
        headers={
            "Authorization": f"Bearer {ADMIN_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "assistant_id": assistant_id,
            "event_type": "sync_contacts",
            "message": "Contacts sync triggered.",
        },
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


async def _post_unity_system_event(
    *,
    assistant_id: int | str,
    event_type: str,
    message: str,
    extra_event_fields: dict | None = None,
) -> None:
    """Post a generic ``unity_system_event`` to the Adapters webhook.

    Thin generalisation of :func:`_trigger_contact_sync` so other
    services (e.g. coordinator onboarding narration) can wake the
    target assistant's Unity session with their own ``event_type`` +
    structured payload. ``extra_event_fields`` lands on the Pub/Sub
    event under the same top-level dict the adapter publishes (see
    ``_publish_unity_system_event`` in
    ``communication/adapters/main.py``) so Unity-side handlers can
    pluck out subtype / details without re-parsing the message body.

    Internal helper. User-facing endpoints should wrap callers in a
    try/except (or use a ``_safe`` wrapper) so a transient Adapters
    outage cannot break the surrounding request.
    """
    url = f"{_adapters_url()}/unity/system-event"
    client = get_async_client()
    payload: dict[str, Any] = {
        "assistant_id": assistant_id,
        "event_type": event_type,
        "message": message,
    }
    if extra_event_fields:
        payload["extra_event_fields"] = extra_event_fields
    response = await client.post(
        url,
        headers={
            "Authorization": f"Bearer {ADMIN_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=20,
    )
    response.raise_for_status()


async def trigger_contact_sync_safe(
    assistant_id: int,
) -> None:
    """Kick a single-assistant Contacts re-derivation, swallowing failures.

    Membership-mutating endpoints (invite accept, member add/remove,
    assistant transfer) call this so a temporary Adapters outage cannot
    fail the user-facing request. Unity's next session bootstrap
    re-derives Contacts on its own, so a missed kick is a soft regression
    at worst.
    """
    try:
        await _trigger_contact_sync(assistant_id)
        logging.info("Triggered contact sync for assistant %s", assistant_id)
    except Exception as exc:
        logging.warning(
            "Failed to trigger contact sync for assistant %s: %s",
            assistant_id,
            exc,
        )


async def fan_out_contact_sync_for_org(
    organization_id: int,
    session: Session,
) -> None:
    """Refresh Contacts for every assistant in an organization.

    Org membership changes (invite acceptance, direct add, removal)
    reshape the contact set every org assistant should see. This helper
    looks up every assistant in ``organization_id`` and asks the Adapters
    runtime to re-derive each one's Contacts table via the
    ``sync_contacts`` system event, in parallel. Per-assistant failures
    are logged by :func:`trigger_contact_sync_safe` but do not interrupt
    the rest — Unity's next session bootstrap will reconcile.
    """
    from orchestra.db.dao.assistant_dao import AssistantDAO

    org_assistants = AssistantDAO(session).list_all_org_assistants(
        organization_id=organization_id,
    )
    if not org_assistants:
        return
    await asyncio.gather(
        *(trigger_contact_sync_safe(a.agent_id) for a in org_assistants),
    )
