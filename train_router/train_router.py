import json

from dataclasses import dataclass

import google.cloud.compute_v1 as compute_v1
from google.api_core.extended_operation import ExtendedOperation
import re
import os
import sys
import subprocess
from typing import Any
import warnings


@dataclass
class TrainRequest:
    train_cfg: dict
    bucket_path: str


def wait_for_extended_operation(
    operation: ExtendedOperation, verbose_name: str = "operation", timeout: int = 300
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
        )
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


def main(msg):
    # train_req = TrainRequest(**json.loads(msg))

    # create a vm
    # vm_data = create_vm()

    # scp the train data over
    # scp the train config over

    project_id = "saas-368716"
    zone = "europe-west1-b"
    instance_name = "router-training-gpu1"

    # start the instance
    command = f"""gcloud compute instances start {instance_name} --project={project_id} --zone={zone}"""
    subprocess.run(command, shell=True)

    # check if nvidia is installed...

    command = f"""gcloud compute ssh {instance_name} --project={project_id} --zone={zone} --command="sudo /opt/deeplearning/install-driver.sh" """
    # subprocess.run(command, shell=True)

    command = f"""gcloud compute scp --project={project_id} --zone={zone} --recurse ./gpu_vm_files/* {instance_name}:~/"""
    subprocess.run(command, shell=True)

    command = f"""gcloud compute scp --project={project_id} --zone={zone} --recurse ./router/ {instance_name}:~/"""
    # subprocess.run(command, shell=True)

    # scp the docker image over
    command = f"""gcloud compute scp --project={project_id} --zone={zone} ./Dockerfile {instance_name}:~/"""
    # subprocess.run(command, shell=True)

    # build & run the docker container

    docker_build_cmd = "docker build -t router_training ."
    command = f"""gcloud compute ssh {instance_name} --project={project_id} --zone={zone} --command="{docker_build_cmd}" """
    subprocess.run(command, shell=True)

    docker_run_cmd = "docker run -d -it --name train router_training"
    command = f"""gcloud compute ssh {instance_name} --project={project_id} --zone={zone} --command="{docker_run_cmd}" """
    subprocess.run(command, shell=True)


if __name__ == "__main__":
    ret = main(1)
    print(ret)
