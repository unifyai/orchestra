# This file automatically generates the dump.jsonl file
# which we can use to seed the database to a specific state
# if you want to generate different seed data, add a new test
# with a corresponding @event.listens_for part

# warning: sometimes the @event.listens doesn't log all sql queries
# temp fix by doing an extra api call
# haven't figured out what causes it to get cut off


import asyncio
import json
import os
import sys

import pytest
from dotenv import find_dotenv, load_dotenv
from httpx import AsyncClient
from sqlalchemy import text

import orchestra

from sqlalchemy import Engine, event

from dotenv import find_dotenv, load_dotenv


api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))
test_user_id = os.getenv("AUTH_ACCOUNT_USER_ID")

HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {api_key}",
}


def upload_dataset(client, file_path, name):
    with open(file_path, "rb") as f:
        file_content = f.read()
    # Prepare the multipart form data
    files = {"file": ("test.jsonl", file_content, "application/x-jsonlines")}
    data = {"name": name}
    # Send POST request to the /dataset endpoint
    return client.post("/v0/dataset", headers=HEADERS, data=data, files=files)


load_dotenv(find_dotenv())
api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))
test_user_id = os.getenv("AUTH_ACCOUNT_USER_ID")

HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {api_key}",
}


PATH_FOR_DUMP = os.path.join(os.path.dirname(__file__), "./tmp_dataset_dump.jsonl")


def upload_dataset(client, file_path, name):

    with open(file_path, "rb") as f:
        file_content = f.read()
    files = {"file": ("test.jsonl", file_content, "application/x-jsonlines")}
    data = {"name": name}
    return client.post("/v0/dataset", headers=HEADERS, data=data, files=files)


def fetch_datasets(client):
    url = "/v0/dataset/list"
    return client.get(url, headers=HEADERS)


@pytest.mark.manual
async def test_upload_dataset(
    client: AsyncClient,
):
    @event.listens_for(Engine, "before_cursor_execute")
    def receive_before_cursor_execute(
        conn, cursor, statement, parameters, context, executemany
    ):
        "listen for the 'before_cursor_execute' event"
        obj = {"statement": statement, "parameters": parameters}
        if (
            statement.startswith("SELECT")
            or statement.startswith("DROP")
            or statement.startswith("UPDATE users")
        ):
            return
        with open(PATH_FOR_DUMP, "a") as f:
            f.write(json.dumps(obj, default=str))
            f.write("\n")
            f.flush()

    # upload
    file_path = "./orchestra/tests/sample_datasets/prompts_with_kws.jsonl"
    name = "test_upload_dataset"
    response = await upload_dataset(client, file_path, name)
    assert response.status_code == 200, response.json()

    response = await fetch_datasets(client)
    assert name in response.json()

    params = {"name": name}
    response = await client.get("/v0/dataset", headers=HEADERS, params=params)
    jsonl = response.json()
    print(jsonl)

    file_path = "./orchestra/tests/sample_datasets/prompts_with_kws_longer.jsonl"
    name = "test_second_upload_dataset"
    response = await upload_dataset(client, file_path, name)
    assert response.status_code == 200, response.json()

    response = await fetch_datasets(client)
    assert name in response.json()
    print(response.json())
    response = await fetch_datasets(client)
    assert name in response.json()
    print(response.json())
