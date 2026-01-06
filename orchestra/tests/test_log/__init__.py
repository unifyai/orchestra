import base64
import json
import os
from datetime import datetime, timezone

import cv2
from httpx import AsyncClient, Request

api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))
api_key_second_user = "2nd_api_key"

HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {api_key}",
}

HEADERS_2 = {
    "accept": "application/json",
    "Authorization": f"Bearer {api_key_second_user}",
}

log_data = {
    "logs_for_group_threshold": [
        {
            "shared_string": "common value",
            "unique_string": "value1",
            "shared_number": 42,
            "unique_number": 1,
            "shared_object": {"key": "value"},
            "mixed_field": "appears twice",
        },
        {
            "shared_string": "common value",
            "unique_string": "value2",
            "shared_number": 42,
            "unique_number": 2,
            "shared_object": {"key": "value"},
            "mixed_field": "appears twice",
        },
        {
            "shared_string": "common value",
            "unique_string": "value3",
            "shared_number": 42,
            "unique_number": 3,
            "shared_object": {"key": "value"},
            "mixed_field": "unique value",
        },
        {
            "shared_string": "common value",
            "unique_string": "value4",
            "shared_number": 42,
            "unique_number": 4,
            "shared_object": {"key": "value"},
            "mixed_field": "another unique",
        },
    ],
    "log": {
        "a/b/c/input": "Some input data",
        "a/b/c/boolean_input": True,
        "a/b/c/numeric_input": 4.5,
    },
    "log_update": {
        "my_list": ["a", "b", "c"],
        "my_dict": {"a": 1, "b": 2, "c": 3},
    },
    "log_update_w_overwrite": {
        "a/b/c/boolean_input": False,
        "a/b/c/numeric_input": 5.4,
    },
    "logs_for_grouping": [
        {
            "a/input": "What is 1 + 1?",
            "system_prompt": "You are an expert mathematician.",
        },
        {
            "a/input": "What is 2 + 2?",
            "system_prompt": "You are an expert mathematician.",
        },
        {
            "a/input": "What is 1 + 1?",
            "system_prompt": "Respond only with a single digit.",
        },
        {
            "input": "What is 2 + 2?",
            "system_prompt": "Respond only with a single digit.",
        },
    ],
    "logs_for_various": [
        {
            "_/description": "boiling water",
            "_/temperature": 100.0,
            "_/state": "liquid->gas",
            "_/safe": False,
            "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
            "explicit_types": {
                "_/description": {"type": "str"},
                "_/temperature": {"type": "float"},
                "_/state": {"type": "str"},
                "_/safe": {"type": "bool"},
                "_/timestamp": {"type": "datetime"},
            },
        },
        {
            "_/description": "freezing water",
            "_/temperature": 0.0,
            "_/state": "liquid->solid",
            "_/safe": True,
            "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
            "explicit_types": {
                "_/description": {"type": "str"},
                "_/temperature": {"type": "float"},
                "_/state": {"type": "str"},
                "_/safe": {"type": "bool"},
                "_/timestamp": {"type": "datetime"},
            },
        },
        {
            "_/description": "surface of the sun",
            "_/temperature": 6000.0,
            "_/state": "gas",
            "_/safe": False,
            "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
            "explicit_types": {
                "_/description": {"type": "str"},
                "_/temperature": {"type": "float"},
                "_/state": {"type": "str"},
                "_/safe": {"type": "bool"},
                "_/timestamp": {"type": "datetime"},
            },
        },
        {
            "_/description": "freezing nitrogen",
            "_/temperature": -210.0,
            "_/state": "liquid->solid",
            "_/safe": False,
            "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
            "explicit_types": {
                "_/description": {"type": "str"},
                "_/temperature": {"type": "float"},
                "_/state": {"type": "str"},
                "_/safe": {"type": "bool"},
                "_/timestamp": {"type": "datetime"},
            },
        },
        {
            "_/description": "lava",
            "_/metadata": [1, 5, 6],
            "_/_data": {"a": 2, "b": 4},
            "_/timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
            "explicit_types": {
                "_/description": {"type": "str"},
                "_/temperature": {"type": "float"},
                "_/state": {"type": "str"},
                "_/safe": {"type": "bool"},
                "_/timestamp": {"type": "datetime"},
                "_/_data": {"type": "dict"},
                "_/metadata": {"type": "list"},
            },
        },
        {
            "_/description": "air",
            "_/metadata": [3, 8, 5],
            "_/_data": {"a": 6, "b": 12, "c": 8, "d": 11},
            "_/timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
            "explicit_types": {
                "_/timestamp": {
                    "type": "datetime",
                },
                "_/_data": {"type": "dict"},
                "_/metadata": {"type": "list"},
            },
        },
        {
            "_/_data": {"a": 8, "b": 10},
            "_/timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
            "explicit_types": {
                "_/_data": {"type": "dict"},
                "_/timestamp": {"type": "datetime"},
                "_/_data": {"type": "dict"},
            },
        },
    ],
}


def _create_log(
    client,
    project_name,
    user=1,
    params=None,
    entries=None,
    context=None,
):
    _headers = HEADERS if user == 1 else HEADERS_2
    if entries is None:
        entries = log_data["log"]
    if params is None:
        params = {"a/b/param1": "test"}

    # Handle both single dict and list of dicts for entries
    if isinstance(entries, dict):
        # set all entries to be mutable (backwards compatibility)
        if "explicit_types" not in entries:
            explicit_types_entries = {k: {"mutable": True} for k in entries.keys()}
            entries["explicit_types"] = explicit_types_entries
        else:
            # Preserve existing explicit_types but ensure mutable=True for backward compatibility
            for k in entries.keys():
                if k != "explicit_types" and k not in entries["explicit_types"]:
                    entries["explicit_types"][k] = {"mutable": True}
                elif (
                    k != "explicit_types"
                    and "mutable" not in entries["explicit_types"][k]
                ):
                    entries["explicit_types"][k]["mutable"] = True
    elif isinstance(entries, list):
        # Handle list of entries
        for entry in entries:
            if "explicit_types" not in entry:
                explicit_types_entries = {k: {"mutable": True} for k in entry.keys()}
                entry["explicit_types"] = explicit_types_entries
            else:
                # Preserve existing explicit_types but ensure mutable=True for backward compatibility
                for k in entry.keys():
                    if k != "explicit_types" and k not in entry["explicit_types"]:
                        entry["explicit_types"][k] = {"mutable": True}
                    elif (
                        k != "explicit_types"
                        and "mutable" not in entry["explicit_types"][k]
                    ):
                        entry["explicit_types"][k]["mutable"] = True

    # Handle both single dict and list of dicts for params
    if isinstance(params, dict):
        # set all params to be mutable (backwards compatibility)
        if "explicit_types" not in params:
            explicit_types_params = {k: {"mutable": True} for k in params.keys()}
            params["explicit_types"] = explicit_types_params
        else:
            # Preserve existing explicit_types but ensure mutable=True for backward compatibility
            for k in params.keys():
                if k != "explicit_types" and k not in params["explicit_types"]:
                    params["explicit_types"][k] = {"mutable": True}
                elif (
                    k != "explicit_types"
                    and "mutable" not in params["explicit_types"][k]
                ):
                    params["explicit_types"][k]["mutable"] = True
    elif isinstance(params, list):
        # Handle list of params
        for param in params:
            if "explicit_types" not in param:
                explicit_types_params = {k: {"mutable": True} for k in param.keys()}
                param["explicit_types"] = explicit_types_params
            else:
                # Preserve existing explicit_types but ensure mutable=True for backward compatibility
                for k in param.keys():
                    if k != "explicit_types" and k not in param["explicit_types"]:
                        param["explicit_types"][k] = {"mutable": True}
                    elif (
                        k != "explicit_types"
                        and "mutable" not in param["explicit_types"][k]
                    ):
                        param["explicit_types"][k]["mutable"] = True

    return client.post(
        "/v0/logs",
        json={
            "project_name": project_name,
            "params": params,
            "entries": entries,
            "context": context,
        },
        headers=_headers,
    )


async def _create_image_log(
    client: AsyncClient,
    project_name: str,
    context_name: str,
    image_path: str,
    additional_entries: dict = None,
    *,
    image_col_name: str = "img",
):
    """
    Helper function to create a log entry with an image.

    Args:
        client: AsyncClient for making HTTP requests
        project_name: Name of the project
        context_name: Name of the context
        image_path: Path to the image file (relative to sample_datasets)
        additional_entries: Additional entries to include in the log

    Returns:
        Response from the log creation API
    """
    # Construct the full path to the image file
    full_img_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
        "sample_datasets",
        image_path,
    )

    # Read and encode the image like in test_create_log_w_image
    success, buffer = cv2.imencode(".png", cv2.imread(full_img_path))
    assert success, f"Failed to encode image at {full_img_path}"
    img_raw = base64.b64encode(buffer).decode("utf-8")

    entries = {
        image_col_name: f"data:image/png;base64,{img_raw}",
    }
    if additional_entries:
        entries.update(additional_entries)

    result = await _create_log(
        client,
        project_name,
        entries=entries,
        context=context_name,
    )
    return result


def _create_derived_entry(
    client,
    project_name,
    key,
    equation,
    referenced_logs,
    context=None,
    user=1,
):
    _headers = HEADERS if user == 1 else HEADERS_2
    return client.post(
        "/v0/logs/derived",
        json={
            "project_name": project_name,
            "key": key,
            "equation": equation,
            "referenced_logs": referenced_logs,
            "context": context,
        },
        headers=_headers,
    )


async def fetch_logs(client, project_name, **query_params):
    default_params = {
        "project_name": project_name,
        "sorting": json.dumps({"id": "ascending"}),
    }
    default_params.update(query_params)
    resp = await client.get("/v0/logs", params=default_params, headers=HEADERS)
    assert resp.status_code == 200, resp.text
    return resp.json()["logs"]


def _get_log(client, project_name, log_id, user=1):
    _headers = HEADERS if user == 1 else HEADERS_2
    return client.get(
        f"/v0/logs?project={project_name}",
        params={"from_ids": f"{log_id}"},
        headers=_headers,
    )


def _update_multiple_logs_w_overwrite(client, log_ids, overwrite, user=1):
    _headers = HEADERS if user == 1 else HEADERS_2
    return client.put(
        f"/v0/logs",
        json={
            "logs": log_ids,
            "entries": log_data["log_update_w_overwrite"],
            "overwrite": overwrite,
        },
        headers=_headers,
    )


# Helper function to delete multiple logs
def _delete_logs(
    client,
    log_ids,
    user=1,
    source_type=None,
    project_name=None,
    context=None,
    delete_empty_fields=False,
    delete_empty_logs=False,
):
    _headers = HEADERS if user == 1 else HEADERS_2
    json_data = {"ids_and_fields": log_ids}
    if source_type:
        json_data["source_type"] = source_type
    if project_name:
        json_data["project"] = project_name
    if context:
        json_data["context"] = context
    if delete_empty_fields:
        json_data["delete_empty_fields"] = delete_empty_fields
    if delete_empty_logs:
        json_data["delete_empty_logs"] = delete_empty_logs
    request = Request(
        "DELETE",
        str(client.base_url) + "/v0/logs",
        json=json_data,
        headers=_headers,
    )
    return client.send(request)


def _update_logs(client, log_ids, entries, user=1, context=None, overwrite=False):
    _headers = HEADERS if user == 1 else HEADERS_2
    return client.put(
        "/v0/logs",
        json={
            "logs": log_ids,
            "entries": entries,
            "overwrite": overwrite,
            "context": context,
        },
        headers=_headers,
    )


def _delete_log_fields_from_logs(
    client,
    fields,
    delete_empty_logs=False,
    delete_empty_fields=True,
    user=1,
    project_name=None,
):
    _headers = HEADERS if user == 1 else HEADERS_2
    request = Request(
        "DELETE",
        str(client.base_url) + f"/v0/logs",
        json={
            "ids_and_fields": fields,
            "project_name": project_name,
            "delete_empty_logs": delete_empty_logs,
            "delete_empty_fields": delete_empty_fields,
        },
        headers=_headers,
    )
    return client.send(request)


async def _create_logs_for_grouping(client, project_name, user=1):
    _headers = HEADERS if user == 1 else HEADERS_2
    data = log_data["logs_for_grouping"]
    for i in range(len(data)):
        # Split into params and entries
        entries = {}
        if "a/input" in data[i]:
            entries["a/input"] = data[i]["a/input"]
        elif "input" in data[i]:
            entries["a/input"] = data[i]["input"]

        response = await _create_log(
            client,
            project_name,
            params={"system_prompt": data[i]["system_prompt"]},
            entries=entries,
        )
        assert response.status_code == 200, response.json()


async def _create_logs_for_grouping_entries(client, project_name, user=1):
    """Entry-based version for dual-mode testing (no params)."""
    _headers = HEADERS if user == 1 else HEADERS_2
    data = log_data["logs_for_grouping"]
    for i in range(len(data)):
        # Put system_prompt in entries instead of params
        entries = {"system_prompt": data[i]["system_prompt"]}
        if "a/input" in data[i]:
            entries["a/input"] = data[i]["a/input"]
        elif "input" in data[i]:
            entries["a/input"] = data[i]["input"]

        response = await _create_log(
            client,
            project_name,
            params={},
            entries=entries,
        )
        assert response.status_code == 200, response.json()


async def _create_logs_for_group_threshold(client, project_name, user=1):
    data = log_data["logs_for_group_threshold"]
    for i in range(len(data)):
        response = await _create_log(client, project_name, entries=data[i])
        assert response.status_code == 200, response.json()


async def _create_several_logs(
    client,
    project_name,
    context_name=None,
    user=1,
    batched=True,
):
    data = log_data["logs_for_various"]
    if batched:
        response = await _create_log(
            client,
            project_name,
            params={"a/b/param1": "test"},
            entries=data,
            context=(
                {"name": context_name, "description": "test context"}
                if context_name
                else None
            ),
        )
        assert response.status_code == 200, response.json()
    else:
        for i in range(len(data)):
            response = await _create_log(
                client,
                project_name,
                entries=data[i],
                params={"a/b/param1": f"test_{i}"},
            )
            assert response.status_code == 200, response.json()


def _create_project(client, project_name, user=1):
    _headers = HEADERS if user == 1 else HEADERS_2
    url = "/v0/project"
    project_data = {"name": project_name}
    return client.post(url, json=project_data, headers=_headers)
