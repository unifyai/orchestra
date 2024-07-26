import json
import os
import shutil
from functools import reduce
from typing import Dict, List, Tuple, Union

import redis

shared_volume = os.environ.get("SHARED_VOLUME")


class OnPremModel:
    def __init__(self, model_class: object, table_name: str):
        self.model_class = model_class
        self.table_name = table_name
        self.shared_volume = shared_volume
        self.json_path = os.path.join(self.shared_volume, "db", f"{table_name}.json")
        if not os.path.exists(self.json_path):
            os.makedirs(os.path.join(self.shared_volume, "db"), exist_ok=True)
            with open(self.json_path, "w") as f:
                json.dump({"data": []}, f)

    def create(self, **data):
        with open(self.json_path) as f:
            entries = json.load(f)["data"]
        id = (entries[-1]["id"] + 1) if len(entries) else 0
        data["id"] = id
        with open(self.json_path, "w") as f:
            json.dump({"data": [*entries, data]}, f)

    def read(
        self,
        filters: Dict[str, Dict[str, Union[str, int]]],
        join_table: str = None,
        join_columns: Tuple[str, str] = None,
        select_columns: Dict[str, List[str]] = None,
    ):
        # getting the entries for the primary table
        with open(self.json_path) as f:
            entries = json.load(f)["data"]

        # getting the entries for the joined table if required
        # the entries for the joined table would be corresponding
        # to those in the primary table
        final_entries = {self.table_name: entries}
        if join_table and join_columns:
            with open(
                os.path.join(self.shared_volume, "db", f"{join_table}.json"),
            ) as f:
                join_entries = json.load(f)["data"]
                join_entries = reduce(
                    lambda x, y: {**x, **y},
                    [{entry[join_columns[1]]: entry} for entry in join_entries],
                )
            final_entries[join_table] = [
                join_entries[entry[join_columns[0]]]
                for entry in final_entries[self.table_name]
            ]

        # filter the entries based on the filter columns
        filtered = False
        for i in range(len(final_entries[self.table_name]) - 1, -1, -1):
            filtered = False
            for table_name in filters:
                fields = list(filters[table_name].keys())
                for field in fields:
                    if (
                        filters[table_name][field] is not None
                        and filters[table_name][field]
                        != final_entries[table_name][i][field]
                    ):
                        [entry.pop(i) for entry in final_entries.values()]
                        filtered = True
                        break
                if filtered:
                    break

        # we do have select columns almost always when doing joins, so don't need to deal with joins here
        if not select_columns:
            return [
                self.model_class(**entry) for entry in final_entries[self.table_name]
            ]

        # select needed columns
        final_results = []
        for i in range(len(final_entries[self.table_name])):
            result = {}
            for table_name in select_columns:
                for column in select_columns[table_name]:
                    result[column] = final_entries[table_name][i][column]
            final_results.append(result)

        return final_results


def send_pubsub_msg(topic: str, msg: Dict[str, str]) -> None:
    r = redis.Redis(host=os.environ.get("REDIS_HOST"))
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
    blobs = []
    for root, _, files in os.walk(dir_path):
        for file in files:
            blobs.append(
                os.path.join(root, file).replace(
                    os.path.join(shared_volume, bucket_name),
                    "",
                ),
            )
    return blobs


def read_json_from_folder(bucket_name: str, file_name: str, raw: bool = False):
    file_path = os.path.join(shared_volume, bucket_name, file_name)
    with open(file_path, "rb") as f:
        json_data = f.read()
    if raw:
        return json_data
    return json.loads(json_data.decode("utf-8"))


def write_json_to_folder(json_data: Dict[str, str], bucket_name: str, file_name: str):
    file_path = os.path.join(shared_volume, bucket_name, file_name)
    os.makedirs(os.sep.join(file_path.split(os.sep)[:-1]), exist_ok=True)
    with open(file_path, "wb") as f:
        f.write(json_data)
