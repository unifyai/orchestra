import os

import requests
import unify

COMMS_URL = os.environ.get("UNITY_COMMS_URL")
ADAPTERS_URL = os.environ.get("UNITY_ADAPTERS_URL")
ADMIN_KEY = os.environ.get("ORCHESTRA_ADMIN_KEY")


def create_phone_number(phone_country: str = "US", is_staging: bool = False):
    """
    Create a phone number for the user by making a POST request to the comms endpoint.

    Args:
        phone_country (str): The country code for phone number provisioning (e.g., "US", "GB").
        is_staging (bool): Whether to create the phone number in staging or prod

    Returns:
        JSON response from the phone creation endpoint
    """
    voice_url = ADAPTERS_URL + "/twilio/call"
    sms_url = ADAPTERS_URL + "/twilio/msg"
    status_callback = ADAPTERS_URL + "/twilio/call-status"
    return requests.post(
        f"{COMMS_URL}/phone/create",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        json={
            "voice_url": voice_url,
            "sms_url": sms_url,
            "status_callback": status_callback,
            "phone_country": phone_country,
        },
    ).json()


def assign_whatsapp_sender(user_whatsapp_number: str, is_staging: bool = False):
    """
    Create a WhatsApp sender by making a POST request to the comms endpoint.

    Args:
        user_whatsapp_number (str): The WhatsApp number to assign
        is_staging (bool): Whether to create the WhatsApp sender in staging or prod

    Returns:
        JSON response from the WhatsApp creation endpoint
    """
    callback_url = ADAPTERS_URL + "/twilio/whatsapp"
    return requests.post(
        f"{COMMS_URL}/whatsapp/create",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        json={
            "user_whatsapp_number": user_whatsapp_number,
            "callback_url": callback_url,
        },
    ).json()


def delete_phone_number(phone_number: str):
    """
    Delete a phone number by making a DELETE request to the comms endpoint.

    Args:
        phone_number (str): The phone number to delete

    Returns:
        JSON response from the phone deletion endpoint
    """
    return requests.delete(
        f"{COMMS_URL}/phone/delete",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        json={"PhoneNumber": phone_number},
    ).json()


def create_email(local: str, first_name: str, last_name: str):
    """
    Create an email for the user by making a POST request to the UNIFY_COMMS_URL endpoint.

    Args:
        local (str): The local part of the email address
        first_name (str): User's first name
        last_name (str): User's last name

    Returns:
        Response from the email creation endpoint
    """
    return requests.post(
        f"{COMMS_URL}/gmail/create",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        json={
            "local": local,
            "first_name": first_name,
            "last_name": last_name,
        },
    ).json()


def delete_email(email: str):
    """
    Delete an email by making a DELETE request to the comms endpoint.

    Args:
        email (str): The email address to delete

    Returns:
        JSON response from the email deletion endpoint
    """
    return requests.delete(
        f"{COMMS_URL}/gmail/delete",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        json={"primary_email": email},
    ).json()


def watch_email(email: str, is_staging: bool = False):
    """
    Watch an email by making a POST request to the comms endpoint.

    Args:
        email (str): The email to watch
        is_staging (bool): Whether to watch the email in staging or prod

    Returns:
        JSON response from the email watch endpoint
    """
    print(f"Watching email: {email}")
    return requests.post(
        f"{COMMS_URL}/gmail/watch",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        json={
            "primary_email": email,
            "topic": "email-notifications-staging"
            if is_staging
            else "email-notifications",
        },
    ).json()


def create_pubsub_topic(assistant_id: str, is_staging: bool = False):
    """
    Create a pubsub topic for the assistant by making a POST request to the comms endpoint.

    Args:
        assistant_id (str): The ID of the assistant
        is_staging (bool): Whether to create the topic in staging or prod

    Returns:
        JSON response from the pubsub topic creation endpoint
    """
    topic_name = f"unity-{assistant_id}" + ("-staging" if is_staging else "")
    return requests.post(
        f"{COMMS_URL}/infra/pubsub/topic",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        data={"topic_name": topic_name},
    ).json()


def delete_pubsub_topic(assistant_id: str, is_staging: bool = False):
    """
    Delete a pubsub topic for the assistant by making a DELETE request to the comms endpoint.

    Args:
        assistant_id (str): The ID of the assistant
        is_staging (bool): Whether to delete the topic in staging or prod

    Returns:
        JSON response from the pubsub topic deletion endpoint
    """
    topic_name = f"unity-{assistant_id}" + ("-staging" if is_staging else "")
    return requests.delete(
        f"{COMMS_URL}/infra/pubsub/topic",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        data={"topic_name": topic_name},
    ).json()


def get_social_platforms_costs():
    """
    Fetch available social platforms and their costs.
    """
    return requests.get(
        f"{COMMS_URL}/social/available-platforms",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
    ).json()


def get_running_jobs(assistant_id: str):
    """
    Get running jobs for the assistant.
    """
    # get running jobs for the assistant
    logs = unify.get_logs(
        project="AssistantJobs",
        context="startup_events",
        filter=f"assistant_id == {assistant_id} and running == True",
    )
    job_names = [log.to_json()["entries"]["job_name"] for log in logs]
    return job_names


def stop_jobs(assistant_id: str):
    """
    Stop a job by making a POST request to the comms endpoint.
    """
    job_names = get_running_jobs(assistant_id)
    # if running job found, stop it
    if len(job_names) > 0:
        response = requests.post(
            f"{COMMS_URL}/infra/job/stop",
            data={"job_name": job_names[0]},
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        )
        response.raise_for_status()

    return {"success": True, "job_names": job_names}


def wake_up_assistant(assistant_id: str, is_staging: bool = False):
    wake_up_url = ADAPTERS_URL + "/assistant/wakeup"
    return requests.post(
        wake_up_url,
        data={"assistant_id": assistant_id},
    )


def reawaken_assistant(assistant_id: str, is_staging: bool = False):
    """
    Triggers the assistant update webhook to reawaken or sync the assistant.
    Args:
        assistant_id (str): The ID of the assistant to reawaken.
        is_staging (bool): Whether to use the staging or production webhook.
    Returns:
        The JSON response from the webhook.
    """
    reawaken_url = ADAPTERS_URL + "/assistant/update"
    response = requests.post(
        reawaken_url,
        data={"assistant_id": assistant_id},
    )
    response.raise_for_status()  # Raise an exception for bad status codes
    return response.json()


def log_pre_hire_chat(assistant_id: str, messages: list, is_staging: bool = False):
    """
    Logs pre-hire chat messages for an assistant using the webhook.
    Args:
        assistant_id (str): The ID of the assistant.
        messages (list): A list of chat message dictionaries.
        is_staging (bool): Whether to use the staging or production webhook.
    Returns:
        The JSON response from the webhook.
    """
    log_pre_hire_chat_url = ADAPTERS_URL + "/log-pre-hire-chats"
    payload = {"assistant_id": assistant_id, "body": messages}
    response = requests.post(
        log_pre_hire_chat_url,
        headers={
            "Authorization": f"Bearer {ADMIN_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
    )
    response.raise_for_status()
    return {"status": "success"}
