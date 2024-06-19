import os
import subprocess
import requests
from google.cloud import storage
from google.cloud import aiplatform


def download_blob(bucket_name, source_blob_name, destination_file_name):
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    blobs = bucket.list_blobs(prefix=source_blob_name)
    for blob in blobs:
        filename = blob.name.split("/")[-1]
        blob.download_to_filename(destination_file_name + filename)


def deploy(user_id: str, router_name: str):
    # fetch the router files + weights from bucket
    if not os.path.isdir("router_files"):
        os.mkdir("router_files")

    save_path = f"router_files/{user_id}/{router_name}/"
    if os.path.isdir(save_path):
        print(f"overwriting files in {save_path}")
    else:
        os.makedirs(save_path)

    download_blob(
        bucket_name="custom_router_data",
        source_blob_name=f"custom_router/{user_id}/{router_name}",
        destination_file_name=save_path,
    )
    # TODO: check if it overwrites ??

    docker_path = (
        f"europe-west1-docker.pkg.dev/saas-368716/router/{user_id}/{router_name}"
    )

    # build the docker container
    subprocess.run(
        f"sudo docker build --build-arg root_path={save_path} . -t {docker_path}",
        shell=True,
    )

    ## push the docker container to artifact registry

    subprocess.run(
        "gcloud auth print-access-token | sudo docker login   -u oauth2accesstoken   --password-stdin europe-west1-docker.pkg.dev",
        shell=True,
    )

    subprocess.run(f"sudo docker push {docker_path}", shell=True)
    # create a new model_version in model registry

    location = "europe-west1"
    project = "saas-368716"
    aiplatform.init(project=project, location=location)

    display_name = f"{user_id}_{router_name}"

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
    # model.deploy(
    #     endpoint=endpoint,
    #     deployed_model_display_name=display_name,
    #     traffic_percentage=100,
    #     min_replica_count=1,
    #     max_replica_count=1,
    #     accelerator_type="NVIDIA_TESLA_T4",
    #     accelerator_count=1,
    #     sync=True,
    # )

    model.wait()

    # send this back to orchestra so we know where it's pointed
    payload = {
        "user_id": user_id,
        "router_name": router_name,
        "router_id": endpoint.name,
    }
    url = f'{os.getenv("ORCHESTRA_BASE_URL")}/v0/admin/create_custom_router'
    headers = {"Authorization": f'Bearer {os.getenv("ORCHESTRA_ADMIN_KEY")}'}
    response = requests.put(url=url, json=payload, headers=headers)
    print(response.text)

    # clean-up weights : TODO


if __name__ == "__main__":
    deploy("clb5hx8d40002s601hooxp3ct", "a_test_router")
