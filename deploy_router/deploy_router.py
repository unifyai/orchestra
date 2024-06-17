#!pip install google-cloud-storage
import os
import subprocess
from google.cloud import storage
from google.cloud import aiplatform


def download_blob(bucket_name, source_blob_name, destination_file_name):
    pass
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    blobs = bucket.list_blobs(prefix=source_blob_name)
    for blob in blobs:
        filename = blob.name.split("test_router/")[-1].replace("/", "_")
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
        bucket_name="unify-router-data",
        source_blob_name=f"custom_routers/{user_id}/{router_name}",
        destination_file_name=save_path,
    )
    # TODO: check if it overwrites ??

    docker_path = (
        f"europe-west1-docker.pkg.dev/saas-368716/router/{user_id}/{router_name}"
    )

    # build the docker container
    # subprocess.run(
    #    [
    #        "sudo",
    #        "docker",
    #        "build",
    #        "--build-arg",
    #        f"root_path={save_path}",
    #        ".",
    #        "-t",
    #        docker_path,
    #    ]
    # )

    ## push the docker container to artifact registry

    # subprocess.run(
    #    "gcloud auth print-access-token | sudo docker login   -u oauth2accesstoken   --password-stdin europe-west1-docker.pkg.dev",
    #    shell=True,
    # )

    # subprocess.run(f"sudo docker push {docker_path}", shell=True)
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

    print(model.display_name)
    print(model.resource_name)

    # send this back to orchestra so we know where it's pointed
    

if __name__ == "__main__":
    deploy("test_user_id", "test_router")
