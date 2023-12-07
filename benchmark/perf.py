from providers.completion import PROVIDER_CLASSES
from typing import List, Tuple
import json
import re
import statistics
from prettytable import PrettyTable
import os


def evaluate_answers(
    evaluator_model, provider, query, ground_truth, answer
) -> int:
    # TODO: need to improve the system prompt
    system = \
    """
    You are given a problem and student's solution. If the correct answer is provided use it, otherwise first think about the solution yourself, then score the student's solution with one of these scores:
    0 - Student provided incorrect or no solution
    3 - Student provided correct solution

    Your output should be using always this template:
    Score: #
    """
    if ground_truth:
        prompt = f"Problem: {query}\nCorrect answer: {ground_truth}\nStudent solution: {answer}"
    else:
        prompt = f"Problem: {query}\nStudent solution: {answer}"
        
    evaluator_result = provider.complete(evaluator_model, [{"content": system, "role": "system"}, {"content": prompt, "role": "user"}])
    found = re.search(r"Score: (\d+)", evaluator_result.choices[0].message.content)
    if found:
        return int(found.group(1))
    else: # TODO: this is a hack, fix it
        return 0


def run(models, benchmark_problems_path='benchmark/problems.jsonl', evaluator="gpt-4"):
    """Benchmark the models on a list of problems."""
    assert models, "No models provided to benchmark"
    problems = []
    with open(benchmark_problems_path, 'r') as file:
        lines = file.readlines()
        for line in lines:
            data = json.loads(line)
            problems.append((data[0], data[1]))

    model_results = {}
    traversed_providers = {}
    for provider in PROVIDER_CLASSES:
        for model in models:
            if model.lower() in PROVIDER_CLASSES[provider].supported_models:
                if provider in traversed_providers:
                    _provider_obj = traversed_providers[provider]
                else:
                    _provider_obj = PROVIDER_CLASSES[provider]()
                    _provider_obj.set_api_key(api_key=str(os.getenv(f"{provider.upper()}_API_KEY")))
                    traversed_providers[provider] = _provider_obj # to avoid traversing the same provider twice

                completion_results = [_provider_obj.complete(model, [{"content": prompt[0], "role": "user"}]) for prompt in problems]
                model_results.setdefault(model, {})[provider] = {
                    'output_answers': [obj.choices[0].message.content for obj in completion_results], 
                    'total_latency': sum([result._response_ms for result in completion_results]),
                    'total_tokens': sum([result.usage.completion_tokens for result in completion_results]),
                    'median_latency': statistics.median([result._response_ms for result in completion_results]),
                }
                model_results[model][provider]['toks/sec'] = \
                    model_results[model][provider]['total_tokens']*1000 / model_results[model][provider]['total_latency']

    if evaluator:
        for provider in PROVIDER_CLASSES:
            if evaluator.lower() in PROVIDER_CLASSES[provider].supported_models:
                evaluator_provider = PROVIDER_CLASSES[provider]()
                evaluator_provider.set_api_key(api_key=str(os.getenv(f"{provider.upper()}_API_KEY")))
                break

        for model in model_results:
            for provider in model_results[model]:
                model_results[model][provider]['score'] = sum([
                        evaluate_answers(
                        evaluator.lower(), 
                        evaluator_provider, 
                        prompt[0], 
                        prompt[1], 
                        answer
                    ) for answer, prompt in zip(model_results[model][provider]['output_answers'], problems)])

    # with open('model_results.json', 'r', encoding='utf-8') as f:
    #     model_results = json.load(f)

    headers = [
        "Model",
        "Provider",
        "Tokens",
        "Latency (s)",
        "Speed (tokens/sec)",
        "QnA score",
    ]

    if not evaluator:
        headers.remove("QnA score")

    table = PrettyTable(headers)

    for model in model_results:
        for provider in model_results[model]:
            row_data = [
                model,
                provider,
                model_results[model][provider]["total_tokens"],
                f'{model_results[model][provider]["total_latency"]:.2f}',
                f'{model_results[model][provider]["toks/sec"]:.2f}',
            ]
            if evaluator:
                row_data.append(model_results[model][provider]["score"])
            table.add_row(row_data)
    return table


print(run(["llama-2-7b-chat", "mistral-7b-instruct-v0.1"]))
