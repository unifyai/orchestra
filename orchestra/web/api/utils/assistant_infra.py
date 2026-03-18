import logging
import os
from typing import List

import httpx
from sqlalchemy import and_
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    Context,
    LogEvent,
    LogEventContext,
    Project,
)

COMMS_URL = os.environ.get("UNITY_COMMS_URL")
ADAPTERS_URL = os.environ.get("UNITY_ADAPTERS_URL")
ADMIN_KEY = os.environ.get("ORCHESTRA_ADMIN_KEY")


def _current_deploy_env() -> str:
    return "staging" if os.environ.get("STAGING", "False") == "True" else "production"


def _normalize_deploy_env(deploy_env: str | None) -> str:
    if deploy_env in {"production", "staging", "preview"}:
        return deploy_env
    return _current_deploy_env()


def _env_suffix(deploy_env: str) -> str:
    return "" if deploy_env == "production" else f"-{deploy_env}"


def _cloud_run_url(service_name: str) -> str:
    return f"https://{service_name}-721804302511.us-central1.run.app"


def _service_url(
    *,
    current_url: str | None,
    env_var_base: str,
    service_name: str,
    deploy_env: str | None,
) -> str:
    normalized_env = _normalize_deploy_env(deploy_env)
    current_env = _current_deploy_env()
    if normalized_env == current_env and current_url:
        return current_url

    override = os.environ.get(f"{env_var_base}_{normalized_env.upper()}")
    if override:
        return override

    target_service = (
        service_name
        if normalized_env == "production"
        else f"{service_name}-{normalized_env}"
    )
    return _cloud_run_url(target_service)


def _comms_url_for(deploy_env: str | None) -> str:
    return _service_url(
        current_url=COMMS_URL,
        env_var_base="UNITY_COMMS_URL",
        service_name="unity-comms-app",
        deploy_env=deploy_env,
    )


def _adapters_url_for(deploy_env: str | None) -> str:
    return _service_url(
        current_url=ADAPTERS_URL,
        env_var_base="UNITY_ADAPTERS_URL",
        service_name="unity-adapters",
        deploy_env=deploy_env,
    )


async def create_phone_number(
    phone_country: str = "US",
    deploy_env: str = "production",
):
    """
    Create a phone number for the user by making a POST request to the comms endpoint.

    Args:
        phone_country (str): The country code for phone number provisioning (e.g., "US", "GB").
        deploy_env (str): Target deployment environment for the provisioned resources

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


async def assign_whatsapp_sender(
    user_whatsapp_number: str,
    deploy_env: str = "production",
):
    """
    Create a WhatsApp sender by making a POST request to the comms endpoint.

    Args:
        user_whatsapp_number (str): The WhatsApp number to assign
        deploy_env (str): Target deployment environment for the provisioned resources

    Returns:
        JSON response from the WhatsApp creation endpoint
    """
    comms_url = _comms_url_for(deploy_env)
    callback_url = _adapters_url_for(deploy_env) + "/twilio/whatsapp"
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{comms_url}/whatsapp/create",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={
                "user_whatsapp_number": user_whatsapp_number,
                "callback_url": callback_url,
            },
            timeout=20,
        )
        return response.json()


async def delete_phone_number(phone_number: str, deploy_env: str = "production"):
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
    deploy_env: str = "production",
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


async def delete_email(email: str, deploy_env: str = "production"):
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


async def watch_email(email: str, deploy_env: str = "production"):
    """
    Watch an email by making a POST request to the comms endpoint.

    Args:
        email (str): The email to watch
        deploy_env (str): Target deployment environment for the provisioned resources

    Returns:
        JSON response from the email watch endpoint
    """
    normalized_env = _normalize_deploy_env(deploy_env)
    comms_url = _comms_url_for(normalized_env)
    print(f"Watching email: {email}")
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{comms_url}/gmail/watch",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={
                "primary_email": email,
                "topic": f"gmail-notifications{_env_suffix(normalized_env)}",
            },
            timeout=20,
        )
        return response.json()


async def create_pubsub_topic(assistant_id: str, deploy_env: str = "production"):
    """
    Create a pubsub topic for the assistant by making a POST request to the comms endpoint.

    Args:
        assistant_id (str): The ID of the assistant
        deploy_env (str): Target deployment environment for the topic

    Returns:
        JSON response from the pubsub topic creation endpoint
    """
    normalized_env = _normalize_deploy_env(deploy_env)
    comms_url = _comms_url_for(normalized_env)
    topic_name = f"unity-{assistant_id}{_env_suffix(normalized_env)}"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"{comms_url}/infra/pubsub/topic",
                headers={"Authorization": f"Bearer {ADMIN_KEY}"},
                data={"topic_name": topic_name},
                timeout=10,
            )
            return response.json()
        except httpx.TimeoutException:
            print("Pubsub topic creation timed out")


async def delete_pubsub_topic(assistant_id: str, deploy_env: str = "production"):
    """
    Delete a pubsub topic for the assistant by making a DELETE request to the comms endpoint.

    Args:
        assistant_id (str): The ID of the assistant
        deploy_env (str): Target deployment environment for the topic

    Returns:
        JSON response from the pubsub topic deletion endpoint
    """
    normalized_env = _normalize_deploy_env(deploy_env)
    comms_url = _comms_url_for(normalized_env)
    topic_name = f"unity-{assistant_id}{_env_suffix(normalized_env)}"
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


async def release_pool_vm(assistant_id: str, deploy_env: str = "production"):
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


async def delete_assistant_disk(assistant_id: str, deploy_env: str = "production"):
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


def get_running_jobs(assistant_id: str, session: Session) -> List[str]:
    """
    Get running jobs for the assistant by querying the database directly.

    Args:
        assistant_id: The assistant ID to find running jobs for
        session: SQLAlchemy database session

    Returns:
        List of job names that are currently running for this assistant
    """
    # Find the AssistantJobs project and startup_events context
    project = session.query(Project).filter(Project.name == "AssistantJobs").first()
    if not project:
        return []

    context = (
        session.query(Context)
        .filter(
            and_(
                Context.project_id == project.id,
                Context.name == "startup_events",
            ),
        )
        .first()
    )
    if not context:
        return []

    # Query LogEvent entries where assistant_id matches and running is True
    log_events = (
        session.query(LogEvent)
        .join(LogEventContext, LogEvent.id == LogEventContext.log_event_id)
        .filter(
            and_(
                LogEventContext.context_id == context.id,
                LogEvent.data["assistant_id"].astext == str(assistant_id),
                LogEvent.data["running"].astext == "true",
            ),
        )
        .all()
    )

    job_names = [
        log_event.data.get("job_name")
        for log_event in log_events
        if log_event.data.get("job_name")
    ]
    return job_names


async def stop_jobs(
    assistant_id: str,
    session: Session,
    deploy_env: str = "production",
):
    """
    Stop a job and release any assigned pool VM.

    Args:
        assistant_id: The assistant ID to stop jobs for
        session: SQLAlchemy database session
    """
    comms_url = _comms_url_for(deploy_env)
    job_names = get_running_jobs(assistant_id, session)
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


async def wake_up_assistant(assistant_id: str, deploy_env: str = "production"):
    wake_up_url = _adapters_url_for(deploy_env) + "/assistant/wakeup"
    async with httpx.AsyncClient() as client:
        return await client.post(
            wake_up_url,
            data={"assistant_id": assistant_id},
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            timeout=20,
        )


async def reawaken_assistant(assistant_id: str, deploy_env: str = "production"):
    """
    Triggers the assistant update webhook to reawaken or sync the assistant.
    Args:
        assistant_id (str): The ID of the assistant to reawaken.
        deploy_env (str): Target deployment environment for the assistant webhook.
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
    deploy_env: str = "production",
):
    """
    Logs pre-hire chat messages for an assistant using the webhook.
    Args:
        assistant_id (str): The ID of the assistant.
        messages (list): A list of chat message dictionaries.
        deploy_env (str): Target deployment environment for the assistant webhook.
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
    deploy_env: str = "production",
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
