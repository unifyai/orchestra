import json
import os
import sys
import subprocess

from httpx import AsyncClient, Limits

import requests
from google.cloud import aiplatform, storage


def download_blob(bucket_name, source_blob_name, destination_file_name):
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    blobs = bucket.list_blobs(prefix=source_blob_name)
    for blob in blobs:
        filename = blob.name.split("/")[-1]
        blob.download_to_filename(destination_file_name + filename)


def deploy_router(msg, client=None):
    # fetch the router files + weights from bucket
    # TODO: cleanup old weights
    # if os.path.isdir("router_files"):
    #     os.remove("router_files")
    print("start deployment")

    msg = json.loads(msg)
    user_id = msg["user_id"]
    router_id = msg["router_id"]
    orchestra_url = msg["orchestra_url"]
    admin_key = msg["admin_key"]

    if client is None:
        limits = Limits(
            max_keepalive_connections=None,
            max_connections=None,
            keepalive_expiry=30,
        )
        client = AsyncClient(base_url=orchestra_url, limits=limits, timeout=60)

    if not os.path.isdir("router_files"):
        os.mkdir("router_files")

    save_path = f"router_files/{user_id}/{router_id}/"
    if os.path.isdir(save_path):
        print(f"overwriting files in {save_path}")
    else:
        os.makedirs(save_path)

    print("downloading weights")
    download_blob(
        bucket_name="custom_router_data",
        source_blob_name=f"custom_router/{user_id}/{router_id}",
        destination_file_name=save_path,
    )
    # TODO: check if it overwrites ??

    docker_path = (
        f"europe-west1-docker.pkg.dev/saas-368716/router/{user_id}/{router_id}"
    )

    # TODO: use the docker sdk
    # build the docker container
    print("building docker container")
    subprocess.run(
        f"sudo docker build --build-arg root_path={save_path} . -t {docker_path}",
        shell=True,
    )

    ## push the docker container to artifact registry
    print("pushing docker container to registry")
    subprocess.run(
        "gcloud auth print-access-token | sudo docker login   -u oauth2accesstoken   --password-stdin europe-west1-docker.pkg.dev",
        shell=True,
    )

    subprocess.run(f"sudo docker push {docker_path}", shell=True)
    # create a new model_version in model registry

    # TODO: This is hardcoded atm
    location = "europe-west1"
    project = "saas-368716"
    aiplatform.init(project=project, location=location)

    display_name = f"{user_id}_{router_id}"

    print("uploading model")
    model = aiplatform.Model.upload(
        display_name=display_name,
        serving_container_image_uri=docker_path,
        serving_container_predict_route="/health",
        serving_container_health_route="/predict",
        serving_container_ports=[80],
    )

    print("creating endpoint ... ")
    endpoint = aiplatform.Endpoint.create(display_name=display_name)
    print("deploying model")
    model.deploy(
        endpoint=endpoint,
        deployed_model_display_name=display_name,
        traffic_percentage=100,
        min_replica_count=1,
        max_replica_count=1,
        accelerator_type="NVIDIA_TESLA_T4",
        accelerator_count=1,
        sync=True,
    )

    model.wait()

    # send this back to orchestra so we know where it's pointed
    payload = {
        "user_id": user_id,
        "router_id": router_id,
        "gcp_router_id": endpoint.name,
    }
    url = f"{orchestra_url}/v0/update_router_deployed"
    headers = {"Authorization": f"Bearer {admin_key}"}
    response = requests.post(url=url, json=payload, headers=headers)
    print(response.text)

    # TODO: clean up docker image


if __name__ == "__main__":
    msg = sys.argv[1]
    deploy_router(msg)
