import json
import os
import re
import statistics
from typing import Any, Dict, List

from litellm import ModelResponse
from prettytable import PrettyTable
from providers.completion import PROVIDER_CLASSES
from providers.completion.base_completion_provider import BaseCompletionProvider


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
        completion_results.append(result)
    return completion_results


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
) -> Dict[str, Any]:
    return {
        "output_answers": [
            obj.choices[0].message.content for obj in completion_results
        ],
        "total_latency": sum(
            [result._response_ms for result in completion_results],  # noqa: WPS437
        ),
        "total_tokens": sum(
            [result.usage.completion_tokens for result in completion_results],
        ),
        "median_latency": statistics.median(
            [result._response_ms for result in completion_results],  # noqa: WPS437
        ),
    }


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
) -> PrettyTable:
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
    :return: Prettytable of tokens count, latency, throughput.
    """
    if not models:
        raise ValueError("No models provided to benchmark")
    problems = load_problems(benchmark_problems_path)

    model_results: Dict[str, Any] = {}
    traversed_providers: Dict[str, BaseCompletionProvider] = {}
    for provider, provider_class in PROVIDER_CLASSES.items():
        for model in models:
            if model.lower() in provider_class.supported_models:
                provider_obj = get_provider(provider, traversed_providers)
                completion_results = get_completion_results(
                    provider_obj,
                    model,
                    problems,
                )

                model_results.setdefault(model, {})[provider] = calculate_results(
                    completion_results,
                )
                model_results[model][provider]["toks/sec"] = (
                    model_results[model][provider]["total_tokens"]
                    * 1000
                    / model_results[model][provider]["total_latency"]
                )

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
    return table


if __name__ == "__main__":
    run(["llama-2-7b-chat", "mistral-7b-instruct-v0.1"], print_table=True)
