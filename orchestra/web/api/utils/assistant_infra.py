import logging
import os
from typing import List

import httpx

COMMS_URL = os.environ.get("UNITY_COMMS_URL")
COMMS_URL_PREVIEW = os.environ.get("UNITY_COMMS_URL_PREVIEW")
ADAPTERS_URL = os.environ.get("UNITY_ADAPTERS_URL")
ADAPTERS_URL_PREVIEW = os.environ.get("UNITY_ADAPTERS_URL_PREVIEW")
ADMIN_KEY = os.environ.get("ORCHESTRA_ADMIN_KEY")


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
    async with httpx.AsyncClient(timeout=90.0) as client:
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
    async with httpx.AsyncClient() as client:
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
    async with httpx.AsyncClient() as client:
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
    async with httpx.AsyncClient() as client:
        response = await client.request(
            "DELETE",
            f"{comms_url}/phone/delete",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"PhoneNumber": phone_number},
            timeout=20,
        )
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
    async with httpx.AsyncClient() as client:
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
    async with httpx.AsyncClient() as client:
        response = await client.request(
            "DELETE",
            f"{comms_url}/gmail/delete",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"primary_email": email},
            timeout=20,
        )
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
    print(f"Watching email: {email}")
    async with httpx.AsyncClient() as client:
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
    async with httpx.AsyncClient() as client:
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


async def delete_pubsub_topic(assistant_id: str, deploy_env: str | None = None):
    """
    Delete a pubsub topic for the assistant by making a DELETE request to the comms endpoint.

    Args:
        assistant_id (str): The ID of the assistant
        deploy_env: 'preview' for preview stack, None for native environment.

    Returns:
        JSON response from the pubsub topic deletion endpoint
    """
    comms_url = _comms_url_for(deploy_env)
    topic_name = f"unity-{assistant_id}{_env_suffix(deploy_env)}"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.request(
                "DELETE",
                f"{comms_url}/infra/pubsub/topic",
                headers={"Authorization": f"Bearer {ADMIN_KEY}"},
                data={"topic_name": topic_name},
                timeout=0.1,
            )
            return response.json()
        except httpx.TimeoutException:
            logging.warning(
                f"delete_pubsub_topic timed out for assistant {assistant_id}",
            )
            return {"success": True, "timed_out": True}


async def release_pool_vm(assistant_id: str, deploy_env: str | None = None):
    """Release any pool VM assigned to this assistant back to idle.
    Idempotent — no-ops if no VM is assigned.
    """
    comms_url = _comms_url_for(deploy_env)
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{comms_url}/infra/vm/pool/release",
                headers={"Authorization": f"Bearer {ADMIN_KEY}"},
                json={"assistant_id": assistant_id},
                timeout=0.1,
            )
            return response.json()
    except httpx.TimeoutException:
        logging.warning(f"release_pool_vm timed out for assistant {assistant_id}")
        return {"success": True, "timed_out": True}


async def delete_assistant_disk(assistant_id: str, deploy_env: str | None = None):
    """Delete an assistant's persistent disk (permanent unhire cleanup)."""
    comms_url = _comms_url_for(deploy_env)
    try:
        async with httpx.AsyncClient() as client:
            response = await client.request(
                "DELETE",
                f"{comms_url}/infra/vm/pool/disk/{assistant_id}",
                headers={"Authorization": f"Bearer {ADMIN_KEY}"},
                timeout=0.1,
            )
            return response.json()
    except httpx.TimeoutException:
        logging.warning(f"delete_assistant_disk timed out for assistant {assistant_id}")
        return {"success": True, "timed_out": True}


async def get_social_platforms_costs():
    """
    Fetch available social platforms and their costs.
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{COMMS_URL}/social/available-platforms",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            timeout=20,
        )
        return response.json()


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
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{comms_url}/infra/jobs",
                params={"label_selector": f"app=unity,assistant-id={label}"},
                headers={"Authorization": f"Bearer {ADMIN_KEY}"},
                timeout=10,
            )
            if response.status_code != 200:
                return []
            data = response.json()
    except Exception:
        return []

    return [
        job["job_name"]
        for job in data.get("jobs", [])
        if job.get("status") == "Running"
    ]


async def stop_jobs(
    assistant_id: str,
    deploy_env: str | None = None,
):
    """
    Stop a job and release any assigned pool VM.

    Args:
        assistant_id: The assistant ID to stop jobs for
        deploy_env: 'preview' for preview stack, None for native environment.
    """
    comms_url = _comms_url_for(deploy_env)
    job_names = await get_running_jobs(assistant_id, deploy_env)
    if len(job_names) > 0:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{comms_url}/infra/job/stop",
                data={"job_name": job_names[0]},
                headers={"Authorization": f"Bearer {ADMIN_KEY}"},
                timeout=20,
            )
            response.raise_for_status()

    await release_pool_vm(str(assistant_id), deploy_env=deploy_env)

    return {"success": True, "job_names": job_names}


def _requires_assistant_disk_cleanup(desktop_mode: str | None) -> bool:
    return desktop_mode in ("windows", "ubuntu")


async def delete_assistant_session(
    assistant_id: str,
    deploy_env: str | None = None,
):
    """Delete the AssistantSession CR for an assistant."""
    comms_url = _comms_url_for(deploy_env)
    if not comms_url or not ADMIN_KEY:
        return {"success": True, "skipped": True, "reason": "missing_comms_config"}

    async with httpx.AsyncClient() as client:
        response = await client.request(
            "DELETE",
            f"{comms_url}/infra/session/{assistant_id}",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            timeout=20,
        )
        response.raise_for_status()
        return response.json()


async def teardown_assistant_runtime(
    assistant_id: str | int,
    deploy_env: str | None = None,
    desktop_mode: str | None = None,
) -> dict:
    """Best-effort runtime teardown for permanent assistant deletion flows."""
    assistant_id = str(assistant_id)
    cleanup_errors: list[str] = []

    try:
        await stop_jobs(assistant_id, deploy_env=deploy_env)
    except Exception as e:
        logging.error(f"Failed to stop jobs for assistant {assistant_id}: {e}")
        cleanup_errors.append(f"Failed to stop job: {e}")

    try:
        await delete_assistant_session(assistant_id, deploy_env=deploy_env)
    except Exception as e:
        logging.error(f"Failed to delete AssistantSession for {assistant_id}: {e}")
        cleanup_errors.append(f"Failed to delete AssistantSession: {e}")

    try:
        await delete_pubsub_topic(assistant_id, deploy_env=deploy_env)
    except Exception as e:
        logging.error(
            f"Failed to delete pubsub topic for assistant {assistant_id}: {e}",
        )
        cleanup_errors.append(f"Failed to delete pubsub topic: {e}")

    if _requires_assistant_disk_cleanup(desktop_mode):
        try:
            await delete_assistant_disk(assistant_id, deploy_env=deploy_env)
        except Exception as e:
            logging.error(f"Failed to delete assistant disk for {assistant_id}: {e}")
            cleanup_errors.append(f"Failed to delete assistant disk: {e}")

    return {
        "success": not cleanup_errors,
        "assistant_id": assistant_id,
        "errors": cleanup_errors,
    }


def teardown_assistant_runtime_sync(
    assistant_id: str | int,
    deploy_env: str | None = None,
    desktop_mode: str | None = None,
) -> dict:
    """Blocking version of runtime teardown for post-commit service cleanup."""
    assistant_id = str(assistant_id)
    cleanup_errors: list[str] = []
    comms_url = _comms_url_for(deploy_env)
    if not comms_url or not ADMIN_KEY:
        return {
            "success": True,
            "assistant_id": assistant_id,
            "errors": cleanup_errors,
            "skipped": True,
            "reason": "missing_comms_config",
        }

    headers = {"Authorization": f"Bearer {ADMIN_KEY}"}

    def _record_error(message: str, error: Exception) -> None:
        logging.error("%s for assistant %s: %s", message, assistant_id, error)
        cleanup_errors.append(f"{message}: {error}")

    def _release_pool_vm_sync(client: httpx.Client) -> None:
        try:
            client.post(
                f"{comms_url}/infra/vm/pool/release",
                headers=headers,
                json={"assistant_id": assistant_id},
                timeout=0.1,
            )
        except httpx.TimeoutException:
            logging.warning("release_pool_vm timed out for assistant %s", assistant_id)

    with httpx.Client() as client:
        try:
            label = assistant_id.lower().replace("_", "-")
            response = client.get(
                f"{comms_url}/infra/jobs",
                params={"label_selector": f"app=unity,assistant-id={label}"},
                headers=headers,
                timeout=10,
            )
            if response.status_code == 200:
                data = response.json()
                running_job_names = [
                    job["job_name"]
                    for job in data.get("jobs", [])
                    if job.get("status") == "Running"
                ]
                if running_job_names:
                    stop_response = client.post(
                        f"{comms_url}/infra/job/stop",
                        data={"job_name": running_job_names[0]},
                        headers=headers,
                        timeout=20,
                    )
                    stop_response.raise_for_status()
        except Exception as e:
            _record_error("Failed to stop job", e)
        finally:
            try:
                _release_pool_vm_sync(client)
            except Exception as e:
                _record_error("Failed to release pool VM", e)

        try:
            response = client.request(
                "DELETE",
                f"{comms_url}/infra/session/{assistant_id}",
                headers=headers,
                timeout=20,
            )
            response.raise_for_status()
        except Exception as e:
            _record_error("Failed to delete AssistantSession", e)

        try:
            client.request(
                "DELETE",
                f"{comms_url}/infra/pubsub/topic",
                headers=headers,
                data={"topic_name": f"unity-{assistant_id}{_env_suffix(deploy_env)}"},
                timeout=0.1,
            )
        except httpx.TimeoutException:
            logging.warning(
                "delete_pubsub_topic timed out for assistant %s",
                assistant_id,
            )
        except Exception as e:
            _record_error("Failed to delete pubsub topic", e)

        if _requires_assistant_disk_cleanup(desktop_mode):
            try:
                client.request(
                    "DELETE",
                    f"{comms_url}/infra/vm/pool/disk/{assistant_id}",
                    headers=headers,
                    timeout=0.1,
                )
            except httpx.TimeoutException:
                logging.warning(
                    "delete_assistant_disk timed out for assistant %s",
                    assistant_id,
                )
            except Exception as e:
                _record_error("Failed to delete assistant disk", e)

    return {
        "success": not cleanup_errors,
        "assistant_id": assistant_id,
        "errors": cleanup_errors,
    }


async def wake_up_assistant(assistant_id: str, deploy_env: str | None = None):
    wake_up_url = _adapters_url_for(deploy_env) + "/assistant/wakeup"
    async with httpx.AsyncClient() as client:
        return await client.post(
            wake_up_url,
            data={"assistant_id": assistant_id},
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            timeout=20,
        )


async def reawaken_assistant(assistant_id: str, deploy_env: str | None = None):
    """
    Triggers the assistant update webhook to reawaken or sync the assistant.
    Args:
        assistant_id (str): The ID of the assistant to reawaken.
        deploy_env: 'preview' for preview stack, None for native environment.
    Returns:
        The JSON response from the webhook.
    """
    reawaken_url = _adapters_url_for(deploy_env) + "/assistant/update"
    async with httpx.AsyncClient() as client:
        response = await client.post(
            reawaken_url,
            data={"assistant_id": assistant_id},
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            timeout=20,
        )
        response.raise_for_status()  # Raise an exception for bad status codes
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
    async with httpx.AsyncClient() as client:
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
    async with httpx.AsyncClient() as client:
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
