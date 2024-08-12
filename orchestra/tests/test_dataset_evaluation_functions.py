import sys
import os
import asyncio
import pytest
import json
from httpx import AsyncClient

# TODO: is there a way around this?
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, project_root)
dataset_eval_path = os.path.join(project_root, "dataset_evaluation")
sys.path.insert(0, dataset_eval_path)

from dataset_evaluation.utils.fetch_queries import generate_queries
from dataset_evaluation.utils.fetch_judgements import generate_judgements

api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))
orchestra_url = str(os.getenv("ORCHESTRA_BASE_URL"))


@pytest.mark.anyio
async def test_generate_queries(client: AsyncClient, tmp_path):
    prompt_file = (
        "orchestra/tests/sample_datasets/dataset_eval/sample_mmlu_prompts.jsonl"
    )
    response_file = tmp_path / "response_file.jsonl"
    endpoint = "llama-3-8b-chat@aws-bedrock"
    batch_size = 1

    await generate_queries(
        prompt_file=prompt_file,
        response_file=response_file,
        endpoint=endpoint,
        batch_size=batch_size,
        api_key=api_key,
        client=client,
    )

    with open(response_file) as f:
        lines = [json.loads(l) for l in f]

    for expected_key in ["id_", "prompt", "endpoint", "model_response"]:
        assert expected_key in lines[0]

    assert len(lines) == 3


@pytest.mark.anyio
async def test_generate_judgements(client: AsyncClient, tmp_path):
    response_file = (
        "orchestra/tests/sample_datasets/dataset_eval/mock_model_responses.jsonl"
    )

    endpoint = "llama-3-8b-chat@aws-bedrock"
    batch_size = 1

    mock_eval_config = {"system_prompt": "hello world"}

    judge_response_file = tmp_path / "judge_response_file.jsonl"

    await generate_judgements(
        asst_response_file=response_file,
        judge_response_file=judge_response_file,
        asst_model_tag=endpoint,
        judge_model_tag=endpoint,
        batch_size=batch_size,
        api_key=api_key,
        client=client,
        eval_config=mock_eval_config,
    )
    with open(judge_response_file) as f:
        lines = [json.loads(l) for l in f]

    for expected_key in [
        "id_",
        "prompt",
        "endpoint",
        "model_response",
        "judge_endpoint",
        "judge_response",
        "score",
    ]:
        assert expected_key in lines[0], str(lines[0])

    assert len(lines) == 3
