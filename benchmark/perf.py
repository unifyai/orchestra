import json
import logging
import os
import re
import statistics
from typing import Any, Dict, List

import numpy as np
from litellm import ModelResponse
from prettytable import PrettyTable
from providers.completion import PRICING_PER_TOKENS, PROVIDER_CLASSES
from providers.completion.base_completion_provider import BaseCompletionProvider

logger = logging.getLogger(__name__)


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
    found = re.search(r"Score: (\d+)", evaluator_result.choices[0].message.content)
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


def get_provider(
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
        provider_obj.set_api_key(
            api_key=str(os.getenv(f"{provider_name.upper()}_API_KEY")),
        )
        traversed_providers[provider_name] = provider_obj  # noqa: WPS529
    return provider_obj


def get_completion_results(  # noqa: D103
    provider: BaseCompletionProvider,
    model: str,
    problems: List[tuple[str, str]],
) -> List[str]:
    completion_results = []
    for prompt in problems:
        result = provider.complete(
            model,
            [{"content": prompt[0], "role": "user"}],
        )
        if result is None:
            raise ValueError(f"{model} on {provider} threw an error during call")
        # handles cold-start skewing latency
        if result._response_ms > COLD_START_THRESHOLD:
            cold_start_latency = result._response_ms
            result = provider.complete(
                model,
                [{"content": prompt[0], "role": "user"}],
            )
        else:
            cold_start_latency = 0
        result.cold_start_latency = cold_start_latency
        completion_results.append(result)
    return completion_results


def get_cost(
    completion_results: List[ModelResponse],
    model: str,
    provider: BaseCompletionProvider,
    problems: List[tuple[str, str]],
) -> float:
    """
    Calculate the total cost incurred on running this benchmarking.

    :param completion_results: The completion results of the model.
    :param model: The model to calculate the cost of.
    :param provider: The provider object of the model.
    :param problems: The problems containing input prompts.
    :return: The cost of the model.
    """
    prompt_cost = 0
    completion_cost = 0
    for result, qna in zip(completion_results, problems):
        cost_data = provider.supported_models[model]["cost"]  # type: ignore
        if cost_data.get("per_character"):
            prompt_cost += (
                provider.get_billable_characters(  # type: ignore
                    qna[0],
                    model,
                )
                * cost_data["prompt"]
            )
            completion_cost += (
                provider.get_billable_characters(  # type: ignore
                    result.choices[0].message.content,
                    model,
                )
                * cost_data["completion"]
            )
        elif cost_data.get("per_second"):
            prompt_cost += (
                provider.hardware_pricing_per_sec[cost_data["hardware"]]  # type: ignore
                * result._response_ms
                / 1000
            )
        else:
            if cost_data.get("online"):
                prompt_cost += cost_data["online"]["charge_per_1000_requests"] / 1000
            prompt_cost += (
                result.usage.prompt_tokens * cost_data["prompt"] / PRICING_PER_TOKENS
            )
            completion_cost += (
                result.usage.completion_tokens
                * cost_data["completion"]
                / PRICING_PER_TOKENS
            )

    return prompt_cost + completion_cost


def get_evaluator_provider(evaluator: str) -> BaseCompletionProvider:  # noqa: D103
    for provider, provider_class in PROVIDER_CLASSES.items():
        if evaluator.lower() in provider_class.supported_models:
            evaluator_provider = provider_class()
            evaluator_provider.set_api_key(
                api_key=str(os.getenv(f"{provider.upper()}_API_KEY")),
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
        "total_tokens": sum(
            [result.usage.total_tokens for result in completion_results],
        ),
        "median_latency": statistics.median(
            [result._response_ms for result in completion_results],
        ),
    }
    cleaned_output["toks/sec"] = (
        cleaned_output["total_tokens"] * 1000 / cleaned_output["total_latency"]
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
                evaluator.lower(),
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


def add_cost_per_million_tokens(  # noqa: D103
    model_results: Dict[str, Any],
):
    for model_name, provider_results in model_results.items():
        for provider_name, provider_data in provider_results.items():
            model_results[model_name][provider_name]["cost_per_1m_toks"] = (
                provider_data["cost"]
                * PRICING_PER_TOKENS
                / provider_data["total_tokens"]
            )


def create_table(  # noqa: D103, WPS210
    model_results: Dict[str, Any],
    evaluator: str,
) -> PrettyTable:
    headers = [
        "Model",
        "Provider",
        "Tokens",
        "Latency (s)",
        "Speed (tokens/sec)",
        "Cost($)/1M tokens",
        "Context window",
        "Cold start",
    ]

    if evaluator:
        headers.append("QnA score")
    table = PrettyTable(headers)

    for model_name, provider_results in model_results.items():
        for provider_name, provider_data in provider_results.items():
            row_data = [
                model_name,
                provider_name,
                provider_data["total_tokens"],
                f'{provider_data["total_latency"]:.2f}',
                f'{provider_data["toks/sec"]:.2f}',
                f'{provider_data["cost_per_1m_toks"]:.2f}',
                provider_data["context_window"],
                provider_data["cold_start_latency"],
            ]
            if evaluator:
                row_data.append(provider_data["score"])
            table.add_row(row_data)
    return table


def run(  # noqa: C901, WPS210, WPS231
    models: List[str],
    benchmark_problems_path: str = "benchmark/problems.jsonl",
    evaluator: str = "gpt-4",
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
    :param evaluator: Model used to evaluate the answers.
    :param print_table: Whether to print the table or not.
    :raises ValueError: If no models are provided to benchmark.
    :return: Dict with benchmarking results of model on providers.
    """
    if not models:
        raise ValueError("No models provided to benchmark")
    problems = load_problems(benchmark_problems_path)

    model_results: Dict[str, Any] = {}
    traversed_providers: Dict[str, BaseCompletionProvider] = {}
    logging.info("Currently benchmarking: ")
    for provider_name, provider_class in PROVIDER_CLASSES.items():
        for model_name in models:
            if model_name.lower() in provider_class.supported_models:
                logging.info(provider_name, model_name)
                provider_obj = get_provider(provider_name, traversed_providers)
                completion_results = get_completion_results(
                    provider_obj,
                    model_name,
                    problems,
                )

                model_results.setdefault(model_name, {})[
                    provider_name
                ] = calculate_results(
                    completion_results,
                    model_name,
                    provider_obj,
                )
                model_results[model_name][provider_name]["cost"] = get_cost(
                    completion_results,
                    model_name,
                    provider_obj,
                    problems,
                )
        logging.info("")
    add_cost_per_million_tokens(model_results)
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


if __name__ == "__main__":
    run(["llama-2-7b-chat", "mistral-7b-instruct-v0.1"], print_table=True)
