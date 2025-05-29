import requests

COMMS_URL = "https://unity-comms-app-262420637606.us-central1.run.app"


def create_phone_number():
    """
    Create a phone number for the user by making a POST request to the comms endpoint.

    Args:
        first_name (str): User's first name
        last_name (str): User's last name

    Returns:
        JSON response from the phone creation endpoint
    """
    response = requests.post(
        f"{COMMS_URL}/phone/create",
        json={
            "voice_url": "https://us-central1-responsive-city-458413-a2.cloudfunctions.net/twilio-call-webhook",
            "sms_url": "https://us-central1-responsive-city-458413-a2.cloudfunctions.net/twilio-msg-webhook",
        },
    )
    return response.json()


def create_whatsapp_sender(phone_number: str, first_name: str, last_name: str):
    """
    Create a WhatsApp sender by making a POST request to the comms endpoint.

    Args:
        phone_number (str): The phone number for WhatsApp
        first_name (str): User's first name
        last_name (str): User's last name

    Returns:
        JSON response from the WhatsApp creation endpoint
    """
    response = requests.post(
        f"{COMMS_URL}/whatsapp/create",
        json={
            "phone_number": phone_number,
            "first_name": first_name,
            "last_name": last_name,
            "callback_url": "https://us-central1-responsive-city-458413-a2.cloudfunctions.net/twilio-whatsapp-webhook",
        },
    )
    return response.json()


def delete_phone_number(phone_number: str):
    """
    Delete a phone number by making a DELETE request to the comms endpoint.

    Args:
        phone_number (str): The phone number to delete

    Returns:
        JSON response from the phone deletion endpoint
    """
    url = f"{COMMS_URL}/phone/delete"
    payload = {"PhoneNumber": phone_number}
    response = requests.delete(url, json=payload)
    return response.json()


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
    url = f"{COMMS_URL}/email/create"
    payload = {
        "local": local,
        "first_name": first_name,
        "last_name": last_name,
    }
    response = requests.post(url, json=payload)
    return response.json()


def delete_email(email: str):
    """
    Delete an email by making a DELETE request to the comms endpoint.

    Args:
        email (str): The email address to delete

    Returns:
        JSON response from the email deletion endpoint
    """
    url = f"{COMMS_URL}/email/delete"
    payload = {
        "primary_email": email,
    }
    response = requests.delete(url, json=payload)
    return response.json()


def watch_email(email: str):
    """
    Watch an email by making a POST request to the comms endpoint.

    Args:
        email (str): The email to watch

    Returns:
        JSON response from the email watch endpoint
    """
    url = f"{COMMS_URL}/email/watch"
    payload = {
        "primary_email": email,
    }
    response = requests.post(url, json=payload)
    return response.json()


def create_pubsub_topic(assistant_id: str):
    """
    Create a pubsub topic for the assistant by making a POST request to the comms endpoint.

    Args:
        assistant_id (str): The ID of the assistant

    Returns:
        JSON response from the pubsub topic creation endpoint
    """
    url = f"{COMMS_URL}/infra/pubsub/topic"
    payload = {
        "assistant_id": assistant_id,
    }
    response = requests.post(url, data=payload)
    return response.json()


def delete_pubsub_topic(assistant_id: str):
    """
    Delete a pubsub topic for the assistant by making a DELETE request to the comms endpoint.

    Args:
        assistant_id (str): The ID of the assistant

    Returns:
        JSON response from the pubsub topic deletion endpoint
    """
    url = f"{COMMS_URL}/infra/pubsub/topic"
    payload = {
        "assistant_id": assistant_id,
    }
    response = requests.delete(url, data=payload)
    return response.json()


def create_cloud_run_job(
    assistant_id: str,
    user_name: str,
    assistant_number: str,
    user_number: str,
):
    """
    Create a Cloud Run job by making a POST request to the comms endpoint.

    Args:
        assistant_id (str): The ID of the assistant
        user_name (str): The user's name
        assistant_number (str): The assistant's phone number
        user_number (str): The user's phone number

    Returns:
        JSON response from the Cloud Run job creation endpoint
    """
    url = f"{COMMS_URL}/infra/job/create"
    payload = {
        "assistant_id": assistant_id,
        "user_name": user_name,
        "assistant_number": assistant_number,
        "user_number": user_number,
    }
    response = requests.post(url, data=payload)
    return response.json()


def delete_cloud_run_job(assistant_id: str):
    """
    Delete a Cloud Run job by making a DELETE request to the comms endpoint.

    Args:
        assistant_id (str): The ID of the assistant

    Returns:
        JSON response from the Cloud Run job deletion endpoint
    """
    url = f"{COMMS_URL}/infra/job/delete"
    payload = {
        "assistant_id": assistant_id,
    }
    response = requests.delete(url, data=payload)
    return response.json()


def start_cloud_run_job(assistant_id: str):
    """
    Start a Cloud Run job by making a POST request to the comms endpoint.

    Args:
        assistant_id (str): The ID of the assistant

    Returns:
        JSON response from the Cloud Run job start endpoint
    """
    url = f"{COMMS_URL}/infra/job/control"
    payload = {
        "assistant_id": assistant_id,
        "action": "start",
    }
    response = requests.post(url, data=payload)
    return response.json()


def stop_cloud_run_job(assistant_id: str):
    """
    Stop a Cloud Run job by making a POST request to the comms endpoint.

    Args:
        assistant_id (str): The ID of the assistant

    Returns:
        JSON response from the Cloud Run job stop endpoint
    """
    url = f"{COMMS_URL}/infra/job/control"
    payload = {
        "assistant_id": assistant_id,
        "action": "stop",
    }
    response = requests.post(url, data=payload)
    return response.json()


def get_cloud_run_job_status(assistant_id: str):
    """
    Get the status of a Cloud Run job by making a GET request to the comms endpoint.

    Args:
        assistant_id (str): The ID of the assistant

    Returns:
        JSON response containing the Cloud Run job status
    """
    url = f"{COMMS_URL}/infra/job/status"
    response = requests.get(
        url,
        params={
            "assistant_id": assistant_id,
        },
    )
    return response.json()
