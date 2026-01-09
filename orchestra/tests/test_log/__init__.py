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
        "a/b/param1": "test",
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

    # Merge params into entries for backwards compatibility
    # The params argument is deprecated but still accepted for existing tests
    if params is not None:
        if isinstance(entries, dict) and isinstance(params, dict):
            # Merge params into entries (entries takes precedence)
            merged = {**params}
            merged.update(entries)
            entries = merged
        elif isinstance(entries, list) and isinstance(params, list):
            # Merge each params dict into corresponding entries dict
            entries = [
                {**params[i], **entries[i]} if i < len(params) else entries[i]
                for i in range(len(entries))
            ]
        elif isinstance(entries, list) and isinstance(params, dict):
            # Apply same params to all entries
            entries = [{**params, **entry} for entry in entries]
        elif isinstance(entries, dict) and isinstance(params, list):
            # Apply entries to all params
            entries = [{**param, **entries} for param in params]

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

    return client.post(
        "/v0/logs",
        json={
            "project_name": project_name,
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
        f"/v0/logs?project_name={project_name}",
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
        json_data["project_name"] = project_name
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
        # Build entries dict
        entries = {}
        if "a/input" in data[i]:
            entries["a/input"] = data[i]["a/input"]
        elif "input" in data[i]:
            entries["a/input"] = data[i]["input"]

        entries["system_prompt"] = data[i]["system_prompt"]
        response = await _create_log(
            client,
            project_name,
            entries=entries,
        )
        assert response.status_code == 200, response.json()


async def _create_logs_for_grouping_entries(client, project_name, user=1):
    """Entry-based version for dual-mode testing."""
    _headers = HEADERS if user == 1 else HEADERS_2
    data = log_data["logs_for_grouping"]
    for i in range(len(data)):
        # Put all fields in entries
        entries = {"system_prompt": data[i]["system_prompt"]}
        if "a/input" in data[i]:
            entries["a/input"] = data[i]["a/input"]
        elif "input" in data[i]:
            entries["a/input"] = data[i]["input"]

        response = await _create_log(
            client,
            project_name,
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
            )
            assert response.status_code == 200, response.json()


def _create_project(client, project_name, user=1):
    _headers = HEADERS if user == 1 else HEADERS_2
    url = "/v0/project"
    project_data = {"name": project_name}
    return client.post(url, json=project_data, headers=_headers)


async def wait_for_gcs_images(
    client: AsyncClient,
    project_name: str,
    context_name: str,
    image_col_name: str = "img",
    max_retries: int = 12,
    base_delay: float = 1.0,
    max_delay: float = 10.0,
) -> None:
    """
    Wait for GCS images to become available after upload.

    GCS has eventual consistency, so images may not be immediately readable
    after upload. This helper polls until all images are accessible.

    Args:
        client: The test client
        project_name: Project containing the logs
        context_name: Context containing the logs
        image_col_name: Name of the image column (default: "img")
        max_retries: Maximum number of retry attempts (default: 12)
        base_delay: Initial delay in seconds (default: 1.0)
        max_delay: Maximum delay cap in seconds (default: 10.0)

    Raises:
        AssertionError: If images are not available after all retries,
                       includes diagnostic info for debugging.
    """
    import asyncio
    import logging

    from orchestra.services.bucket_service import BucketService

    bucket_service = BucketService()

    # Fetch all logs to get image URLs
    response = await client.get(
        f"/v0/logs?project_name={project_name}&context={context_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    all_logs = response.json()["logs"]

    # Track diagnostic info for debugging intermittent failures
    diagnostic_info: dict[str, dict] = {}
    total_wait_time = 0.0

    for attempt in range(max_retries):
        unavailable_images = []
        for log in all_logs:
            log_name = log["entries"].get("name", f"log_{log.get('id', 'unknown')}")
            image_value = log["entries"].get(image_col_name)

            # Collect diagnostic info on first attempt
            if attempt == 0:
                if image_value is None:
                    diagnostic_info[log_name] = {
                        "status": "no_img_field",
                        "value_preview": None,
                    }
                elif image_value.startswith("data:"):
                    diagnostic_info[log_name] = {
                        "status": "base64_data_uri",
                        "value_preview": image_value[:80] + "...",
                    }
                elif (
                    image_value.startswith("gs://")
                    or "storage.googleapis.com" in image_value
                ):
                    diagnostic_info[log_name] = {
                        "status": "gcs_url",
                        "full_url": image_value,
                        "extracted_filename": image_value.split("/")[-1],
                    }
                else:
                    diagnostic_info[log_name] = {
                        "status": "unknown_format",
                        "value_preview": (
                            image_value[:80] + "..."
                            if len(image_value) > 80
                            else image_value
                        ),
                    }

            if image_value:
                # Check if this is a GCS URL
                if (
                    image_value.startswith("gs://")
                    or "storage.googleapis.com" in image_value
                ):
                    filename = image_value.split("/")[-1]
                    try:
                        result = bucket_service.get_media(filename)
                        if result is None:
                            unavailable_images.append(log_name)
                            if attempt == 0:
                                diagnostic_info[log_name][
                                    "error"
                                ] = "get_media returned None (NotFound)"
                    except Exception as e:
                        unavailable_images.append(log_name)
                        if attempt == 0:
                            diagnostic_info[log_name][
                                "error"
                            ] = f"Exception: {type(e).__name__}: {e}"
                # else: it's inline Base64 data, no GCS fetch needed

        if not unavailable_images:
            logging.info(
                f"All {len(all_logs)} images available in GCS after {attempt + 1} "
                f"attempts ({total_wait_time}s total wait)",
            )
            return

        if attempt < max_retries - 1:
            delay = min(base_delay * (2**attempt), max_delay)
            total_wait_time += delay
            logging.warning(
                f"GCS pre-flight check: {len(unavailable_images)} images not yet "
                f"available ({unavailable_images}), retrying in {delay}s "
                f"(attempt {attempt + 1}/{max_retries})",
            )
            await asyncio.sleep(delay)
        else:
            # Build detailed diagnostic message
            diag_str = json.dumps(diagnostic_info, indent=2)
            raise AssertionError(
                f"GCS pre-flight check failed: Images {unavailable_images} not "
                f"available after {max_retries} attempts "
                f"({total_wait_time}s total wait time).\n\n"
                f"=== DIAGNOSTIC INFO ===\n{diag_str}\n"
                f"=== END DIAGNOSTIC INFO ===\n\n"
                f"This will help debug whether the issue is:\n"
                f"- GCS URLs not being stored (check 'status' field)\n"
                f"- Wrong filename extraction (check 'extracted_filename')\n"
                f"- GCS read failures (check 'error' field)",
            )
