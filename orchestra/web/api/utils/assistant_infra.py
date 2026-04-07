import asyncio
import logging
import os
import time
from typing import Any, List

import httpx

from orchestra.web.api.utils.http_client import get_async_client

COMMS_URL = os.environ.get("UNITY_COMMS_URL")
COMMS_URL_PREVIEW = os.environ.get("UNITY_COMMS_URL_PREVIEW")
ADAPTERS_URL = os.environ.get("UNITY_ADAPTERS_URL")
ADAPTERS_URL_PREVIEW = os.environ.get("UNITY_ADAPTERS_URL_PREVIEW")
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
            message = step.get("error") or step.get("reason") or "cleanup incomplete"
            errors.append(f"{step_name}: {message}")
    return errors


def _comms_url_for(deploy_env: str | None) -> str:
    if deploy_env == "preview":
        return COMMS_URL_PREVIEW or ""
    return COMMS_URL or ""


def _adapters_url_for(deploy_env: str | None) -> str:
    if deploy_env == "preview":
        return ADAPTERS_URL_PREVIEW or ""
    return ADAPTERS_URL or ""


def _env_suffix(deploy_env: str | None) -> str:
    if deploy_env == "preview":
        return "-preview"
    is_staging = os.environ.get("STAGING", "False") == "True"
    return "-staging" if is_staging else ""


async def create_phone_number(
    phone_country: str = "US",
    deploy_env: str | None = None,
):
    """
    Create a phone number for the user by making a POST request to the comms endpoint.

    Args:
        phone_country (str): The country code for phone number provisioning (e.g., "US", "GB").
        deploy_env: 'preview' for preview stack, None for native environment.

    Returns:
        JSON response from the phone creation endpoint
    """
    comms_url = _comms_url_for(deploy_env)
    adapters_url = _adapters_url_for(deploy_env)
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
    deploy_env: str | None = None,
) -> dict:
    """Register a WhatsApp sender with Twilio via the Communication service.

    This calls the existing ``POST /whatsapp/create`` on the Communication
    service to set up the Twilio Messaging Channel Sender and webhook.
    """
    comms_url = _comms_url_for(deploy_env)
    callback_url = _adapters_url_for(deploy_env) + "/twilio/whatsapp"
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
    deploy_env: str | None = None,
) -> dict:
    """Send template-based WhatsApp notifications for a pool number change.

    Each recipient dict must contain: ``to``, ``user_name``, ``agent_name``.
    Returns per-recipient message SIDs for delivery tracking.
    """
    comms_url = _comms_url_for(deploy_env)
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


async def delete_phone_number(phone_number: str, deploy_env: str | None = None):
    """
    Delete a phone number by making a DELETE request to the comms endpoint.

    Args:
        phone_number (str): The phone number to delete

    Returns:
        JSON response from the phone deletion endpoint
    """
    comms_url = _comms_url_for(deploy_env)
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


async def create_email(
    local: str,
    first_name: str,
    last_name: str,
    deploy_env: str | None = None,
):
    """
    Create an email for the user by making a POST request to the UNIFY_COMMS_URL endpoint.

    Args:
        local (str): The local part of the email address
        first_name (str): User's first name
        last_name (str): User's last name

    Returns:
        Response from the email creation endpoint
    """
    comms_url = _comms_url_for(deploy_env)
    client = get_async_client()
    response = await client.post(
        f"{comms_url}/gmail/create",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        json={
            "local": local,
            "first_name": first_name,
            "last_name": last_name,
        },
        timeout=20,
    )
    return response.json()


async def delete_email(email: str, deploy_env: str | None = None):
    """
    Delete an email by making a DELETE request to the comms endpoint.

    Args:
        email (str): The email address to delete

    Returns:
        JSON response from the email deletion endpoint
    """
    comms_url = _comms_url_for(deploy_env)
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


async def watch_email(email: str, deploy_env: str | None = None):
    """
    Watch an email by making a POST request to the comms endpoint.

    Args:
        email (str): The email to watch
        deploy_env: 'preview' for preview stack, None for native environment.

    Returns:
        JSON response from the email watch endpoint
    """
    comms_url = _comms_url_for(deploy_env)
    client = get_async_client()
    response = await client.post(
        f"{comms_url}/gmail/watch",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        json={
            "primary_email": email,
            "topic": f"gmail-notifications{_env_suffix(deploy_env)}",
        },
        timeout=20,
    )
    return response.json()


async def create_pubsub_topic(assistant_id: str, deploy_env: str | None = None):
    """
    Create a pubsub topic for the assistant by making a POST request to the comms endpoint.

    Args:
        assistant_id (str): The ID of the assistant
        deploy_env: 'preview' for preview stack, None for native environment.

    Returns:
        JSON response from the pubsub topic creation endpoint
    """
    comms_url = _comms_url_for(deploy_env)
    topic_name = f"unity-{assistant_id}{_env_suffix(deploy_env)}"
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
    deploy_env: str | None,
    method: str,
    path: str,
    data: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    timeout: float = PERMANENT_CLEANUP_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Execute one async cleanup request and capture timeout/error state."""
    comms_url = _comms_url_for(deploy_env)
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
    deploy_env: str | None,
    method: str,
    path: str,
    data: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    timeout: float = PERMANENT_CLEANUP_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Synchronous variant of ``_request_cleanup_step`` for blocking callers."""
    comms_url = _comms_url_for(deploy_env)
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


async def delete_pubsub_topic(assistant_id: str, deploy_env: str | None = None):
    """
    Delete a pubsub topic for the assistant by making a DELETE request to the comms endpoint.

    Args:
        assistant_id (str): The ID of the assistant
        deploy_env: 'preview' for preview stack, None for native environment.

    Returns:
        JSON response from the pubsub topic deletion endpoint
    """
    topic_name = f"unity-{assistant_id}{_env_suffix(deploy_env)}"
    return await _request_cleanup_step(
        name="delete_pubsub_topic",
        deploy_env=deploy_env,
        method="DELETE",
        path="/infra/pubsub/topic",
        data={"topic_name": topic_name},
    )


async def release_pool_vm(assistant_id: str, deploy_env: str | None = None):
    """Release any pool VM assigned to this assistant back to idle.
    Idempotent — no-ops if no VM is assigned.
    """
    return await _request_cleanup_step(
        name="release_pool_vm",
        deploy_env=deploy_env,
        method="POST",
        path="/infra/vm/pool/release",
        json_body={"assistant_id": assistant_id},
        timeout=30.0,
    )


async def stop_assistant_session_runtime(
    assistant_id: str,
    deploy_env: str | None = None,
):
    """Patch the AssistantSession desired state to ``Stopped``."""

    return await _request_cleanup_step(
        name="stop_assistant_session_runtime",
        deploy_env=deploy_env,
        method="POST",
        path=f"/infra/session/{assistant_id}/stop",
        timeout=20.0,
    )


async def delete_assistant_disk(assistant_id: str, deploy_env: str | None = None):
    """Delete an assistant's persistent disk (permanent unhire cleanup)."""
    return await _request_cleanup_step(
        name="delete_assistant_disk",
        deploy_env=deploy_env,
        method="DELETE",
        path=f"/infra/vm/pool/disk/{assistant_id}",
    )


async def get_social_platforms_costs():
    """
    Fetch available social platforms and their costs.
    """
    client = get_async_client()
    response = await client.get(
        f"{COMMS_URL}/social/available-platforms",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        timeout=20,
    )
    return response.json()


RUNTIME_JOB_LOOKBACK_HOURS = 36


async def get_running_jobs(
    assistant_id: str,
    deploy_env: str | None = None,
) -> List[str]:
    """
    Get running jobs for the assistant by querying K8s via the comms service.

    Args:
        assistant_id: The assistant ID to find running jobs for
        deploy_env: 'preview' for preview stack, None for native environment.

    Returns:
        List of job names that are currently running for this assistant
    """
    comms_url = _comms_url_for(deploy_env)
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
    deploy_env: str | None = None,
) -> dict[str, Any] | None:
    """Read the Comms runtime aggregate for one assistant."""

    comms_url = _comms_url_for(deploy_env)
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
    deploy_env: str | None = None,
):
    """
    Stop any running Unity job for the assistant.

    Returns structured step results so permanent-delete callers can distinguish
    "nothing was running" from "cleanup timed out".
    """
    assistant_id = str(assistant_id)
    steps: dict[str, dict[str, Any]] = {}
    job_names: list[str] = []
    comms_url = _comms_url_for(deploy_env)

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
            try:
                stop_response = await client.post(
                    f"{comms_url}/infra/job/stop",
                    data={"job_name": job_names[0]},
                    headers={"Authorization": f"Bearer {ADMIN_KEY}"},
                    timeout=20,
                )
                stop_response.raise_for_status()
                steps["stop_job"] = _cleanup_step_result(
                    "stop_job",
                    success=True,
                    response=_safe_json(stop_response),
                )
            except httpx.TimeoutException:
                steps["stop_job"] = _cleanup_step_result(
                    "stop_job",
                    success=False,
                    timed_out=True,
                    error="request timed out",
                )
            except Exception as exc:
                steps["stop_job"] = _cleanup_step_result(
                    "stop_job",
                    success=False,
                    error=str(exc),
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
    deploy_env: str | None = None,
):
    """Delete the AssistantSession CR for an assistant as a tracked step."""
    return await _request_cleanup_step(
        name="delete_assistant_session",
        deploy_env=deploy_env,
        method="DELETE",
        path=f"/infra/session/{assistant_id}",
        timeout=20,
    )


async def wait_for_runtime_cleanup(
    assistant_id: str,
    deploy_env: str | None = None,
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
            deploy_env=deploy_env,
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
    deploy_env: str | None = None,
    desktop_mode: str | None = None,
) -> dict:
    """Runtime teardown with explicit step-level incomplete states."""
    assistant_id = str(assistant_id)
    stop_session_step = await stop_assistant_session_runtime(
        assistant_id,
        deploy_env=deploy_env,
    )
    if not stop_session_step.get("success"):
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
        wait_step = await wait_for_runtime_cleanup(assistant_id, deploy_env=deploy_env)
        if wait_step.get("success"):
            session_step = await delete_assistant_session(
                assistant_id,
                deploy_env=deploy_env,
            )
        else:
            session_step = _cleanup_step_result(
                "delete_assistant_session",
                success=True,
                skipped=True,
                reason="runtime_cleanup_incomplete",
            )

    if wait_step.get("success") and session_step.get("success"):
        topic_step = await delete_pubsub_topic(assistant_id, deploy_env=deploy_env)
        if _requires_assistant_disk_cleanup(desktop_mode):
            disk_step = await delete_assistant_disk(assistant_id, deploy_env=deploy_env)
        else:
            disk_step = _cleanup_step_result(
                "delete_assistant_disk",
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

    steps = {
        "stop_assistant_session_runtime": stop_session_step,
        "delete_assistant_session": session_step,
        "wait_for_runtime_cleanup": wait_step,
        "delete_pubsub_topic": topic_step,
        "delete_assistant_disk": disk_step,
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
    deploy_env: str | None = None,
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
            deploy_env=deploy_env,
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
    deploy_env: str | None = None,
    desktop_mode: str | None = None,
) -> dict:
    """Blocking version of runtime teardown with explicit step states."""
    assistant_id = str(assistant_id)
    comms_url = _comms_url_for(deploy_env)
    if not comms_url or not ADMIN_KEY:
        return {
            "success": True,
            "assistant_id": assistant_id,
            "skipped": True,
            "reason": "missing_comms_config",
            "errors": [],
            "steps": {},
        }

    headers = {"Authorization": f"Bearer {ADMIN_KEY}"}
    steps: dict[str, dict[str, Any]] = {}
    steps["stop_assistant_session_runtime"] = _request_cleanup_step_sync(
        name="stop_assistant_session_runtime",
        deploy_env=deploy_env,
        method="POST",
        path=f"/infra/session/{assistant_id}/stop",
        timeout=20,
    )
    if not steps["stop_assistant_session_runtime"].get("success"):
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
        steps["wait_for_runtime_cleanup"] = _wait_for_runtime_cleanup_sync(
            assistant_id,
            deploy_env=deploy_env,
        )
        if steps["wait_for_runtime_cleanup"].get("success"):
            steps["delete_assistant_session"] = _request_cleanup_step_sync(
                name="delete_assistant_session",
                deploy_env=deploy_env,
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
            deploy_env=deploy_env,
            method="DELETE",
            path="/infra/pubsub/topic",
            data={"topic_name": f"unity-{assistant_id}{_env_suffix(deploy_env)}"},
        )
        if _requires_assistant_disk_cleanup(desktop_mode):
            steps["delete_assistant_disk"] = _request_cleanup_step_sync(
                name="delete_assistant_disk",
                deploy_env=deploy_env,
                method="DELETE",
                path=f"/infra/vm/pool/disk/{assistant_id}",
            )
        else:
            steps["delete_assistant_disk"] = _cleanup_step_result(
                "delete_assistant_disk",
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

    errors = _cleanup_errors_from_steps(steps)
    return {
        "success": not errors,
        "assistant_id": assistant_id,
        "steps": steps,
        "errors": errors,
    }


async def wake_up_assistant(assistant_id: str, deploy_env: str | None = None):
    """Post the wakeup webhook and return the adapter-edge response.

    A ``200`` from adapters only means the wakeup request was accepted there.
    AssistantSession creation and runtime convergence continue asynchronously in
    communication after this call returns.
    """
    wake_up_url = _adapters_url_for(deploy_env) + "/assistant/wakeup"
    client = get_async_client()
    return await client.post(
        wake_up_url,
        data={"assistant_id": assistant_id},
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        timeout=20,
    )


async def reawaken_assistant(assistant_id: str, deploy_env: str | None = None):
    """
    Trigger the assistant update webhook to reawaken or sync the assistant.

    This only waits for adapters to accept the update request. Runtime
    convergence still happens asynchronously downstream in communication.

    Args:
        assistant_id (str): The ID of the assistant to reawaken.
        deploy_env: 'preview' for preview stack, None for native environment.
    Returns:
        The JSON response from the webhook.
    """
    reawaken_url = _adapters_url_for(deploy_env) + "/assistant/update"
    client = get_async_client()
    response = await client.post(
        reawaken_url,
        data={"assistant_id": assistant_id},
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


async def log_pre_hire_chat(
    assistant_id: str,
    messages: list,
    deploy_env: str | None = None,
):
    """
    Logs pre-hire chat messages for an assistant using the webhook.
    Args:
        assistant_id (str): The ID of the assistant.
        messages (list): A list of chat message dictionaries.
        deploy_env: 'preview' for preview stack, None for native environment.
    Returns:
        The JSON response from the webhook.
    """
    log_pre_hire_chat_url = _adapters_url_for(deploy_env) + "/unity/pre-hire"
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


async def trigger_contact_sync(
    assistant_id: int,
    deploy_env: str | None = None,
) -> dict:
    """
    Trigger contact sync for an assistant via the system-event webhook.

    Args:
        assistant_id: The assistant ID to sync contacts for

    Returns:
        JSON response from the webhook
    """
    url = f"{_adapters_url_for(deploy_env)}/unity/system-event"
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
