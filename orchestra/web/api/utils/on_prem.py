import json
import os
import shutil
from typing import Dict

import redis

shared_volume = os.environ.get("SHARED_VOLUME")


def send_pubsub_msg(topic: str, msg: Dict[str, str]) -> None:
    r = redis.Redis()
    r.publish(topic.split("/")[-1] + "-sub", json.dumps(msg).encode())


def file_exists(bucket_name: str, file_name: str) -> bool:
    file_path = os.path.join(shared_volume, bucket_name, file_name)
    return os.path.exists(file_path)


def dir_exists(bucket_name: str, dir_name: str) -> bool:
    dir_path = os.path.join(shared_volume, bucket_name, dir_name)
    return len(os.listdir(dir_path)) > 0


def delete_dir(bucket_name: str, dir_name: str) -> None:
    dir_path = os.path.join(shared_volume, bucket_name, dir_name)
    shutil.rmtree(dir_path)


def list_dir(bucket_name: str, prefix: str):
    dir_path = os.path.join(shared_volume, bucket_name, prefix)
    return os.listdir(dir_path)


def read_json_from_folder(bucket_name: str, file_name: str):
    file_path = os.path.join(shared_volume, bucket_name, file_name)
    with open(file_path) as f:
        return json.loads(f)


def write_json_to_folder(json_data: Dict[str, str], bucket_name: str, file_name: str):
    file_path = os.path.join(shared_volume, bucket_name, file_name)
    os.makedirs(os.sep.join(file_path.split(os.sep)[:-1]), exist_ok=True)
    with open(file_path, "wb") as f:
        f.write(json_data)
