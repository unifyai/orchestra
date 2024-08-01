import json
from typing import Dict, List

from google.cloud import aiplatform, pubsub_v1, storage
from google.cloud.exceptions import NotFound

from orchestra.web.api.utils.http_responses import evaluation_does_not_exist

# Pub/Sub


def send_pubsub_msg(topic: str, msg: Dict[str, str]) -> None:
    # TODO: Make sure this sends msgs correctly in:
    # - Staging
    # - Local
    # - Tests / CI

    # To instantiate with specific credentials
    # from google.oauth2 import service_account
    # key_path = "./archive/pubsub_2_clickhouse.json"
    # credentials = service_account.Credentials.from_service_account_file(key_path)
    # publisher = pubsub_v1.PublisherClient(credentials=credentials)

    publisher = pubsub_v1.PublisherClient()
    future = publisher.publish(topic, json.dumps(msg).encode())
    future.result()


# Cloud Storage


def blob_exists(bucket_name: str, blob_name: str) -> bool:
    blob = storage.Client().bucket(bucket_name).blob(blob_name)
    try:
        blob.reload()
    except NotFound:
        return False
    return True


def get_scores(user_id: str, dataset: str):
    bucket_name = "uploaded_datasets"
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(f"{user_id}/{dataset}/0/scores.json")
    try:
        content = blob.download_as_bytes().decode("utf-8")
        return json.loads(content)
    except:
        return evaluation_does_not_exist(dataset)


def dir_exists(bucket_name: str, dir_name: str) -> bool:
    bucket = storage.Client().bucket(bucket_name)
    blobs = list(bucket.list_blobs(prefix=dir_name))
    return len(blobs) > 0


def delete_dir(bucket_name: str, dir_name: str) -> None:
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


def read_json_from_bucket(bucket_name, blob_name, raw=False):
    blob = storage.Client().bucket(bucket_name).blob(blob_name)
    json_data = blob.download_as_bytes()
    if raw:
        return json_data
    return json.loads(json_data.decode("utf-8"))


def upload_json_to_bucket(json_data: Dict[str, str], bucket_name: str, blob_name: str):
    blob = storage.Client().bucket(bucket_name).blob(blob_name)
    blob.upload_from_string(json_data, content_type="application/json")


# VertexAI


def vertex_ai_endpoint_exists(name: str) -> bool:
    endpoints = vertex_ai_endpoint_list()
    return name in endpoints


def vertex_ai_endpoint_list() -> List[str]:
    region = "europe-west1"
    project_id = "saas-368716"
    client_options = {"api_endpoint": f"{region}-aiplatform.googleapis.com"}
    client = aiplatform.gapic.EndpointServiceClient(client_options=client_options)

    # Specify the parent resource
    parent = f"projects/{project_id}/locations/{region}"

    # List the endpoints
    return [e.display_name for e in client.list_endpoints(parent=parent)]
