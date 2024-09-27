import asyncio
import json
import os
import sys

import pytest
from dotenv import find_dotenv, load_dotenv
from httpx import AsyncClient
from sqlalchemy import text

import orchestra

from .test_datasets import upload_dataset

# TODO: Less hacky way for this?
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
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

# makes the tests run async
pytestmark = pytest.mark.anyio


def create_default_prompt(client, default_prompt_name):
    data = {"name": default_prompt_name, "prompt": {"temperature": 0.8}}
    # Send POST request to the /dataset endpoint
    return client.post("/v0/default_prompt", headers=HEADERS, json=data)


def _upload_dataset(client, dataset_name, data_path):
    data = {"name": dataset_name}
    with open(data_path, "rb") as f:
        file_content = f.read()
    files = {"file": ("test.jsonl", file_content, "application/x-jsonlines")}
    response = client.post("/v0/dataset", data=data, files=files, headers=HEADERS)
    return response


def _delete_dataset_evaluation(client, dataset_name):
    response = client.delete(
        "/v0/dataset",
        params={"name": dataset_name},
        headers=HEADERS,
    )
    return response


load_dotenv(find_dotenv())


async def test_trigger_eval(
    client: AsyncClient,
    tmp_path,
    monkeypatch,
):
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
    {user_message}
    </user_prompt>

    <ref_ans>
    {ref_ans}
    </ref_ans>

    <assistant_response>
    {assistant_response}
    </assistant_respose>
    """,
            },
        ],
        "temperature": 0.7,
    }

    judge_model = "llama-3-8b-chat@aws-bedrock"

    url = "/v0/evaluator"
    params = {
        "name": eval_name,
        "judge_prompt": judge_prompt,
        "prompt_parser": {
            "user_message": "['messages'][-1]['content']",
            "ref_ans": "['extra_fields']['ref_answer']",
        },
        "judge_models": judge_model,
    }
    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

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
        "endpoint": endpoint,
        "dataset": dataset,
        "evaluator": eval_name,
    }

    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    url = "/v0/evaluation"
    endpoint = "llama-3-8b-chat@aws-bedrock"
    params = {
        "url": url,
        "endpoint": endpoint,
        "dataset": dataset,
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
    #################################
    # per prompt
    url = "/v0/evaluation"
    params = {
        "dataset": dataset,
        "evaluator": eval_name,
        "endpoint": endpoint,
        "per_prompt": True,
    }
    response = await client.get(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    scores = response.json()
    assert eval_name in scores
    assert endpoint in scores[eval_name]
    assert "score" in scores[eval_name][endpoint]
    assert "progress" in scores[eval_name][endpoint]
    assert "per_prompt" in scores[eval_name][endpoint]


async def test_trigger_eval_duplicate(
    client: AsyncClient,
    tmp_path,
    monkeypatch,
):
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
    {user_message}
    </user_prompt>

    <ref_ans>
    {ref_ans}
    </ref_ans>

    <assistant_response>
    {assistant_response}
    </assistant_respose>
    """,
            },
        ],
        "temperature": 0.7,
    }

    judge_model = "llama-3-8b-chat@aws-bedrock"

    url = "/v0/evaluator"
    params = {
        "name": eval_name,
        "judge_prompt": judge_prompt,
        "prompt_parser": {
            "user_message": "['messages'][-1]['content']",
            "ref_ans": "['extra_fields']['ref_answer']",
        },
        "judge_models": judge_model,
    }
    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    # create dataset

    file_path = "./orchestra/tests/sample_datasets/prompts_with_kws.jsonl"
    dataset = "test_dataset_eval"
    response = await upload_dataset(client, file_path, dataset)
    assert response.status_code == 200, response.json()

    # trigger duplicate eval

    url = "/v0/evaluation"
    endpoint = "llama-3-8b-chat@aws-bedrock"
    params = {
        "url": url,
        "endpoint": endpoint,
        "dataset": dataset,
        "evaluator": eval_name,
    }

    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    url = "/v0/queries"
    response = await client.get(url, headers=HEADERS)
    assert len(response.json()) == 4

    # retrigger
    url = "/v0/evaluation"
    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    # check no extra queries
    url = "/v0/queries"
    response = await client.get(url, headers=HEADERS)
    assert len(response.json()) == 4

    # TODO: check the evaluation didn't get double-uploaded


# TODO: Parametrise this test to use mostly the same code as above
async def test_trigger_eval_with_default_prompt(
    client: AsyncClient,
    tmp_path,
    monkeypatch,
):
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
    eval_name = "test_eval_dp"
    default_prompt_name = "dp_1"
    judge_prompt = "dummy system prompt {user_message} {assistant_message}"
    judge_model = "llama-3-8b-chat@aws-bedrock"

    url = "/v0/evaluator"
    params = {
        "name": eval_name,
        "judge_prompt": judge_prompt,
        "judge_models": judge_model,
    }
    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    # create dataset

    file_path = "./orchestra/tests/sample_datasets/prompts_with_kws.jsonl"
    dataset = "test_dataset_eval"
    response = await upload_dataset(client, file_path, dataset)
    assert response.status_code == 200, response.json()

    # create default prompt
    response = await create_default_prompt(client, default_prompt_name)
    assert response.status_code == 200, response.json()

    # create trigger evaluation
    url = "/v0/evaluation"
    endpoint = "gpt-3.5-turbo@openai"
    params = {
        "url": url,
        "dataset": dataset,
        "endpoint": endpoint,
        "evaluator": eval_name,
        "default_prompt": default_prompt_name,
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
        "default_prompt": default_prompt_name,
    }
    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    ############################

    url = "/v0/evaluation"
    params = {
        "dataset": dataset,
        "evaluator": eval_name,
        "default_prompt": default_prompt_name,
    }
    response = await client.get(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    scores = response.json()
    assert eval_name in scores
    assert endpoint in scores[eval_name]
    assert "score" in scores[eval_name][endpoint]
    assert "progress" in scores[eval_name][endpoint]
    #################################
    # per prompt
    url = "/v0/evaluation"
    params = {
        "dataset": dataset,
        "evaluator": eval_name,
        "endpoint": endpoint,
        "per_prompt": True,
        "default_prompt": default_prompt_name,
    }
    response = await client.get(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    scores = response.json()
    assert eval_name in scores
    assert endpoint in scores[eval_name]
    assert "score" in scores[eval_name][endpoint]
    assert "progress" in scores[eval_name][endpoint]
    assert "per_prompt" in scores[eval_name][endpoint]


####################################################


async def test_trigger_pass_dataset(
    client: AsyncClient,
    tmp_path,
    monkeypatch,
    dbsession,
):
    await _seed_evaluations_db(dbsession)

    def mock_send_to_dataset_evaluation_server(action, **data):
        assert data["prompts"] == [1, 2]

    monkeypatch.setattr(
        orchestra.web.api.evaluations.views,
        "send_to_dataset_evaluation_server",
        mock_send_to_dataset_evaluation_server,
    )

    # create trigger evaluation

    url = "/v0/evaluation"
    endpoint = "llama-3-8b-chat@aws-bedrock"
    params = {
        "url": url,
        "endpoint": endpoint,
        "dataset": "test_dataset_eval",
        "evaluator": "test_eval",
    }
    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()


async def test_trigger_pass_prompts(
    client: AsyncClient,
    tmp_path,
    monkeypatch,
    dbsession,
):
    await _seed_evaluations_db(dbsession)

    def mock_send_to_dataset_evaluation_server(action, **data):
        assert data["prompts"] == [1, 2]

    monkeypatch.setattr(
        orchestra.web.api.evaluations.views,
        "send_to_dataset_evaluation_server",
        mock_send_to_dataset_evaluation_server,
    )

    # create trigger evaluation

    url = "/v0/evaluation"
    endpoint = "llama-3-8b-chat@aws-bedrock"
    params = {
        "url": url,
        "endpoint": endpoint,
        "prompts": "1,2",
        "evaluator": "test_eval",
    }
    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()


async def test_trigger_pass_invalid_prompts(
    client: AsyncClient,
    tmp_path,
    monkeypatch,
    dbsession,
):
    await _seed_evaluations_db(dbsession)

    def mock_send_to_dataset_evaluation_server(action, **data):
        assert data["prompts"] == [1, 3, 99]

    monkeypatch.setattr(
        orchestra.web.api.evaluations.views,
        "send_to_dataset_evaluation_server",
        mock_send_to_dataset_evaluation_server,
    )

    # create trigger evaluation

    url = "/v0/evaluation"
    endpoint = "llama-3-8b-chat@aws-bedrock"
    params = {
        "url": url,
        "endpoint": endpoint,
        "prompts": "1,3,99",
        "evaluator": "test_eval",
    }
    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 400, response.json()
    assert response.json() == {"detail": "The following prompt_ids are invalid: 3, 99"}


########################


async def test_client_side_scores(
    client: AsyncClient,
    dbsession,
):
    await _seed_evaluations_db(
        dbsession, path="./orchestra/tests/sql_dumps/evaluations/dump_clientside.jsonl"
    )

    eval_name = "test_eval_clientside"

    url = "/v0/evaluation"
    dataset = "test_dataset"
    endpoint = "llama-3-8b-chat@aws-bedrock"
    file_path = "./orchestra/tests/sample_datasets/prompts_with_kws_scored.jsonl"
    with open(file_path, "rb") as f:
        file_content = f.read()
    files = {
        "client_side_scores": ("test.jsonl", file_content, "application/x-jsonlines"),
    }

    params = {
        "url": url,
        "dataset": dataset,
        "endpoint": endpoint,
        "evaluator": eval_name,
    }
    response = await client.post(url, params=params, files=files, headers=HEADERS)
    assert response.status_code == 200, response.json()

    # get results

    url = "/v0/evaluation"
    params = {
        "endpoint": endpoint,
        "dataset": dataset,
        "evaluator": eval_name,
    }
    response = await client.get(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    scores = response.json()
    assert scores == {
        "test_eval_clientside": {
            "llama-3-8b-chat@aws-bedrock": {"score": 0.5, "progress": 100.0}
        }
    }


async def test_client_side_rationales(
    client: AsyncClient,
    dbsession,
):
    await _seed_evaluations_db(
        dbsession, path="./orchestra/tests/sql_dumps/evaluations/dump_clientside.jsonl"
    )

    eval_name = "test_eval_clientside"

    url = "/v0/evaluation"
    dataset = "test_dataset"
    endpoint = "llama-3-8b-chat@aws-bedrock"
    file_path = (
        "./orchestra/tests/sample_datasets/prompts_with_kws_scored_rationale.jsonl"
    )
    with open(file_path, "rb") as f:
        file_content = f.read()
    files = {
        "client_side_scores": ("test.jsonl", file_content, "application/x-jsonlines"),
    }

    params = {
        "url": url,
        "dataset": dataset,
        "endpoint": endpoint,
        "evaluator": eval_name,
    }
    response = await client.post(url, params=params, files=files, headers=HEADERS)
    assert response.status_code == 200, response.json()

    # get results

    url = "/v0/evaluation"
    params = {
        "endpoint": endpoint,
        "dataset": dataset,
        "evaluator": eval_name,
        "return_rationale": True,
        "return_response": True,
        "per_prompt": True,
    }
    response = await client.get(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    scores = response.json()
    assert scores == {
        "test_eval_clientside": {
            "llama-3-8b-chat@aws-bedrock": {
                "score": 0.5,
                "progress": 100.0,
                "per_prompt": [
                    {
                        "id": 1,
                        "response": "Madrid",
                        "score": 1.0,
                        "evaluation": [
                            {
                                "endpoint": "client_side",
                                "rationale": "Correct answer",
                                "rationale_score": 1.0,
                            }
                        ],
                    },
                    {
                        "id": 2,
                        "response": "30",
                        "score": 0.0,
                        "evaluation": [
                            {
                                "endpoint": "client_side",
                                "rationale": "Incorrect answer",
                                "rationale_score": 0.0,
                            }
                        ],
                    },
                ],
            }
        }
    }


async def test_client_side_no_rationales(
    client: AsyncClient,
    dbsession,
):
    await _seed_evaluations_db(
        dbsession, path="./orchestra/tests/sql_dumps/evaluations/dump_clientside.jsonl"
    )

    eval_name = "test_eval_clientside"

    url = "/v0/evaluation"
    dataset = "test_dataset"
    endpoint = "llama-3-8b-chat@aws-bedrock"
    file_path = "./orchestra/tests/sample_datasets/prompts_with_kws_scored.jsonl"
    with open(file_path, "rb") as f:
        file_content = f.read()
    files = {
        "client_side_scores": ("test.jsonl", file_content, "application/x-jsonlines"),
    }

    params = {
        "url": url,
        "dataset": dataset,
        "endpoint": endpoint,
        "evaluator": eval_name,
    }
    response = await client.post(url, params=params, files=files, headers=HEADERS)
    assert response.status_code == 200, response.json()

    # get results

    url = "/v0/evaluation"
    params = {
        "endpoint": endpoint,
        "dataset": dataset,
        "evaluator": eval_name,
        "return_rationale": True,
        "return_response": True,
        "per_prompt": True,
    }
    response = await client.get(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    scores = response.json()
    assert scores == {
        "test_eval_clientside": {
            "llama-3-8b-chat@aws-bedrock": {
                "score": 0.5,
                "progress": 100.0,
                "per_prompt": [
                    {
                        "id": 1,
                        "response": "",
                        "score": 1.0,
                        "evaluation": [
                            {
                                "endpoint": "client_side",
                                "rationale": "",
                                "rationale_score": 1.0,
                            }
                        ],
                    },
                    {
                        "id": 2,
                        "response": "",
                        "score": 0.0,
                        "evaluation": [
                            {
                                "endpoint": "client_side",
                                "rationale": "",
                                "rationale_score": 0.0,
                            }
                        ],
                    },
                ],
            }
        }
    }


# helper utils


async def populate_from_file(path, session):
    with open(path) as f:
        for line in f:
            command = json.loads(line)
            statement = command["statement"]
            statement = statement.replace("%(", ":").replace(")s", "")
            session.execute(text(statement), command["parameters"])


async def get_evaluation_scores(client, params):
    url = "/v0/evaluation"
    response = await client.get(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    scores = response.json()
    return scores


# Listing Tests

# The database contains
# {
#     "test_eval": {
#         "gpt-3.5-turbo@openai": {"score": 0.65, "progress": 100.0},
#         "llama-3-8b-chat@aws-bedrock": {"score": 0.4, "progress": 100.0},
#     },
#     "test_eval_multi_judge": {
#         "llama-3-8b-chat@aws-bedrock": {"score": 0.5, "progress": 100.0}
#     },
# }


async def _seed_evaluations_db(
    dbsession, path="./orchestra/tests/sql_dumps/evaluations/dump_trigger.jsonl"
):
    await populate_from_file(path=path, session=dbsession)


async def _helper_test_list_evaluations(client, params, expected_scores):
    scores = await get_evaluation_scores(client, params)
    assert scores == expected_scores


async def test_list_evaluation_evaluator_and_endpoint(client: AsyncClient, dbsession):
    await _seed_evaluations_db(dbsession)
    params = {
        "dataset": "test_dataset_eval",
        "evaluator": "test_eval",
        "endpoint": "llama-3-8b-chat@aws-bedrock",
    }
    expected_scores = {
        "test_eval": {"llama-3-8b-chat@aws-bedrock": {"score": 0.4, "progress": 100.0}}
    }
    await _helper_test_list_evaluations(client, params, expected_scores)


async def test_list_evaluation_evaluator_and_endpoint_via_prompts(
    client: AsyncClient, dbsession
):
    await _seed_evaluations_db(dbsession)
    params = {
        "prompts": "1,2",
        "evaluator": "test_eval",
        "endpoint": "llama-3-8b-chat@aws-bedrock",
    }
    expected_scores = {
        "test_eval": {"llama-3-8b-chat@aws-bedrock": {"score": 0.4, "progress": 100.0}}
    }
    await _helper_test_list_evaluations(client, params, expected_scores)


async def test_list_evaluation_endpoint(client: AsyncClient, dbsession):

    await _seed_evaluations_db(dbsession)

    params = {
        "dataset": "test_dataset_eval",
        "endpoint": "llama-3-8b-chat@aws-bedrock",
    }
    expected_scores = {
        "test_eval": {"llama-3-8b-chat@aws-bedrock": {"score": 0.4, "progress": 100.0}},
        "test_eval_multi_judge": {
            "llama-3-8b-chat@aws-bedrock": {"score": 0.5, "progress": 100.0}
        },
    }
    await _helper_test_list_evaluations(client, params, expected_scores)


async def test_list_evaluation_evaluator(client: AsyncClient, dbsession):
    await _seed_evaluations_db(dbsession)

    params = {
        "dataset": "test_dataset_eval",
        "evaluator": "test_eval",
    }

    expected_scores = {
        "test_eval": {
            "gpt-3.5-turbo@openai": {"score": 0.65, "progress": 100.0},
            "llama-3-8b-chat@aws-bedrock": {"score": 0.4, "progress": 100.0},
        }
    }
    await _helper_test_list_evaluations(client, params, expected_scores)


async def test_list_evaluation_all(client: AsyncClient, dbsession):
    await _seed_evaluations_db(dbsession)

    params = {
        "dataset": "test_dataset_eval",
    }

    expected_scores = {
        "test_eval": {
            "gpt-3.5-turbo@openai": {"score": 0.65, "progress": 100.0},
            "llama-3-8b-chat@aws-bedrock": {"score": 0.4, "progress": 100.0},
        },
        "test_eval_multi_judge": {
            "llama-3-8b-chat@aws-bedrock": {"score": 0.5, "progress": 100.0}
        },
    }
    await _helper_test_list_evaluations(client, params, expected_scores)


async def test_list_evaluation_per_prompt(client: AsyncClient, dbsession):
    await _seed_evaluations_db(dbsession)
    params = {
        "dataset": "test_dataset_eval",
        "evaluator": "test_eval",
        "endpoint": "llama-3-8b-chat@aws-bedrock",
        "per_prompt": True,
    }
    expected_scores = {
        "test_eval": {
            "llama-3-8b-chat@aws-bedrock": {
                "per_prompt": [{"id": 1, "score": 0.8}, {"id": 2, "score": 0.0}],
                "score": 0.4,
                "progress": 100.0,
            }
        }
    }
    await _helper_test_list_evaluations(client, params, expected_scores)


async def test_list_evaluation_rationale_no_perprompt(client: AsyncClient, dbsession):
    await _seed_evaluations_db(dbsession)

    params = {
        "dataset": "test_dataset_eval",
        "evaluator": "test_eval",
        "return_rationale": True,
    }
    url = "/v0/evaluation"
    response = await client.get(url, params=params, headers=HEADERS)
    assert response.status_code == 404, response.json()
    assert response.json() == {
        "detail": "If return_rationale=True, need to also have per_prompt=True."
    }


async def test_list_evaluation_rationale_response(client: AsyncClient, dbsession):
    await _seed_evaluations_db(dbsession)
    params = {
        "dataset": "test_dataset_eval",
        "evaluator": "test_eval_multi_judge",
        "endpoint": "llama-3-8b-chat@aws-bedrock",
        "return_rationale": True,
        "return_response": True,
        "per_prompt": True,
    }
    expected_scores = {
        "test_eval_multi_judge": {
            "llama-3-8b-chat@aws-bedrock": {
                "score": 0.5,
                "progress": 100.0,
                "per_prompt": [
                    {
                        "id": 1,
                        "response": "The capital of Spain is Madrid.",
                        "score": 0.75,
                        "evaluation": [
                            {
                                "endpoint": "llama-3-8b-chat@aws-bedrock",
                                "rationale": '<explanation>\nThe assistant\'s response is a straightforward and accurate answer to the user\'s question. The capital of Spain is indeed Madrid. The answer is clear, concise, and easy to understand. However, the response lacks any additional information or context that might be helpful to the user. Nevertheless, the answer is correct and relevant to the question.\n\nFinal rating: good\n\n{"assistant_rating": "good"}',
                                "rationale_score": 0.5,
                            },
                            {
                                "endpoint": "gpt-3.5-turbo@openai",
                                "rationale": 'The assistant\'s answer is correct and directly addresses the user prompt by providing the capital of Spain, which is Madrid. The response is clear and on point.\n\n{"assistant_rating": "excellent"}',
                                "rationale_score": 1.0,
                            },
                        ],
                    },
                    {
                        "id": 2,
                        "response": "The square root of 1009 to 1 decimal place is 32.1.",
                        "score": 0.25,
                        "evaluation": [
                            {
                                "endpoint": "llama-3-8b-chat@aws-bedrock",
                                "rationale": 'The square root of 1009 to 1 decimal place is indeed approximately 32.1. However, the assistant\'s response lacks a clear explanation or justification for the calculation. A simple "because I said so" or "because it\'s correct" is not sufficient.\n\nThe assistant\'s response is straightforward and provides the correct answer, but it does not demonstrate an understanding of the underlying mathematical concept or provide any additional context. A good answer should not only provide the correct answer but also explain the reasoning or process used to arrive at that answer.\n\nBased on the rating rules, I would rate the assistant\'s answer as "good".\n\n{"assistant_rating": "good"}',
                                "rationale_score": 0.5,
                            },
                            {
                                "endpoint": "gpt-3.5-turbo@openai",
                                "rationale": 'The square root of 1009 to 1 decimal place is actually approximately 31.8, not 32.1. The assistant\'s answer is incorrect. Therefore, I would rate this response as "bad".\n\n{"assistant_rating": "bad"}',
                                "rationale_score": 0.0,
                            },
                        ],
                    },
                ],
            }
        }
    }
    await _helper_test_list_evaluations(client, params, expected_scores)


async def test_list_evaluation_rationale(client: AsyncClient, dbsession):
    await _seed_evaluations_db(dbsession)
    params = {
        "dataset": "test_dataset_eval",
        "evaluator": "test_eval_multi_judge",
        "endpoint": "llama-3-8b-chat@aws-bedrock",
        "return_rationale": True,
        "per_prompt": True,
    }
    expected_scores = {
        "test_eval_multi_judge": {
            "llama-3-8b-chat@aws-bedrock": {
                "score": 0.5,
                "progress": 100.0,
                "per_prompt": [
                    {
                        "id": 1,
                        "score": 0.75,
                        "evaluation": [
                            {
                                "endpoint": "llama-3-8b-chat@aws-bedrock",
                                "rationale": '<explanation>\nThe assistant\'s response is a straightforward and accurate answer to the user\'s question. The capital of Spain is indeed Madrid. The answer is clear, concise, and easy to understand. However, the response lacks any additional information or context that might be helpful to the user. Nevertheless, the answer is correct and relevant to the question.\n\nFinal rating: good\n\n{"assistant_rating": "good"}',
                                "rationale_score": 0.5,
                            },
                            {
                                "endpoint": "gpt-3.5-turbo@openai",
                                "rationale": 'The assistant\'s answer is correct and directly addresses the user prompt by providing the capital of Spain, which is Madrid. The response is clear and on point.\n\n{"assistant_rating": "excellent"}',
                                "rationale_score": 1.0,
                            },
                        ],
                    },
                    {
                        "id": 2,
                        "score": 0.25,
                        "evaluation": [
                            {
                                "endpoint": "llama-3-8b-chat@aws-bedrock",
                                "rationale": 'The square root of 1009 to 1 decimal place is indeed approximately 32.1. However, the assistant\'s response lacks a clear explanation or justification for the calculation. A simple "because I said so" or "because it\'s correct" is not sufficient.\n\nThe assistant\'s response is straightforward and provides the correct answer, but it does not demonstrate an understanding of the underlying mathematical concept or provide any additional context. A good answer should not only provide the correct answer but also explain the reasoning or process used to arrive at that answer.\n\nBased on the rating rules, I would rate the assistant\'s answer as "good".\n\n{"assistant_rating": "good"}',
                                "rationale_score": 0.5,
                            },
                            {
                                "endpoint": "gpt-3.5-turbo@openai",
                                "rationale": 'The square root of 1009 to 1 decimal place is actually approximately 31.8, not 32.1. The assistant\'s answer is incorrect. Therefore, I would rate this response as "bad".\n\n{"assistant_rating": "bad"}',
                                "rationale_score": 0.0,
                            },
                        ],
                    },
                ],
            }
        }
    }
    await _helper_test_list_evaluations(client, params, expected_scores)


async def test_list_evaluation_responses(client: AsyncClient, dbsession):
    await _seed_evaluations_db(dbsession)
    params = {
        "dataset": "test_dataset_eval",
        "evaluator": "test_eval_multi_judge",
        "endpoint": "llama-3-8b-chat@aws-bedrock",
        "return_response": True,
        "per_prompt": True,
    }
    expected_scores = {
        "test_eval_multi_judge": {
            "llama-3-8b-chat@aws-bedrock": {
                "score": 0.5,
                "progress": 100.0,
                "per_prompt": [
                    {
                        "id": 1,
                        "response": "The capital of Spain is Madrid.",
                        "score": 0.75,
                    },
                    {
                        "id": 2,
                        "response": "The square root of 1009 to 1 decimal place is 32.1.",
                        "score": 0.25,
                    },
                ],
            }
        }
    }
    await _helper_test_list_evaluations(client, params, expected_scores)


async def test_list_evaluation_responses_from_prompt_ids(
    client: AsyncClient, dbsession
):
    await _seed_evaluations_db(dbsession)
    params = {
        "prompts": "2",
        "evaluator": "test_eval_multi_judge",
        "endpoint": "llama-3-8b-chat@aws-bedrock",
        "return_response": True,
        "per_prompt": True,
    }
    expected_scores = {
        "test_eval_multi_judge": {
            "llama-3-8b-chat@aws-bedrock": {
                "score": 0.25,
                "progress": 100.0,
                "per_prompt": [
                    {
                        "id": 2,
                        "response": "The square root of 1009 to 1 decimal place is 32.1.",
                        "score": 0.25,
                    },
                ],
            }
        }
    }
    await _helper_test_list_evaluations(client, params, expected_scores)


async def test_list_evaluation_sub_scorers(client: AsyncClient, dbsession):
    await _seed_evaluations_db(dbsession)
    params = {
        "dataset": "test_dataset_eval",
        "evaluator": "test_eval_multi_judge",
        "endpoint": "llama-3-8b-chat@aws-bedrock",
        "sub_scorers": True,
    }
    expected_scores = {
        "test_eval_multi_judge": {
            "llama-3-8b-chat@aws-bedrock": {
                "score": 0.5,
                "progress": 100.0,
                "sub_scores": {
                    "gpt-3.5-turbo@openai": {"0.0": 1, "1.0": 1},
                    "llama-3-8b-chat@aws-bedrock": {"0.5": 2},
                },
            }
        }
    }
    await _helper_test_list_evaluations(client, params, expected_scores)


# Deletion Tests


async def test_delete_evaluation_endpoint_and_evaluator(client: AsyncClient, dbsession):

    await _seed_evaluations_db(dbsession)

    params = {
        "dataset": "test_dataset_eval",
        "evaluator": "test_eval",
        "endpoint": "llama-3-8b-chat@aws-bedrock",
    }
    # delete evaluation
    response = await client.delete(
        "/v0/evaluation",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert response.json() == {
        "info": "Evaluation deleted successfully. You deleted 2 evaluations.",
    }

    # check deleted
    expected_scores = {
        "test_eval": {"gpt-3.5-turbo@openai": {"score": 0.65, "progress": 100.0}},
        "test_eval_multi_judge": {
            "llama-3-8b-chat@aws-bedrock": {"score": 0.5, "progress": 100.0}
        },
    }
    all_params = {"dataset": "test_dataset_eval"}
    await _helper_test_list_evaluations(client, all_params, expected_scores)


async def test_delete_evaluation_endpoint_and_evaluator_from_prompt_ids(
    client: AsyncClient, dbsession
):

    await _seed_evaluations_db(dbsession)

    params = {
        "prompts": "1,2",
        "evaluator": "test_eval",
        "endpoint": "llama-3-8b-chat@aws-bedrock",
    }
    # delete evaluation
    response = await client.delete(
        "/v0/evaluation",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert response.json() == {
        "info": "Evaluation deleted successfully. You deleted 2 evaluations.",
    }

    # check deleted
    expected_scores = {
        "test_eval": {"gpt-3.5-turbo@openai": {"score": 0.65, "progress": 100.0}},
        "test_eval_multi_judge": {
            "llama-3-8b-chat@aws-bedrock": {"score": 0.5, "progress": 100.0}
        },
    }
    all_params = {"dataset": "test_dataset_eval"}
    await _helper_test_list_evaluations(client, all_params, expected_scores)


async def test_delete_no_endpoint(client: AsyncClient, dbsession):

    await _seed_evaluations_db(dbsession)

    params = {
        "dataset": "test_dataset_eval",
        "evaluator": "test_eval",
    }

    # delete evaluation
    response = await client.delete(
        "/v0/evaluation",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert response.json() == {
        "info": "Evaluation deleted successfully. You deleted 4 evaluations.",
    }

    # check deleted
    expected_scores = {
        "test_eval_multi_judge": {
            "llama-3-8b-chat@aws-bedrock": {"score": 0.5, "progress": 100.0},
        },
    }
    all_params = {"dataset": "test_dataset_eval"}
    await _helper_test_list_evaluations(client, all_params, expected_scores)


async def test_delete_no_evaluator(client: AsyncClient, dbsession):

    await _seed_evaluations_db(dbsession)

    params = {
        "dataset": "test_dataset_eval",
        "endpoint": "llama-3-8b-chat@aws-bedrock",
    }

    # delete evaluation
    response = await client.delete(
        "/v0/evaluation",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert response.json() == {
        "info": "Evaluation deleted successfully. You deleted 4 evaluations.",
    }

    # check deleted
    expected_scores = {
        "test_eval": {
            "gpt-3.5-turbo@openai": {"score": 0.65, "progress": 100.0},
        },
    }
    all_params = {"dataset": "test_dataset_eval"}
    await _helper_test_list_evaluations(client, all_params, expected_scores)


async def test_delete_no_evaluator_no_endpoint(client: AsyncClient, dbsession):

    await _seed_evaluations_db(dbsession)

    params = {
        "dataset": "test_dataset_eval",
    }

    # delete evaluation
    response = await client.delete(
        "/v0/evaluation",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert response.json() == {
        "info": "Evaluation deleted successfully. You deleted 6 evaluations.",
    }

    # check deleted
    expected_scores = {}
    all_params = {"dataset": "test_dataset_eval"}
    await _helper_test_list_evaluations(client, all_params, expected_scores)


async def test_delete_bad_evaluator(client: AsyncClient, dbsession):

    await _seed_evaluations_db(dbsession)

    params = {"dataset": "test_dataset_eval", "evaluator": "fake_evaluator"}

    # delete evaluation
    response = await client.delete(
        "/v0/evaluation",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 404
    assert response.json() == {
        "detail": "The evaluator fake_evaluator does not exist in your account",
    }


async def test_delete_bad_endpoint(client: AsyncClient, dbsession):

    await _seed_evaluations_db(dbsession)

    params = {"dataset": "test_dataset_eval", "endpoint": "llama-5b@amazon.com"}

    # delete evaluation
    response = await client.delete(
        "/v0/evaluation",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "Could not find endpoint: llama-5b@amazon.com"}
