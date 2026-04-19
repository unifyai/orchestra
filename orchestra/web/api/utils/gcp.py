import json
import logging
from typing import Dict, List, Optional, Tuple, Union

from google.cloud import aiplatform, aiplatform_v1, pubsub_v1, storage
from google.cloud.exceptions import NotFound
from google.cloud.pubsub_v1.publisher.exceptions import MessageTooLargeError

from orchestra.settings import settings
from orchestra.web.api.utils.helpers import CustomEncoder

logger = logging.getLogger(__name__)

# Pub/Sub
try:
    PUBLISHER = pubsub_v1.PublisherClient()
except:
    PUBLISHER = None


def send_pubsub_msg(topic: str, msg: Dict[str, str]) -> None:
    # TODO: Make sure this sends msgs correctly in:
    # - Staging
    # - Local
    # - Tests / CI

    # Skip if PUBLISHER is not available (e.g., in test environment)
    if PUBLISHER is None:
        return

    try:
        PUBLISHER.publish(topic, json.dumps(msg, cls=CustomEncoder).encode())
    except MessageTooLargeError as e:
        logger.error(f"Error sending pubsub message: {e}")


# Cloud Storage


def parse_gcs_url(gcs_url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Parses a GCS URL (gs://bucket-name/object-path) and returns (bucket_name, object_path).
    Returns (None, None) if the URL is not a valid GCS URL.
    """
    if gcs_url and gcs_url.startswith("gs://"):
        parts = gcs_url[5:].split("/", 1)
        if len(parts) == 2:
            return parts[0], parts[1]
        elif len(parts) == 1:  # Bucket only, no path
            return parts[0], ""
    return None, None


def blob_exists(bucket_name: str, blob_name: str) -> bool:
    blob = storage.Client().bucket(bucket_name).blob(blob_name)
    try:
        blob.reload()
    except NotFound:
        return False
    return True


def get_input_tokens(user_id: str, dataset: str):
    bucket_name = "uploaded_datasets"
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(f"{user_id}/{dataset}/0/num_tokens.json")
    try:
        content = blob.download_as_bytes().decode("utf-8")
        return json.loads(content)["num_tokens"]
    except:
        return 1


def get_response_tokens(user_id: str, dataset: str, endpoint: str):
    bucket_name = "uploaded_datasets"
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(f"{user_id}/{dataset}/0/{endpoint}/num_tokens_in_responses.json")
    try:
        content = blob.download_as_bytes().decode("utf-8")
        return json.loads(content)["num_tokens"]
    except:
        return 1


def dir_exists(bucket_name: str, dir_name: str) -> bool:
    bucket = storage.Client().bucket(bucket_name)
    blobs = list(bucket.list_blobs(prefix=dir_name))
    return len(blobs) > 0


def delete(bucket_name: str, dir_name: str) -> None:
    bucket = storage.Client().bucket(bucket_name)

    # Ensure the directory_name ends with a slash
    if not dir_name.endswith("/"):
        dir_name += "/"

    # List all blobs with the directory_name prefix
    blobs = bucket.list_blobs(prefix=dir_name)

    # Delete each blob
    for blob in blobs:
        blob.delete()


def list_dir(bucket_name: str, prefix: str):
    bucket = storage.Client().bucket(bucket_name)
    # List blobs with the specified prefix
    return list(bucket.list_blobs(prefix=prefix))


def read_from_bucket(bucket_name, blob_name, raw=False, decode=False):
    blob = storage.Client().bucket(bucket_name).blob(blob_name)
    data = blob.download_as_bytes()
    if raw:
        if decode:
            return data.decode("utf-8")
        return data
    return json.loads(data.decode("utf-8"))


def upload_to_bucket(
    data: Union[str, Dict[str, str]],
    bucket_name: str,
    blob_name: str,
    content_type: str = "application/json",
):
    blob = storage.Client().bucket(bucket_name).blob(blob_name)
    blob.upload_from_string(data, content_type=content_type)


# GCP Vertex AI endpoint management (used for model deployment)


def vertex_ai_endpoint_exists(name: str) -> bool:
    endpoints = vertex_ai_endpoint_list()
    return name in endpoints


def vertex_ai_endpoint_list() -> List[str]:
    region = settings.gcp_location
    project_id = settings.gcp_project
    client_options = {"api_endpoint": f"{region}-aiplatform.googleapis.com"}
    client = aiplatform.gapic.EndpointServiceClient(client_options=client_options)

    # Specify the parent resource
    parent = f"projects/{project_id}/locations/{region}"

    # List the endpoints
    return [e.display_name for e in client.list_endpoints(parent=parent)]


def vertex_ai_endpoint_undeploy(user_id, name):
    region = settings.gcp_location
    project_id = settings.gcp_project
    client_options = {"api_endpoint": f"{region}-aiplatform.googleapis.com"}
    client = aiplatform_v1.EndpointServiceClient(client_options=client_options)

    parent = f"projects/{project_id}/locations/{region}"
    for e in client.list_endpoints(parent=parent):
        if e.display_name == f"{user_id}_{name}":
            endpoint_name = e.name
            deployed_model_id = e.deployed_models[0].id
            break
    else:
        raise Exception

    request = aiplatform_v1.UndeployModelRequest(
        endpoint=endpoint_name,
        deployed_model_id=deployed_model_id,
    )

    operation = client.undeploy_model(request=request)
    response = operation.result()
    return response
