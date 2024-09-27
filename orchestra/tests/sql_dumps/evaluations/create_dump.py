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


from sqlalchemy import Engine, event
from dotenv import find_dotenv, load_dotenv

import orchestra
from orchestra.tests.test_evaluation import _seed_evaluations_db

# TODO: Less hacky way for this?
project_root = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
)
sys.path.insert(0, project_root)
dataset_eval_path = os.path.join(project_root, "dataset_evaluation")
sys.path.insert(0, dataset_eval_path)
from dataset_evaluation.evaluate_dataset import evaluate_dataset

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


PATH_FOR_DUMP = os.path.join(os.path.dirname(__file__), "./tmp_dump_trigger.jsonl")
# wipe the file
open(PATH_FOR_DUMP, "w").close()


@pytest.mark.manual
async def test_create_data_trigger_eval(
    client: AsyncClient,
    tmp_path,
    monkeypatch,
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

    def mock_send_to_dataset_evaluation_server(action, **data):
        data.pop("user_email", "")
        message_data = json.dumps(
            {
                "action": action,
                **data,
                "orchestra_url": "",
                "admin_key": os.environ.get("ORCHESTRA_ADMIN_KEY"),
            },
        )
        save_dir = tmp_path / "save_files"
        if action == "evaluate":
            asyncio.run(
                evaluate_dataset(
                    message_data,
                    save_dir,
                    shared_volume="",
                    client=client,
                ),
            )
        elif action == "refresh_scores":
            user_id = data["user_id"]
            asyncio.run(refresh_scores_for_user(user_id, save_dir))
        else:
            raise NotImplementedError

    monkeypatch.setattr(
        orchestra.web.api.evaluations.views,
        "send_to_dataset_evaluation_server",
        mock_send_to_dataset_evaluation_server,
    )

    # create evaluator
    eval_name = "test_eval"
    judge_prompt = {
        "messages": [
            {
                "role": "system",
                "content": """As a judge, rate the assistant's answer to the user prompt.""",
            },
            {
                "role": "user",
                "content": """
    <user_prompt>
    {user_prompt}
    </user_prompt>

    <assistant_response>
    {response}
    </assistant_respose>

    follow these rating rules:
    <rating rules>
    {class_config}
    </rating rules>""",
            },
        ],
        "temperature": 0.7,
    }
    judge_model = "llama-3-8b-chat@aws-bedrock"

    url = "/v0/evaluator"
    params = {
        "name": eval_name,
        "judge_prompt": judge_prompt,
        "judge_models": judge_model,
    }
    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    url = "/v0/evaluator"
    params = {
        "name": "test_eval_multi_judge",
        "judge_prompt": judge_prompt,
        "judge_models": ["llama-3-8b-chat@aws-bedrock", "gpt-3.5-turbo@openai"],
    }
    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    # create second
    # create dataset

    file_path = "./orchestra/tests/sample_datasets/prompts_with_kws.jsonl"
    dataset = "test_dataset_eval"
    response = await upload_dataset(client, file_path, dataset)
    assert response.status_code == 200, response.json()

    # create trigger evaluation
    url = "/v0/evaluation"
    endpoint = "gpt-3.5-turbo@openai"
    params = {
        "url": url,
        "dataset": dataset,
        "endpoint": endpoint,
        "evaluator": eval_name,
    }
    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    url = "/v0/evaluation"
    endpoint = "llama-3-8b-chat@aws-bedrock"
    params = {
        "url": url,
        "dataset": dataset,
        "endpoint": endpoint,
        "evaluator": eval_name,
    }
    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    url = "/v0/evaluation"
    endpoint = "llama-3-8b-chat@aws-bedrock"
    params = {
        "url": url,
        "dataset": dataset,
        "endpoint": endpoint,
        "evaluator": "test_eval_multi_judge",
    }
    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    ############################

    url = "/v0/evaluation"
    params = {"dataset": dataset, "evaluator": eval_name}
    response = await client.get(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    scores = response.json()
    assert eval_name in scores
    assert endpoint in scores[eval_name]
    assert "score" in scores[eval_name][endpoint]
    assert "progress" in scores[eval_name][endpoint]

    url = "/v0/evaluation"
    params = {"dataset": dataset, "evaluator": eval_name}
    response = await client.get(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    scores = response.json()
    assert eval_name in scores
    assert endpoint in scores[eval_name]
    assert "score" in scores[eval_name][endpoint]
    assert "progress" in scores[eval_name][endpoint]

    url = "/v0/evaluation"
    params = {"dataset": dataset, "evaluator": eval_name}
    response = await client.get(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    scores = response.json()
    assert eval_name in scores
    assert endpoint in scores[eval_name]
    assert "score" in scores[eval_name][endpoint]
    assert "progress" in scores[eval_name][endpoint]


@pytest.mark.manual
async def test_create_data_clientside(
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

    # create test dataset
    file_path = "./orchestra/tests/sample_datasets/prompts_with_kws.jsonl"
    dataset = "test_dataset"
    response = await upload_dataset(client, file_path, dataset)
    assert response.status_code == 200, response.json()

    url = "/v0/evaluator"
    eval_name = "test_eval_clientside"
    params = {
        "name": eval_name,
        "client_side": True,
    }
    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    url = "/v0/evaluation"
    params = {"dataset": dataset, "evaluator": eval_name}
    response = await client.get(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    scores = response.json()




@pytest.mark.manual
async def test_create_data_for_router(
    client: AsyncClient,
    tmp_path,
    monkeypatch,
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

    def mock_send_to_dataset_evaluation_server(action, **data):
        data.pop("user_email", "")
        message_data = json.dumps(
            {
                "action": action,
                **data,
                "orchestra_url": "",
                "admin_key": os.environ.get("ORCHESTRA_ADMIN_KEY"),
            },
        )
        save_dir = tmp_path / "save_files"
        if action == "evaluate":
            asyncio.run(
                evaluate_dataset(
                    message_data,
                    save_dir,
                    shared_volume="",
                    client=client,
                ),
            )
        elif action == "refresh_scores":
            user_id = data["user_id"]
            asyncio.run(refresh_scores_for_user(user_id, save_dir))
        else:
            raise NotImplementedError

    monkeypatch.setattr(
        orchestra.web.api.evaluations.views,
        "send_to_dataset_evaluation_server",
        mock_send_to_dataset_evaluation_server,
    )

    # create evaluator
    eval_name = "test_eval"
    judge_prompt = {
        "messages": [
            {
                "role": "system",
                "content": """As a judge, rate the assistant's answer to the user prompt.""",
            },
            {
                "role": "user",
                "content": """
    <user_prompt>
    {user_prompt}
    </user_prompt>

    <assistant_response>
    {response}
    </assistant_respose>

    follow these rating rules:
    <rating rules>
    {class_config}
    </rating rules>""",
            },
        ],
        "temperature": 0.7,
    }
    judge_model = "llama-3-8b-chat@aws-bedrock"

    url = "/v0/evaluator"
    params = {
        "name": eval_name,
        "judge_prompt": judge_prompt,
        "judge_models": judge_model,
    }
    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    url = "/v0/evaluator"
    params = {
        "name": "test_eval_multi_judge",
        "judge_prompt": judge_prompt,
        "judge_models": ["llama-3-8b-chat@aws-bedrock", "gpt-3.5-turbo@openai"],
    }
    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    # create second
    # create dataset

    file_path = "./orchestra/tests/sample_datasets/prompts_router_train.jsonl"
    dataset = "test_dataset_eval"
    response = await upload_dataset(client, file_path, dataset)
    assert response.status_code == 200, response.json()

    # create trigger evaluation
    url = "/v0/evaluation"
    endpoint = "llama-3-70b-chat@aws-bedrock"
    params = {
        "url": url,
        "dataset": dataset,
        "endpoint": endpoint,
        "evaluator": eval_name,
    }
    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    
    url = "/v0/evaluation"
    endpoint = "llama-3-8b-chat@aws-bedrock"
    params = {
        "url": url,
        "dataset": dataset,
        "endpoint": endpoint,
        "evaluator": eval_name,
    }
    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()



    ############################

    url = "/v0/evaluation"
    params = {"dataset": dataset, "evaluator": eval_name}
    response = await client.get(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    scores = response.json()
    assert eval_name in scores
    assert endpoint in scores[eval_name]
    assert "score" in scores[eval_name][endpoint]
    assert "progress" in scores[eval_name][endpoint]

    url = "/v0/evaluation"
    params = {"dataset": dataset, "evaluator": eval_name}
    response = await client.get(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    scores = response.json()
    assert eval_name in scores
    assert endpoint in scores[eval_name]
    assert "score" in scores[eval_name][endpoint]
    assert "progress" in scores[eval_name][endpoint]

    url = "/v0/evaluation"
    params = {"dataset": dataset, "evaluator": eval_name}
    response = await client.get(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    scores = response.json()
    assert eval_name in scores
    assert endpoint in scores[eval_name]
    assert "score" in scores[eval_name][endpoint]
    assert "progress" in scores[eval_name][endpoint]



@pytest.mark.manual
async def test_create_data_for_trained_router(client: AsyncClient, monkeypatch, dbsession):

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

    # mocking pubsub
    def mock_send_to_train_server(action, **data):
        pass

    monkeypatch.setattr(
        orchestra.web.api.router_training.views,
        "send_to_train_server",
        mock_send_to_train_server,
    )

    ### test begins
    await _seed_evaluations_db(
        dbsession,
        path="./orchestra/tests/sql_dumps/evaluations/dump_router_train.jsonl",
    )

    url = "/v0/router/train"
    params = {
        "name": "my_test_router",
        "prompts": "1,2,3",
        "endpoints": ["llama-3-8b-chat@aws-bedrock", "llama-3-70b-chat@aws-bedrock"],
        "evaluator": "test_eval",
    }
    response = await client.post(url, params=params, headers=HEADERS)

    url = "/v0/router/train"
    params = {
        "name": "my_test_router_2",
        "prompts": "1,2,3,4",
        "endpoints": ["llama-3-8b-chat@aws-bedrock", "llama-3-70b-chat@aws-bedrock"],
        "evaluator": "test_eval",
    }
    response = await client.post(url, params=params, headers=HEADERS)
