import json
import os
import shutil
from functools import reduce, wraps
from typing import Callable, Dict, List, Tuple, Union

import redis
import requests
from fastapi import HTTPException

from orchestra.web.api.utils.http_responses import evaluation_does_not_exist

shared_volume = os.environ.get("SHARED_VOLUME")


class OnPremModel:
    def __init__(self, model_class: object, table_name: str, id_field: str = "id"):
        self.model_class = model_class
        self.table_name = table_name
        self.shared_volume = shared_volume
        self.json_path = os.path.join(self.shared_volume, "db", f"{table_name}.json")
        self.id_field = id_field
        if not os.path.exists(self.json_path):
            os.makedirs(os.path.join(self.shared_volume, "db"), exist_ok=True)
            with open(self.json_path, "w") as f:
                json.dump({"data": []}, f)

    def create(self, **data):
        with open(self.json_path) as f:
            entries = json.load(f)["data"]
        id = (entries[-1][self.id_field] + 1) if len(entries) else 0
        data[self.id_field] = id
        with open(self.json_path, "w") as f:
            json.dump({"data": [*entries, data]}, f, indent=4)

    def read(
        self,
        filters: Dict[str, Dict[str, Union[str, int]]],
        join_table: str = None,
        join_columns: Tuple[str, str] = None,
        select_columns: Dict[str, List[str]] = None,
        return_raw: bool = False,
    ):
        # getting the entries for the primary table
        with open(self.json_path) as f:
            entries = json.load(f)["data"]

        # getting the entries for the joined table if required
        # the entries for the joined table would be corresponding
        # to those in the primary table
        final_entries = {self.table_name: entries}
        if join_table and join_columns:
            json_path = os.path.join(self.shared_volume, "db", f"{join_table}.json")
            if os.path.exists(json_path):
                with open(
                    os.path.join(self.shared_volume, "db", f"{join_table}.json"),
                ) as f:
                    join_entries = json.load(f)["data"]
                    if join_entries:
                        join_entries = reduce(
                            lambda x, y: {**x, **y},
                            [{entry[join_columns[1]]: entry} for entry in join_entries],
                        )
                    else:
                        join_entries = dict()
            else:
                join_entries = dict()
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
                    if filters[table_name][field] is not None:
                        if isinstance(filters[table_name][field], (str, int)):
                            check = (
                                filters[table_name][field]
                                == final_entries[table_name][i][field]
                            )
                        else:
                            check = filters[table_name][field](
                                final_entries[table_name][i][field],
                            )
                        if not check:
                            [entry.pop(i) for entry in final_entries.values()]
                            filtered = True
                            break
                if filtered:
                    break

        # we do have select columns almost always when doing joins, so don't need to deal with joins here
        if not select_columns:
            return [
                self.model_class(**entry) if not return_raw else entry
                for entry in final_entries[self.table_name]
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

    def update(
        self,
        filters: Dict[str, Dict[str, Union[str, int]]],
        updates: Dict[str, Union[str, int]],
    ):
        with open(self.json_path) as f:
            entries = json.load(f)["data"]
        relevant_entry = self.read(filters, return_raw=True)[0]
        entries = [
            entry
            for entry in entries
            if entry[self.id_field] != relevant_entry[self.id_field]
        ]
        for field, value in updates.items():
            relevant_entry[field] = value
        with open(self.json_path, "w") as f:
            json.dump({"data": [*entries, relevant_entry]}, f, indent=4)

    def delete(self, filters: Dict[str, Dict[str, Union[str, int]]]):
        with open(self.json_path) as f:
            entries = json.load(f)["data"]
        relevant_entry = self.read(filters, return_raw=True)[0]
        entries = [
            entry
            for entry in entries
            if entry[self.id_field] != relevant_entry[self.id_field]
        ]
        with open(self.json_path, "w") as f:
            json.dump({"data": entries}, f, indent=4)


def send_pubsub_msg(topic: str, msg: Dict[str, str]) -> None:
    r = redis.Redis(host=os.environ.get("REDIS_HOST", "host.docker.internal"))
    r.publish(topic.split("/")[-1] + "-sub", json.dumps(msg).encode())


def file_exists(bucket_name: str, file_name: str) -> bool:
    file_path = os.path.join(shared_volume, bucket_name, file_name)
    return os.path.exists(file_path)


def get_scores(user_id: str, dataset: str):
    bucket_name = "uploaded_datasets"
    file_path = os.path.join(
        shared_volume,
        bucket_name,
        user_id,
        dataset,
        "0",
        "scores.json",
    )
    if os.path.exists(file_path):
        with open(file_path, "rb") as f:
            json_data = f.read()
        return json.loads(json_data.decode("utf-8"))
    return evaluation_does_not_exist(dataset)


def get_input_tokens(user_id: str, dataset: str):
    bucket_name = "uploaded_datasets"
    file_path = os.path.join(
        shared_volume,
        bucket_name,
        user_id,
        dataset,
        "0",
        "num_tokens.json",
    )
    if os.path.exists(file_path):
        with open(file_path, "rb") as f:
            json_data = f.read()
        return json.loads(json_data.decode("utf-8"))["num_tokens"]
    return 1


def get_response_tokens(user_id: str, dataset: str, endpoint: str):
    bucket_name = "uploaded_datasets"
    file_path = os.path.join(
        shared_volume,
        bucket_name,
        user_id,
        dataset,
        "0",
        endpoint,
        "num_tokens_in_response.json",
    )
    if os.path.exists(file_path):
        with open(file_path, "rb") as f:
            json_data = f.read()
        return json.loads(json_data.decode("utf-8"))["num_tokens"]
    return 1


def dir_exists(bucket_name: str, dir_name: str) -> bool:
    dir_path = os.path.join(shared_volume, bucket_name, dir_name)
    return len(os.listdir(dir_path)) > 0


def delete(bucket_name: str, dir_name: str) -> None:
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


def read_from_folder(
    bucket_name: str,
    file_name: str,
    raw: bool = False,
    decode: bool = False,
):
    file_path = os.path.join(shared_volume, bucket_name, file_name.strip("/"))
    with open(file_path, "rb") as f:
        data = f.read()
    if raw:
        if decode:
            return data.decode("utf-8")
        return data
    return json.loads(data.decode("utf-8"))


def write_to_folder(data: Union[str, Dict[str, str]], bucket_name: str, file_name: str):
    file_path = os.path.join(shared_volume, bucket_name, file_name)
    os.makedirs(os.sep.join(file_path.split(os.sep)[:-1]), exist_ok=True)
    with open(file_path, "wb") as f:
        f.write(str(data).encode("utf-8"))


def handle_on_prem(endpoint: str, method: str):
    def decorator(fn: Callable):
        @wraps(fn)
        def wrapped_function(*args, **kwargs):
            non_dao_kwargs = {
                key: value
                for key, value in kwargs.items()
                if "DAO" not in value.__class__.__name__ and key != "request_fastapi"
            }
            headers = (
                dict()
                if "request_fastapi" not in kwargs
                else dict(kwargs["request_fastapi"]._headers)
            )
            headers = {
                key: value
                for key, value in headers.items()
                if key in ["content-type", "authorization"]
            }
            request_url = os.environ.get("PUBLIC_ORCHESTRA_URL", "") + endpoint
            if os.environ.get("ON_PREM"):
                if len(non_dao_kwargs.keys()) == 0:
                    non_dao_kwargs = None
                if method == "get":
                    return requests.get(
                        request_url,
                        params=non_dao_kwargs,
                        headers=headers,
                    ).json()
                elif method == "post":
                    return requests.post(
                        request_url,
                        params=non_dao_kwargs,
                        headers=headers,
                    ).json()
                else:
                    raise HTTPException(
                        status_code=404,
                        detail="This endpoint is not available in an on-prem setup.",
                    )
            return fn(*args, **kwargs)

        return wrapped_function

    return decorator


def internal_id_to_displayname(user_id):
    bucket_name = "uploaded_datasets"
    dir_path = os.path.join(shared_volume, bucket_name, user_id)
    id_to_displayname = {}
    blobs = []
    for root, _, files in os.walk(dir_path):
        for file in files:
            blobs.append(os.path.join(root, file))
    for blob in blobs:
        if not blob.endswith("metadata.json"):
            continue
        internal_id = blob.split("/")[-2]
        with open(blob, "rb") as f:
            display_name = json.loads(f.read().decode("utf-8"))["display_name"]
        id_to_displayname[internal_id] = display_name

    return id_to_displayname
