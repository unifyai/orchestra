import os

import requests

COMMS_URL = "https://unity-comms-app-262420637606.us-central1.run.app"
ADMIN_KEY = os.environ.get("ORCHESTRA_ADMIN_KEY")


def create_phone_number(country: str = "US"):
    """
    Create a phone number for the user by making a POST request to the comms endpoint.

    Args:
        country (str): The country code for phone number provisioning (e.g., "US", "GB").

    Returns:
        JSON response from the phone creation endpoint
    """
    return requests.post(
        f"{COMMS_URL}/phone/create",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        json={
            "voice_url": "https://us-central1-responsive-city-458413-a2.cloudfunctions.net/twilio-call-webhook",
            "sms_url": "https://us-central1-responsive-city-458413-a2.cloudfunctions.net/twilio-msg-webhook",
            "country": country,
        },
    ).json()


def assign_whatsapp_sender(user_whatsapp_number: str):
    """
    Create a WhatsApp sender by making a POST request to the comms endpoint.

    Args:
        phone_number (str): The phone number for WhatsApp
        first_name (str): User's first name
        last_name (str): User's last name

    Returns:
        JSON response from the WhatsApp creation endpoint
    """
    return requests.post(
        f"{COMMS_URL}/whatsapp/create",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        json={
            "user_whatsapp_number": user_whatsapp_number,
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
        f"{COMMS_URL}/email/create",
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
        f"{COMMS_URL}/email/delete",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        json={"primary_email": email},
    ).json()


def watch_email(email: str):
    """
    Watch an email by making a POST request to the comms endpoint.

    Args:
        email (str): The email to watch

    Returns:
        JSON response from the email watch endpoint
    """
    print(f"Watching email: {email}")
    return requests.post(
        f"{COMMS_URL}/email/watch",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        json={"primary_email": email},
    ).json()


def create_pubsub_topic(assistant_id: str):
    """
    Create a pubsub topic for the assistant by making a POST request to the comms endpoint.

    Args:
        assistant_id (str): The ID of the assistant

    Returns:
        JSON response from the pubsub topic creation endpoint
    """
    return requests.post(
        f"{COMMS_URL}/infra/pubsub/topic",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        data={"assistant_id": assistant_id},
    ).json()


def delete_pubsub_topic(assistant_id: str):
    """
    Delete a pubsub topic for the assistant by making a DELETE request to the comms endpoint.

    Args:
        assistant_id (str): The ID of the assistant

    Returns:
        JSON response from the pubsub topic deletion endpoint
    """
    return requests.delete(
        f"{COMMS_URL}/infra/pubsub/topic",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        data={"assistant_id": assistant_id},
    ).json()


def create_cloud_run_service(
    api_key: str,
    assistant_id: str,
    user_name: str,
    assistant_number: str,
    user_number: str,
):
    """
    Create a Cloud Run service by making a POST request to the comms endpoint.

    Args:
        api_key (str): The API key of the user
        assistant_id (str): The ID of the assistant
        user_name (str): The user's name
        assistant_number (str): The assistant's phone number
        user_number (str): The user's phone number

    Returns:
        JSON response from the Cloud Run service creation endpoint
    """
    return requests.post(
        f"{COMMS_URL}/infra/service/create",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        data={
            "api_key": api_key,
            "assistant_id": assistant_id,
            "user_name": user_name,
            "assistant_number": assistant_number,
            "user_number": user_number,
        },
    ).json()


def delete_cloud_run_service(assistant_id: str):
    """
    Delete a Cloud Run service by making a DELETE request to the comms endpoint.

    Args:
        assistant_id (str): The ID of the assistant

    Returns:
        JSON response from the Cloud Run service deletion endpoint
    """
    return requests.delete(
        f"{COMMS_URL}/infra/service/delete",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        data={"assistant_id": assistant_id},
    ).json()


def get_social_platforms_costs():
    """
    Fetch available social platforms and their costs.
    """
    return requests.get(
        f"{COMMS_URL}/social/available-platforms",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
    ).json()
