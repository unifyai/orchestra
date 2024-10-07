import json
import logging
import math
import os
import random
import shutil
import subprocess
import sys
from typing import Any

# import google.cloud.compute_v1 as compute_v1
from google.api_core.extended_operation import ExtendedOperation
from httpx import AsyncClient, Limits

logging.basicConfig(filename="router_training.log", level=logging.INFO)


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


async def create_train_data(
    user_id,
    api_key,
    admin_key,
    datum_ids,
    endpoints,
    evaluator,
    client,
):
    # download all the prompts with datum_ids ...
    # download all the scores with datum_ids ... per endpoint per evaluator
    # combine

    ## getting

    url = "/v0/get_prompts"

    params = {"datum_ids": ",".join(str(p) for p in datum_ids), "user_id": user_id}

    HEADERS = {
        "accept": "application/json",
        "Authorization": f"Bearer {admin_key}",
        "Content-Type": "application/json",
    }
    response = await client.get(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    prompts = response.json()
    id_to_prompt = {e["id"]: json.loads(e["messages"])[0]["content"] for e in prompts}

    id_model_to_score = {}
    for endpoint in endpoints:
        try:
            url = "/v0/evaluation"
            HEADERS = {
                "accept": "application/json",
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            params = {
                "prompts": ",".join(str(i) for i in datum_ids),
                "endpoint": endpoint,
                "evaluator": evaluator,
                "user_id": user_id,
                "per_prompt": True,
            }
            response = await client.get(url, params=params, headers=HEADERS)
            endpoint_scores = response.json()
            endpoint_scores = endpoint_scores[evaluator][endpoint]["per_prompt"]
            for entry in endpoint_scores:
                _id = entry["id"]
                id_model_to_score[(_id, endpoint)] = entry["score"]
        except Exception as e:
            print(e)

    combined_files = []
    for id_ in id_to_prompt:
        scores_per_endpoint = {}
        for e in endpoints:
            try:
                scores_per_endpoint[e] = id_model_to_score[(id_, e)]
            except:
                scores_per_endpoint[e] = 0  # TODO: what to do here instead
        combined_files.append(
            {"id_": id_, "prompt": id_to_prompt[id_], "scores": scores_per_endpoint},
        )

    n = len(combined_files)
    random.shuffle(combined_files)
    train_frac = 0.8
    valid_num = math.floor((1 - train_frac) * n)

    train_data = combined_files[valid_num:]
    valid_data = combined_files[:valid_num]
    return train_data, valid_data


async def train_router(msg, save_dir=None, client=None):

    msg = json.loads(msg)
    user_id = msg["user_id"]
    # user_email = msg["user_email"]
    api_key = msg["api_key"]
    admin_key = msg["admin_key"]
    datum_ids = msg["datum_ids"]
    router_id = msg["router_id"]
    endpoints = msg["endpoints"]
    evaluator = msg["evaluator"]
    orchestra_url = msg["orchestra_url"]

    if client is None:
        limits = Limits(
            max_keepalive_connections=None,
            max_connections=None,
            keepalive_expiry=30,
        )
        client = AsyncClient(base_url=orchestra_url, limits=limits, timeout=60)

    logging.info("starting TRAINING")
    # TODO: retrieve correct evaluation data

    train_data, valid_data = await create_train_data(
        user_id,
        api_key,
        admin_key,
        datum_ids,
        endpoints,
        evaluator,
        client,
    )

    unrolled_data = []
    for td in train_data:
        for e, score in td["scores"].items():
            unrolled_data.append(
                {
                    "id_": td["id_"],
                    "prompt": td["prompt"],
                    "model_provider": e,
                    "score": score,
                },
            )
    train_data = unrolled_data

    # start
    import random

    run_name = f"{user_id}_{router_id}_{random.randint(10000,99999)}"
    _dir = os.path.dirname(os.path.abspath(__file__))
    run_folder = os.path.join(_dir, "save_files", run_name)
    train_job_files_folder = os.path.join(run_folder, "train_job_files")

    os.makedirs(run_folder, exist_ok=True)

    shutil.copytree(
        os.path.join(_dir, "reference_gpu_vm_files"),
        run_folder,
        dirs_exist_ok=True,
    )

    with open(os.path.join(train_job_files_folder, "train_data.jsonl"), "w") as f:
        for line in train_data:
            f.write(json.dumps(line) + "\n")

    with open(os.path.join(train_job_files_folder, "valid_data.jsonl"), "w") as f:
        for line in valid_data:
            f.write(json.dumps(line) + "\n")

    # user config
    with open(os.path.join(train_job_files_folder, "user_config.json"), "w") as f:
        json.dump(
            {
                "user_id": user_id,
                "router_id": router_id,
                "evaluator": evaluator,
                "endpoints": endpoints,
            },
            f,
        )

    # For now: uses a vm that's already created + nvidia installed...
    # vm_data = create_vm()

    gcp_config = {
        "project_id": "saas-368716",
        "zone": "europe-west1-b",
        "instance_name": "router-training-gpu1",
    }

    project_id = gcp_config["project_id"]
    zone = gcp_config["zone"]
    instance_name = gcp_config["instance_name"]

    # start the instance
    logging.info("starting gpu")
    command = f"""gcloud compute instances start {instance_name} --project={project_id} --zone={zone}"""
    subprocess.run(command, shell=True)

    # prune old docker containers
    logging.info("pruning docker")
    docker_prune_cmd = "docker system prune -f"
    command = f"""gcloud compute ssh {instance_name} --project={project_id} --zone={zone} --command="{docker_prune_cmd}" """
    subprocess.run(command, shell=True)

    # check if nvidia is installed
    # command = f"""gcloud compute ssh {instance_name} --project={project_id} --zone={zone} --command="sudo /opt/deeplearning/install-driver.sh" """
    # subprocess.run(command, shell=True)

    logging.info("move files")
    command = f"""gcloud compute scp --project={project_id} --zone={zone} --recurse {run_folder} {instance_name}:~/{run_name}/"""
    subprocess.run(command, shell=True)

    # build & run the docker container
    logging.info("building docker container")
    docker_build_cmd = f"docker build -t router_training ~/{run_name}"
    command = f"""gcloud compute ssh {instance_name} --project={project_id} --zone={zone} --command="{docker_build_cmd}" """
    subprocess.run(command, shell=True)

    logging.info("running docker container")
    docker_run_cmd = f"docker run --env ORCHESTRA_BASE_URL='{orchestra_url}' --env ORCHESTRA_ADMIN_KEY='{admin_key}' -d --gpus all -it router_training"
    command = f"""gcloud compute ssh {instance_name} --project={project_id} --zone={zone} --command="{docker_run_cmd}" """
    subprocess.run(command, shell=True)


if __name__ == "__main__":
    msg = sys.argv[1]
    args = json.loads(args)

    train_router(msg)
