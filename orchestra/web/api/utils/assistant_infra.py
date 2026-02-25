import os
from typing import List, Literal

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


async def create_phone_number(phone_country: str = "US", is_staging: bool = False):
    """
    Create a phone number for the user by making a POST request to the comms endpoint.

    Args:
        phone_country (str): The country code for phone number provisioning (e.g., "US", "GB").
        is_staging (bool): Whether to create the phone number in staging or prod

    Returns:
        JSON response from the phone creation endpoint
    """
    voice_url = ADAPTERS_URL + "/twilio/call"
    sms_url = ADAPTERS_URL + "/twilio/sms"
    status_callback = ADAPTERS_URL + "/twilio/call-status"
    async with httpx.AsyncClient(timeout=90.0) as client:
        try:
            response = await client.post(
                f"{COMMS_URL}/phone/create",
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


async def assign_whatsapp_sender(user_whatsapp_number: str, is_staging: bool = False):
    """
    Create a WhatsApp sender by making a POST request to the comms endpoint.

    Args:
        user_whatsapp_number (str): The WhatsApp number to assign
        is_staging (bool): Whether to create the WhatsApp sender in staging or prod

    Returns:
        JSON response from the WhatsApp creation endpoint
    """
    callback_url = ADAPTERS_URL + "/twilio/whatsapp"
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{COMMS_URL}/whatsapp/create",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={
                "user_whatsapp_number": user_whatsapp_number,
                "callback_url": callback_url,
            },
            timeout=20,
        )
        return response.json()


async def delete_phone_number(phone_number: str):
    """
    Delete a phone number by making a DELETE request to the comms endpoint.

    Args:
        phone_number (str): The phone number to delete

    Returns:
        JSON response from the phone deletion endpoint
    """
    async with httpx.AsyncClient() as client:
        response = await client.request(
            "DELETE",
            f"{COMMS_URL}/phone/delete",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"PhoneNumber": phone_number},
            timeout=20,
        )
        return response.json()


async def create_email(local: str, first_name: str, last_name: str):
    """
    Create an email for the user by making a POST request to the UNIFY_COMMS_URL endpoint.

    Args:
        local (str): The local part of the email address
        first_name (str): User's first name
        last_name (str): User's last name

    Returns:
        Response from the email creation endpoint
    """
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{COMMS_URL}/gmail/create",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={
                "local": local,
                "first_name": first_name,
                "last_name": last_name,
            },
            timeout=20,
        )
        return response.json()


async def delete_email(email: str):
    """
    Delete an email by making a DELETE request to the comms endpoint.

    Args:
        email (str): The email address to delete

    Returns:
        JSON response from the email deletion endpoint
    """
    async with httpx.AsyncClient() as client:
        response = await client.request(
            "DELETE",
            f"{COMMS_URL}/gmail/delete",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"primary_email": email},
            timeout=20,
        )
        return response.json()


async def watch_email(email: str, is_staging: bool = False):
    """
    Watch an email by making a POST request to the comms endpoint.

    Args:
        email (str): The email to watch
        is_staging (bool): Whether to watch the email in staging or prod

    Returns:
        JSON response from the email watch endpoint
    """
    print(f"Watching email: {email}")
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{COMMS_URL}/gmail/watch",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={
                "primary_email": email,
                "topic": (
                    "gmail-notifications-staging"
                    if is_staging
                    else "gmail-notifications"
                ),
            },
            timeout=20,
        )
        return response.json()


async def create_pubsub_topic(assistant_id: str, is_staging: bool = False):
    """
    Create a pubsub topic for the assistant by making a POST request to the comms endpoint.

    Args:
        assistant_id (str): The ID of the assistant
        is_staging (bool): Whether to create the topic in staging or prod

    Returns:
        JSON response from the pubsub topic creation endpoint
    """
    topic_name = f"unity-{assistant_id}" + ("-staging" if is_staging else "")
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"{COMMS_URL}/infra/pubsub/topic",
                headers={"Authorization": f"Bearer {ADMIN_KEY}"},
                data={"topic_name": topic_name},
                timeout=40,
            )
            return response.json()
        except httpx.TimeoutException:
            print("Pubsub topic creation timed out")


async def delete_pubsub_topic(assistant_id: str, is_staging: bool = False):
    """
    Delete a pubsub topic for the assistant by making a DELETE request to the comms endpoint.

    Args:
        assistant_id (str): The ID of the assistant
        is_staging (bool): Whether to delete the topic in staging or prod

    Returns:
        JSON response from the pubsub topic deletion endpoint
    """
    topic_name = f"unity-{assistant_id}" + ("-staging" if is_staging else "")
    async with httpx.AsyncClient() as client:
        response = await client.request(
            "DELETE",
            f"{COMMS_URL}/infra/pubsub/topic",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            data={"topic_name": topic_name},
            timeout=20,
        )
        return response.json()


async def create_vm(
    assistant_id: str,
    unify_apikey: str,
    assistant_name: str,
    vm_type: Literal["windows", "ubuntu"],
):
    """
    Create a VM for the assistant via the infra service.

    Args:
        assistant_id: Numeric assistant ID (e.g., "12345")
        unify_apikey: API key used for VNC password
        assistant_name: Used for VM username
        vm_type: "windows" or "ubuntu"

    Returns:
        JSON response with vm_name, ip_address, hostname, desktop_url, status
    """
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{COMMS_URL}/infra/vm/create",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={
                "assistant_id": assistant_id,
                "unify_apikey": unify_apikey,
                "assistant_name": assistant_name,
                "vm_type": vm_type,
            },
            timeout=120,
        )
        return response.json()


async def delete_vm(
    assistant_id: str,
    vm_type: Literal["windows", "ubuntu"],
):
    """
    Delete a VM and associated resources (DNS, static IP).

    Args:
        assistant_id: Numeric assistant ID (e.g., "12345")
        vm_type: "windows" or "ubuntu"

    Returns:
        JSON response with vm_deleted, dns_deleted, ip_released flags
    """
    async with httpx.AsyncClient() as client:
        response = await client.request(
            "DELETE",
            f"{COMMS_URL}/infra/vm/delete",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"assistant_id": assistant_id, "vm_type": vm_type},
            timeout=120,
        )
        return response.json()


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


async def stop_jobs(assistant_id: str, session: Session):
    """
    Stop a job by making a POST request to the comms endpoint.

    Args:
        assistant_id: The assistant ID to stop jobs for
        session: SQLAlchemy database session
    """
    job_names = get_running_jobs(assistant_id, session)
    # if running job found, stop it
    if len(job_names) > 0:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{COMMS_URL}/infra/job/stop",
                data={"job_name": job_names[0]},
                headers={"Authorization": f"Bearer {ADMIN_KEY}"},
                timeout=20,
            )
            response.raise_for_status()

    return {"success": True, "job_names": job_names}


async def wake_up_assistant(assistant_id: str, is_staging: bool = False):
    wake_up_url = ADAPTERS_URL + "/assistant/wakeup"
    async with httpx.AsyncClient() as client:
        return await client.post(
            wake_up_url,
            data={"assistant_id": assistant_id},
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            timeout=20,
        )


async def reawaken_assistant(assistant_id: str, is_staging: bool = False):
    """
    Triggers the assistant update webhook to reawaken or sync the assistant.
    Args:
        assistant_id (str): The ID of the assistant to reawaken.
        is_staging (bool): Whether to use the staging or production webhook.
    Returns:
        The JSON response from the webhook.
    """
    reawaken_url = ADAPTERS_URL + "/assistant/update"
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
    is_staging: bool = False,
):
    """
    Logs pre-hire chat messages for an assistant using the webhook.
    Args:
        assistant_id (str): The ID of the assistant.
        messages (list): A list of chat message dictionaries.
        is_staging (bool): Whether to use the staging or production webhook.
    Returns:
        The JSON response from the webhook.
    """
    log_pre_hire_chat_url = ADAPTERS_URL + "/unity/pre-hire"
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


async def trigger_contact_sync(assistant_id: int) -> dict:
    """
    Trigger contact sync for an assistant via the system-event webhook.

    Args:
        assistant_id: The assistant ID to sync contacts for

    Returns:
        JSON response from the webhook
    """
    url = f"{ADAPTERS_URL}/unity/system-event"
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
