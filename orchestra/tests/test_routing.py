import asyncio
from httpx import AsyncClient

from orchestra.tests.utils import HEADERS



## UTILS


sample_path = "./orchestra/tests/sample_datasets/with_ref.jsonl"

def _upload_dataset(client, dataset_name, data_path):
    data = {"name": dataset_name}
    with open(data_path, "rb") as f:
        file_content = f.read()
    files = {"file": ("test.jsonl", file_content, "application/x-jsonlines")}
    response = client.post("/v0/dataset", data=data, files=files, headers=HEADERS)
    return response


## TESTS


async def test_train_router(client: AsyncClient):
    url = "/v0/router/train"
    router_name = f"test_router_train_{int(time.time()*1000 % 100000)}"
    dataset_name = "test_train_router"
    endpoints = ["llama-3.1-8b-chat@aws-bedrock", "claude-3-haiku@aws-bedrock"]
    response = await _upload_dataset(client, dataset_name=dataset_name, data_path=sample_path)
    assert response.status_code == 200

    params = {"name": router_name, "dataset": dataset_name, "endpoints": endpoints}
    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200

    # check if it's actually trained
    for tries in range(30*60):
        url = "/v0/router/train/list"
        response = await client.get(url, headers=HEADERS)
        if router_name not in response.json():
            asyncio.sleep(60)

    # delete it ?
    url = "/v0/router/train"
    params = {"name": router_name}
    response = await client.delete(url, params=params, headers=HEADERS)
    assert response.status_code == 200
    
    url = "/v0/router/train/list"
    response = await client.get(url, headers=HEADERS)
    assert router_name not in response.json()

    assert False


def test_train_delete_router(client: AsyncClient):
    url = "/v0/router/train"
    assert False


def test_train_list_router(client: AsyncClient):
    url = "/v0/router/train/list"
    assert False

###

def test_deploy_router(client: AsyncClient):
    url = "/v0/router/deploy"
    assert False


def test_deploy_delete_router(client: AsyncClient):
    url = "/v0/router/deploy"
    assert False


def test_deploy_list_router(client: AsyncClient):
    url = "/v0/router/deploy/list"
    assert False
