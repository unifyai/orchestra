import asyncio
import datetime
import json
import logging
import os
import re
import statistics
from typing import Any, Dict, List, Optional, cast

import numpy as np
from litellm import ModelResponse
from prettytable import PrettyTable
from providers.completion import PROVIDER_CLASSES
from providers.completion.base_completion_provider import BaseCompletionProvider
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from tqdm import tqdm

from orchestra.db.dao.datapoint_dao import DatapointDAO
from orchestra.db.dao.endpoint_dao import EndpointDAO
from orchestra.db.dao.model_dao import ModelDAO
from orchestra.db.dao.provider_dao import ProviderDAO
from orchestra.web.api.admin.schema import DatapointModelRequest, EndpointModelRequest
from orchestra.web.api.admin.views import create_datapoint_model, create_endpoint_model
from orchestra.web.api.endpoint.views import get_endpoint
from orchestra.web.api.model.views import get_model
from orchestra.web.api.provider.views import get_provider

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

handler = logging.StreamHandler()
handler.setLevel(logging.INFO)
logger.addHandler(handler)

MAX_TOKENS = 500
COLD_START_THRESHOLD = 30000


def evaluate_answers(
    evaluator_model: str,
    provider: BaseCompletionProvider,
    query: str,
    ground_truth: str,
    answer: str,
) -> int:
    """
    Evaluates the answer using the evaluator model and returns the score.

    :param evaluator_model: The model used to evaluate the answer.
    :param provider: The provider of the evaluator.
    :param query: The problem sent to models being benchmarked.
    :param ground_truth: The correct answer for the query.
    :param answer: The answer to be evaluated.
    :raises ValueError: If evaluator_model provider throws error during API call.
    :return: The score of the evaluated answer.
    """
    system = (
        "You are given a problem and student's solution. "
        "If the correct answer is provided use it, otherwise first think about "
        "the solution yourself, then score the student's solution "
        "with one of these scores:\n"
        "0 - Student provided incorrect or no solution\n"
        "3 - Student provided correct solution\n\n"
        "Your output should be using always this template:\n"
        "Score: #\n"
    )
    if ground_truth:
        prompt = (
            f"Problem: {query}\n"
            f"Correct answer: {ground_truth}\n"
            f"Student solution: {answer}"
        )
    else:
        prompt = f"Problem: {query}\nStudent solution: {answer}"

    evaluator_result = provider.complete(
        evaluator_model,
        [{"content": system, "role": "system"}, {"content": prompt, "role": "user"}],
        max_tokens=MAX_TOKENS,
    )
    if evaluator_result is None:
        raise ValueError(f"{evaluator_model} on {provider} threw an error during call")
    found = re.search(
        r"Score: (\d+)",
        evaluator_result[0].choices[0].message.content,  # noqa: WPS219, E501
    )
    if found:
        return int(found.group(1))
    return 0


def load_problems(benchmark_problems_path: str) -> List[tuple[str, str]]:
    """
    Load the problems from the benchmark_problems_path file.

    :param benchmark_problems_path: JSONL file path with problems.
    :return: List of problems.
    """
    problems = []
    with open(benchmark_problems_path, "r") as file:
        lines = file.readlines()
        for line in lines:
            data = json.loads(line)
            problems.append((data[0], data[1]))
    return problems


def get_provider_obj(
    provider_name: str,
    traversed_providers: Dict[str, BaseCompletionProvider],
) -> BaseCompletionProvider:
    """
    Get the provider object from the provider name.

    Will avoid creating a new if it already exists.

    :param provider_name: The provider answer get object of.
    :param traversed_providers: List of traversed providers.
    :return: Provider object.
    """
    provider_obj = traversed_providers.get(provider_name)
    if provider_obj is None:
        provider_obj = PROVIDER_CLASSES[provider_name]()
        if provider_name == "Vertex AI":
            from providers.completion.vertexai import VertexAI  # noqa: WPS433

            provider_obj = cast(VertexAI, provider_obj)
            provider_obj.set_service_account_credentials(
                str(os.getenv("ORCHESTRA_VERTEX_AI_SERVICE_ACC_JSON")),
                str(os.getenv("ORCHESTRA_VERTEX_AI_GCLOUD_PATH")),
            )
            provider_obj.set_project(str(os.getenv("ORCHESTRA_VERTEX_AI_PROJECT")))
            provider_obj.set_location(str(os.getenv("ORCHESTRA_VERTEX_AI_LOCATION")))
        else:
            provider_obj.set_api_key(
                api_key=str(
                    os.getenv(
                        f"ORCHESTRA_{provider_name.replace('-', '_').upper()}_API_KEY",  # noqa: WPS237, E501
                    ),
                ),
            )
        traversed_providers[provider_name] = provider_obj  # noqa: WPS529
    return provider_obj


def get_completion_results(  # noqa: D103
    provider: BaseCompletionProvider,
    model: str,
    problems: List[tuple[str, str]],
) -> Optional[List[str]]:
    completion_results = []
    for prompt in tqdm(problems):
        result = provider.complete(
            model,
            [{"content": prompt[0], "role": "user"}],
        )
        if result is None:
            return None
        # handles cold-start skewing latency
        if result[0]._response_ms > COLD_START_THRESHOLD:
            cold_start_latency = result[0]._response_ms
            result = provider.complete(
                model,
                [{"content": prompt[0], "role": "user"}],
            )
        else:
            cold_start_latency = 0
        result[0].cold_start_latency = cold_start_latency
        completion_results.append(result[0])
    return completion_results


def add_cost_info(  # noqa: WPS211
    model_results: Dict[str, Any],
    model_name: str,
    provider_name: str,
    provider: BaseCompletionProvider,
):
    """
    Adds input & output cost metadata to the model_results dict.

    :param model_results: The model and provider results dict.
    :param model_name: The model to calculate the cost of.
    :param provider_name: The provider to calculate the cost of.
    :param provider: The provider object of the model.
    """
    cost_data = provider.supported_models[model_name]["cost"]  # type: ignore
    if cost_data.get("per_character"):
        model_results[model_name][provider_name][
            "input_cost_llm_per_character"
        ] = cost_data["prompt"]
        model_results[model_name][provider_name][
            "output_cost_llm_per_character"
        ] = cost_data["completion"]
    elif cost_data.get("per_second"):
        logger.info(
            f"Per second pricing not supported yet so skipped "
            f"{model_name}: {provider_name} ",
        )
    else:
        model_results[model_name][provider_name]["input_cost_llm"] = cost_data["prompt"]
        model_results[model_name][provider_name]["output_cost_llm"] = cost_data[
            "completion"
        ]


def get_evaluator_provider(evaluator: str) -> BaseCompletionProvider:  # noqa: D103
    for provider_name, provider_class in PROVIDER_CLASSES.items():
        if evaluator in provider_class.supported_models:
            evaluator_provider = provider_class()
            evaluator_provider.set_api_key(
                api_key=str(os.getenv(f"ORCHESTRA_{provider_name.upper()}_API_KEY")),
            )
            return evaluator_provider
    raise ValueError("Evaluator model not supported")


def calculate_results(  # noqa: D103
    completion_results: List[ModelResponse],
    model_name: str,
    provider: BaseCompletionProvider,
) -> Dict[str, Any]:
    cleaned_output = {
        "output_answers": [
            obj.choices[0].message.content for obj in completion_results
        ],
        "total_latency": sum(
            [result._response_ms for result in completion_results],
        ),
        "total_output_tokens": sum(
            [result.usage.total_tokens for result in completion_results],
        ),
        "median_latency": statistics.median(
            [result._response_ms for result in completion_results],
        ),
    }
    cleaned_output["output_toks_per_sec"] = (
        cleaned_output["total_output_tokens"] * 1000 / cleaned_output["total_latency"]
    )
    cleaned_output["context_window"] = provider.supported_models[model_name.lower()][
        "context_window"
    ]

    cold_start_avg = np.mean(
        [result.cold_start_latency for result in completion_results],
    )
    if cold_start_avg == 0:
        cleaned_output["cold_start_latency"] = "0"
    else:
        cold_start_std = np.std(
            [result.cold_start_latency for result in completion_results],
        )
        cleaned_output[
            "cold_start_latency"
        ] = f"{cold_start_avg:.2f} ± {cold_start_std:.2f}"
    return cleaned_output


def calculate_score(  # noqa: D103
    evaluator: str,
    evaluator_provider: BaseCompletionProvider,
    provider_data: Dict[str, List[str]],
    problems: List[tuple[str, str]],
) -> int:
    return sum(
        [
            evaluate_answers(
                evaluator,
                evaluator_provider,
                prompt[0],
                prompt[1],
                answer,
            )
            for answer, prompt in zip(
                provider_data["output_answers"],
                problems,
            )
        ],
    )


def create_table(  # noqa: D103, WPS210
    model_results: Dict[str, Any],
    evaluator: Optional[str],
) -> PrettyTable:
    headers = [
        "Model",
        "Provider",
        "Output tokens",
        "Latency (ms)",
        "Speed (output tokens/sec)",
        "Input cost",
        "Output cost",
        "Context window",
        "Cold start",
    ]

    if evaluator:
        headers.append("QnA score")
    table = PrettyTable(headers)

    for model_name, provider_results in model_results.items():
        for provider_name, provider_data in provider_results.items():
            model_results[model_name][provider_name].pop("output_answers", None)
            row_data = [
                model_name,
                provider_name,
                provider_data["total_output_tokens"],
                f'{provider_data["total_latency"]:.2f}',
                f'{provider_data["output_toks_per_sec"]:.2f}',
                provider_data.get(
                    "input_cost_llm",
                    provider_data.get("input_cost_llm_per_character", "NA"),
                ),
                provider_data.get(
                    "output_cost_llm",
                    provider_data.get("output_cost_llm_per_character", "NA"),
                ),
                provider_data["context_window"],
                provider_data["cold_start_latency"],
            ]
            if evaluator:
                row_data.append(provider_data["score"])
            table.add_row(row_data)
    return table


def run_benchmark(  # noqa: C901, WPS210, WPS231
    models: List[str],
    benchmark_problems_path: str = "benchmark/problems.jsonl",
    evaluator: Optional[str] = None,
    print_table: bool = False,
) -> Dict:
    """
    Benchmarks selected language models across diverse cloud providers.

    The generated table presents comprehensive results, including total token count,
    latency, and throughput metrics for efficient comparison. For instance,
    choosing 5 models supported by 3 providers results in a detailed 15-entry
    benchmark summary.

    :param models: List of models to benchmark.
    :param benchmark_problems_path: JSONL file path with problems.
    :param evaluator: Model used to evaluate the answers. Should be SOTA like gpt-4.
    :param print_table: Whether to print the table or not.
    :raises ValueError: If no models are provided to benchmark.
    :return: Dict with benchmarking results of model on providers.
    """
    if not models:
        raise ValueError("No models provided to benchmark")
    problems = load_problems(benchmark_problems_path)

    model_results: Dict[str, Any] = {}
    traversed_providers: Dict[str, BaseCompletionProvider] = {}
    logger.info("Currently benchmarking: ")
    for provider_name, provider_class in PROVIDER_CLASSES.items():
        for model_name in models:
            if model_name.lower() in provider_class.supported_models:
                logger.info(f"{model_name} on {provider_name}")
                provider_obj = get_provider_obj(provider_name, traversed_providers)
                completion_results = get_completion_results(
                    provider_obj,
                    model_name,
                    problems,
                )
                if completion_results is None:
                    logger.error(f"{model_name} on {provider_name} was skipped")
                    continue

                model_results.setdefault(model_name, {})[
                    provider_name
                ] = calculate_results(
                    completion_results,
                    model_name,
                    provider_obj,
                )
                add_cost_info(
                    model_results,
                    model_name,
                    provider_name,
                    provider_obj,
                )
        logger.info("--------------------")
    if evaluator:
        evaluator_provider = get_evaluator_provider(evaluator)
        for provider_results in model_results.values():
            for provider_data in provider_results.values():
                provider_data["score"] = calculate_score(
                    evaluator,
                    evaluator_provider,
                    provider_data,
                    problems,
                )
    table = create_table(model_results, evaluator)
    if print_table:
        print(table)  # noqa: WPS421
    return model_results


async def get_or_create_endpoint(  # noqa: D103
    mdl_id,
    provider_id,
    endpoint_dao,
    model_name,
    provider_name,
):
    endpoint_id = await get_endpoint(
        mdl_id=mdl_id,
        provider_id=provider_id,
        endpoint_dao=endpoint_dao,
    )
    if not endpoint_id:
        logger.info(
            f"No endpoints found for {model_name}({mdl_id}), "
            f"{provider_name}({provider_id}), creating new endpoint",
        )
        endpoint_obj = EndpointModelRequest(
            mdl_id=mdl_id,
            provider_id=provider_id,
        )
        await create_endpoint_model(
            new_endpoint_object=endpoint_obj,
            endpoint_dao=endpoint_dao,
        )
        logger.info("Endpoint created")
        endpoint_id = await get_endpoint(
            mdl_id=mdl_id,
            provider_id=provider_id,
            endpoint_dao=endpoint_dao,
        )
    elif len(endpoint_id) > 1:
        logger.info(
            f"Multiple endpoints found for {model_name}, {provider_name}, "
            f"using first: {endpoint_id[0].id}",
        )
    return endpoint_id[0].id


async def put_data_to_db(  # noqa: D103, WPS211, WPS210
    data,
    model_name,
    provider_name,
    async_session,
):
    async with async_session() as session:
        provider_dao = ProviderDAO(session)
        model_dao = ModelDAO(session)
        endpoint_dao = EndpointDAO(session)
        datapoint_dao = DatapointDAO(session)
        logger.info(f"{provider_name}, {model_name}")
        provider_id = await get_provider(name=provider_name, provider_dao=provider_dao)
        mdl_id = await get_model(mdl_code=model_name, model_dao=model_dao)

        endpoint_id = await get_or_create_endpoint(
            mdl_id[0].id,
            provider_id[0].id,
            endpoint_dao,
            model_name,
            provider_name,
        )
        datapoint_obj = DatapointModelRequest(
            endpoint_id=endpoint_id,
            measured_at=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            metric_name=data["metric_name"],
            value=data["value"],
        )
        await create_datapoint_model(
            new_datapoint_object=datapoint_obj,
            datapoint_dao=datapoint_dao,
        )
        await session.commit()
        logger.info(
            f"Datapoint added for {model_name}, {provider_name} "
            f"(endpoint_id: {endpoint_id})",
        )


async def process_benchmarking_results(  # noqa: D103, WPS210, WPS231
    benchmarking_results,
    metrics_to_push,
):
    user = os.getenv("ORCHESTRA_DB_USER", "orchestra")
    password = os.getenv("ORCHESTRA_DB_PASS", "orchestra")
    host = os.getenv("ORCHESTRA_DB_HOST", "localhost")
    port = os.getenv("ORCHESTRA_DB_PORT", "5432")
    db_name = os.getenv("ORCHESTRA_DB_BASE", "orchestra")
    db_url = f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db_name}"  # noqa: WPS221, E501
    logger.info(db_url)
    engine = create_async_engine(db_url)
    async_session = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    tasks = []
    for model_name, provider_results in benchmarking_results.items():
        for provider_name, provider_data in provider_results.items():
            for metric_name, value in provider_data.items():
                if metric_name in metrics_to_push:
                    data = {
                        "metric_name": metric_name,
                        "value": round(value, 2) if isinstance(value, float) else value,
                    }
                    task = put_data_to_db(
                        data,
                        model_name,
                        provider_name,
                        async_session,
                    )
                    tasks.append(task)
    total_write_count = len(tasks)
    logger.info(f"Pushing {total_write_count} benchmarking results entries to db")
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    model_list = [
        model
        for provider in PROVIDER_CLASSES.values()
        for model in provider.supported_models.keys()
    ]
    model_list = list(set(model_list))
    benchmarking_results = run_benchmark(model_list, print_table=True)
    logger.info(benchmarking_results)
    metrics_to_push = [
        "output_toks_per_sec",
        "context_window",
        "cold_start_latency",
        "input_cost_llm",
        "output_cost_llm",
        "input_cost_llm_per_character",
        "output_cost_llm_per_character",
    ]
    asyncio.run(
        process_benchmarking_results(
            benchmarking_results,
            metrics_to_push,
        ),
    )
