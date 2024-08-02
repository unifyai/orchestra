import argparse
import subprocess
import sys
import time
import json
import re
import math
import random
import requests
import os
from dataclasses import dataclass
from typing import Any, List

import google.cloud.compute_v1 as compute_v1
from google.api_core.extended_operation import ExtendedOperation
from google.cloud import storage
from google.cloud.exceptions import NotFound


def wait_for_extended_operation(
    operation: ExtendedOperation,
    verbose_name: str = "operation",
    timeout: int = 300,
) -> Any:
    result = operation.result(timeout=timeout)

    if operation.error_code:
        print(
            f"Error during {verbose_name}: [Code: {operation.error_code}]: {operation.error_message}",
            file=sys.stderr,
            flush=True,
        )
        print(f"Operation ID: {operation.name}", file=sys.stderr, flush=True)
        raise operation.exception() or RuntimeError(operation.error_message)

    if operation.warnings:
        print(f"Warnings during {verbose_name}:\n", file=sys.stderr, flush=True)
        for warning in operation.warnings:
            print(f" - {warning.code}: {warning.message}", file=sys.stderr, flush=True)

    return result


def create_vm():
    boot_disk = compute_v1.AttachedDisk()
    initialize_params = compute_v1.AttachedDiskInitializeParams()
    initialize_params.source_image = "projects/ml-images/global/images/c0-deeplearning-common-cu122-v20240613-debian-11"
    initialize_params.disk_size_gb = 50
    initialize_params.disk_type = (
        "projects/saas-368716/zones/europe-west1-b/diskTypes/pd-balanced"
    )
    boot_disk.initialize_params = initialize_params
    boot_disk.auto_delete = True
    boot_disk.boot = True

    project_id = "saas-368716"
    zone = "europe-west1-b"
    instance_name = "router-training-gpu1"
    disks = [boot_disk]
    machine_type = "zones/europe-west1-b/machineTypes/n1-standard-2"
    network_link = "global/networks/default"
    subnetwork_link = "regions/europe-west1/subnetworks/default"

    instance_client = compute_v1.InstancesClient()

    # Use the network interface provided in the network_link argument.
    network_interface = compute_v1.NetworkInterface()
    network_interface.network = network_link
    network_interface.subnetwork = subnetwork_link

    access = compute_v1.AccessConfig()
    access.type_ = compute_v1.AccessConfig.Type.ONE_TO_ONE_NAT.name
    access.name = "External NAT"
    access.network_tier = access.NetworkTier.PREMIUM.name
    network_interface.access_configs = [access]

    # Collect information into the Instance object.
    instance = compute_v1.Instance()
    instance.network_interfaces = [network_interface]
    instance.name = instance_name
    instance.disks = disks

    instance.machine_type = machine_type

    instance.scheduling = compute_v1.Scheduling()

    accelerators = [
        compute_v1.AcceleratorConfig(
            accelerator_count=1,
            accelerator_type="projects/saas-368716/zones/europe-west1-b/acceleratorTypes/nvidia-tesla-t4",
        ),
    ]
    instance.guest_accelerators = accelerators
    instance.scheduling.on_host_maintenance = (
        compute_v1.Scheduling.OnHostMaintenance.TERMINATE.name
    )

    # Prepare the request to insert an instance.
    request = compute_v1.InsertInstanceRequest()
    request.zone = zone
    request.project = project_id
    request.instance_resource = instance

    # Wait for the create operation to complete.
    print(f"Creating the {instance_name} instance in {zone}...")

    operation = instance_client.insert(request=request)

    wait_for_extended_operation(operation, "instance creation")

    print(f"Instance {instance_name} created.")
    return instance_client.get(project=project_id, zone=zone, instance=instance_name)


@dataclass
class TrainRequest:
    train_cfg: dict
    bucket_path: str


def evaluation_available(user_id, dataset_name, endpoint):
    bucket_name = f"uploaded_datasets"
    blob_dir = f"{user_id}/{dataset_name}/0/{endpoint}/"

    blob_names = [
        blob_dir + "responses.jsonl",
        blob_dir + "judgements.jsonl",
    ]

    for blob_name in blob_names:
        blob = storage.Client().bucket(bucket_name).blob(blob_name)
        try:
            blob.reload()
        except NotFound:
            return False
    return True


def start_evaluation(api_key, base_url, dataset, endpoint):
    url = base_url + "/evaluation"
    headers = {
        "Authorization": f"Bearer {api_key}",
    }
    payload = {"dataset": dataset, "endpoint": endpoint}
    response = requests.post(url, params=payload, headers=headers)

    # TODO: Log this properly
    print(response.status_code)
    print(response.text)


def extract_judgement(text):
    score_mapping: {
        "irrelevant": 0,
        "very_bad": 0,
        "very_good": 0.8,
        "very bad": 0,
        "bad": 0,
        "good": 0.5,
        "satisfactory": 0.5,
        "very good": 0.8,
        "excellent": 1.0,
    }

    json_text = re.search(
        '\{[\n\r\s]+"assistant_rating":.*?\}', text, flags=re.DOTALL | re.MULTILINE
    )
    if json_text is None:
        return float("nan")

    judge_response = json_text.group(0)

    try:
        rating = json.loads(judge_response)["assistant_rating"]

        if isinstance(rating, list):
            rating = rating[0]
        try:
            rating = score_mapping[rating.lower()]
        except:
            return 0.0
        return rating

    except:
        return float("nan")


def create_train_data(user_id, dataset, endpoints):
    bucket_name = "uploaded_datasets"
    all_prompts = []
    id_to_prompt = {}
    id_model_to_score = {}
    for endpoint in endpoints:
        blob_name = f"{user_id}/{dataset}/0/{endpoint}/judgements.jsonl"
        blob = storage.Client().bucket(bucket_name).blob(blob_name)
        ret = blob.download_as_bytes().decode("utf-8").split("\n")
        for line in ret:
            if line:
                data = json.loads(line)
                data["score"] = extract_judgement(data["judge_response"])
                del data["judge_response"]
                all_prompts.append(data)
                id_to_prompt[data["id_"]] = data["prompt"]
                id_model_to_score[(data["id_"], endpoint)] = data["score"]

    # partition:
    combined_files = []
    for id_ in id_to_prompt:
        scores_per_endpoint = {e: id_model_to_score[(id_, e)] for e in endpoints}
        combined_files.append(
            {"id_": id_, "prompt": id_to_prompt[id_], "scores": scores_per_endpoint}
        )

    n = len(combined_files)
    random.shuffle(combined_files)
    train_frac = 0.8
    valid_num = math.floor((1 - train_frac) * n)

    train_data = combined_files[valid_num:]
    valid_data = combined_files[:valid_num]

    return train_data, valid_data

import logging
logger = logging.getLogger(__name__)

def main(user_id, api_key, router_name, dataset, endpoints, orchestra_url):
    logger.info("starting TRAINING")
    for e in endpoints:
        if not evaluation_available(user_id, dataset, e):
            print(f"starting evaluation for {e}")
            start_evaluation(api_key, orchestra_url, dataset, e)

    timeout = 2 * 3600
    start_time = time.time()
    while (time.time() - start_time) < timeout:
        if all([evaluation_available(user_id, dataset, e) for e in endpoints]):
            break
        time.sleep(60)
    else:
        raise Exception

    train_data, valid_data = create_train_data(user_id, dataset, endpoints)
    # TODO: train data
    unrolled_data = []
    print(train_data)
    for td in train_data:
        for e, score in td["scores"].items():
            unrolled_data.append(
                {
                    "id_": td["id_"],
                    "prompt": td["prompt"],
                    "model_provider": e,
                    "score": score,
                }
            )
    train_data = unrolled_data

    dir = os.path.dirname(os.path.abspath(__file__))
    with open(
        os.path.join(dir, "gpu_vm_files", "train_job_files", "train_data.jsonl"), "w"
    ) as f:
        for line in train_data:
            f.write(json.dumps(line) + "\n")

    with open(
        os.path.join(dir, "gpu_vm_files", "train_job_files", "valid_data.jsonl"), "w"
    ) as f:
        for line in valid_data:
            f.write(json.dumps(line) + "\n")

    # user config
    with open(
        os.path.join(dir, "gpu_vm_files", "train_job_files", "user_config.json"), "w"
    ) as f:
        json.dump(
            {
                "user_id": user_id,
                "router_name": router_name,
                "dataset": dataset,
                "endpoints": endpoints,
            },
            f,
        )

    # For now: uses a vm that's already created + nvidia installed...
    # vm_data = create_vm()

    project_id = "saas-368716"
    zone = "europe-west1-b"
    instance_name = "router-training-gpu1"

    # start the instance
    command = f"""gcloud compute instances start {instance_name} --project={project_id} --zone={zone}"""
    subprocess.run(command, shell=True)

    # prune old docker containers
    docker_prune_cmd = "docker container prune -f"
    command = f"""gcloud compute ssh {instance_name} --project={project_id} --zone={zone} --command="{docker_prune_cmd}" """
    subprocess.run(command, shell=True)

    # check if nvidia is installed
    command = f"""gcloud compute ssh {instance_name} --project={project_id} --zone={zone} --command="sudo /opt/deeplearning/install-driver.sh" """
    # subprocess.run(command, shell=True)

    command = f"""gcloud compute scp --project={project_id} --zone={zone} --recurse ./gpu_vm_files/* {instance_name}:~/"""
    subprocess.run(command, shell=True)

    # build & run the docker container

    docker_build_cmd = "docker build -t router_training ."
    command = f"""gcloud compute ssh {instance_name} --project={project_id} --zone={zone} --command="{docker_build_cmd}" """
    subprocess.run(command, shell=True)

    docker_run_cmd = "docker run -d --gpus all -it router_training"
    command = f"""gcloud compute ssh {instance_name} --project={project_id} --zone={zone} --command="{docker_run_cmd}" """
    subprocess.run(command, shell=True)


if __name__ == "__main__":
    # Create the parser
    parser = argparse.ArgumentParser(description="Train Router Script")

    # Define the arguments
    parser.add_argument("--user_id", type=str, required=True, help="User ID")
    parser.add_argument("--api_key", type=str, required=True, help="User API KEY")
    parser.add_argument("--router_name", type=str, required=True, help="Router Name")
    parser.add_argument("--dataset", type=str, required=True, help="Dataset Name")
    parser.add_argument(
        "--endpoints",
        type=str,
        required=True,
        help="List of endpoints",
    )
    parser.add_argument(
        "--orchestra_url",
        type=str,
        required=True,
        help="Orchestra URL",
    )

    # Parse the arguments
    args = parser.parse_args()

    # Access the arguments
    user_id = args.user_id
    api_key = args.api_key
    router_name = args.router_name
    dataset = args.dataset
    endpoints = args.endpoints
    orchestra_url = args.orchestra_url

    endpoints = endpoints.split(",")

    main(
        user_id=user_id,
        api_key=api_key,
        router_name=router_name,
        dataset=dataset,
        endpoints=endpoints,
        orchestra_url=orchestra_url,
    )
