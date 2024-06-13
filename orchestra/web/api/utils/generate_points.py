from functools import cmp_to_key
import math
import copy
from statistics import mean
import numpy as np
import scipy


ROUTER_POINT = "router_2.12e-01_5.00e-04_2.78e-04"


def reorganize_data(raw_responses):
    datasets = dict()
    _metrics = copy.deepcopy(metrics)
    for response in raw_responses:
        it = response.input_tokens if response.input_tokens is not None else 1
        ot = response.output_tokens if response.output_tokens is not None else 1
        relevant_info = {
            "prompt": response.prompt,
            "mdl_name": response.mdl_name,
            "score": float(response.gt_score),
            "pred": float(response.score),
            "input_tokens": float(it),
            "output_tokens": float(ot),
        }
        for endpoint in _metrics.keys():
            if response.mdl_name in endpoint:
                _metrics[endpoint]["input_tokens"] = (
                    _metrics[endpoint].get("input_tokens", 0)
                    + relevant_info["input_tokens"]
                )
                _metrics[endpoint]["output_tokens"] = (
                    _metrics[endpoint].get("output_tokens", 0)
                    + relevant_info["output_tokens"]
                )
        if "pred" in relevant_info and relevant_info["pred"] != "null":
            if response.dataset_name in datasets:
                datasets[response.dataset_name].append(relevant_info)
            else:
                datasets[response.dataset_name] = [relevant_info]

    final_results = dict()
    for dataset in datasets:
        prompts = dict()
        for benchmark in datasets[dataset]:
            if benchmark["prompt"] in prompts:
                prompts[benchmark["prompt"]][
                    f"{benchmark['mdl_name']}_score"
                ] = benchmark["score"]
                prompts[benchmark["prompt"]][
                    f"{benchmark['mdl_name']}_pred"
                ] = benchmark["pred"]
            else:
                prompts[benchmark["prompt"]] = {
                    f"{benchmark['mdl_name']}_score": benchmark["score"],
                    f"{benchmark['mdl_name']}_pred": benchmark["pred"],
                }
        dataset_results = []
        for prompt in prompts:
            dataset_results.append({"prompt": prompt, **prompts[prompt]})
        final_results[dataset] = dataset_results

    return _metrics, final_results


def compute_cost(endpoint_metrics):
    input_cost = endpoint_metrics["input_cost"] * endpoint_metrics.get(
        "input_tokens", 1
    )
    output_cost = endpoint_metrics["output_cost"] * endpoint_metrics.get(
        "output_tokens", 1
    )
    total_tokens = endpoint_metrics.get("input_tokens", 1) + endpoint_metrics.get(
        "output_tokens", 1
    )
    weighted_cost = (input_cost + output_cost) / total_tokens
    return weighted_cost


def generate_router_points(data, _metrics):
    final_scores = dict()
    endpoint_to_model = lambda endpoint: (
        endpoint.split("@")[0].replace("gpt-4-turbo", "gpt-4-0125-preview") + "_pred"
    )
    endpoints = _metrics.keys()

    endpoint_costs = {e: compute_cost(_metrics[e]) for e in endpoints}
    endpoint_ttft = {e: _metrics[e]["ttft"] for e in endpoints}
    endpoint_itl = {e: _metrics[e]["itl"] for e in endpoints}

    def get_mean(endpoint):
        scores = [
            prompt[endpoint_to_model(endpoint)]
            for prompt in prompt_list
            if endpoint_to_model(endpoint) in prompt
        ]
        if len(scores):
            return mean(scores)
        return None

    for dataset in data:
        prompt_list = data[dataset]
        endpoint_scores = {e: get_mean(e) for e in endpoints}
        endpoint_scores = {
            endpoint: score
            for endpoint, score in endpoint_scores.items()
            if score != None
        }
        current_endpoints = list(endpoint_scores.keys())
        if not current_endpoints:
            final_scores[dataset] = []
            continue

        cost_multiplier = mean(
            [
                endpoint_scores[endpoint] / endpoint_costs[endpoint]
                for endpoint in current_endpoints
            ]
        )
        ttft_multiplier = mean(
            [
                endpoint_scores[endpoint] / endpoint_ttft[endpoint]
                for endpoint in current_endpoints
            ]
        )
        itl_multiplier = mean(
            [
                endpoint_scores[endpoint] / endpoint_itl[endpoint]
                for endpoint in current_endpoints
            ]
        )

        num = 20
        a_values, b_values, c_values = [], [], []
        a_start, b_start, c_start = (
            math.log10(cost_multiplier * 3),
            math.log10(ttft_multiplier * 3),
            math.log10(itl_multiplier * 3),
        )
        end = math.log10(1e-8)
        a_step, b_step, c_step = (
            (end - a_start) / (num - 1),
            (end - b_start) / (num - 1),
            (end - b_start) / (num - 1),
        )
        values = [
            (
                10 ** (a_start + a_step * i),
                10 ** (b_start + b_step * i),
                10 ** (c_start + c_step * i),
            )
            for i in range(num)
        ]
        a_values = [value[0] for value in values]
        b_values = [value[1] for value in values]
        c_values = [value[2] for value in values]

        router_points = []
        for a_idx, a in enumerate(a_values):
            for b_idx, b in enumerate(b_values):
                for c_idx, c in enumerate(c_values):
                    a, b, c = round(a, 7), round(b, 7), round(c, 7)
                    prompt_scores = []
                    for i in range(len(prompt_list)):
                        endpoint_scores = {}
                        for endpoint in current_endpoints:
                            model = endpoint_to_model(endpoint)
                            if model in prompt_list[i]:
                                endpoint_scores[endpoint] = (
                                    prompt_list[i][model]
                                    - a * endpoint_costs[endpoint]
                                    - b * endpoint_ttft[endpoint]
                                    - c * endpoint_itl[endpoint]
                                )
                        prompt_scores.append(endpoint_scores)

                    prompt_max = []
                    for i, score in enumerate(prompt_scores):
                        max_endpoint, max_objective = None, None
                        for endpoint, objective in score.items():
                            if max_endpoint == None or objective > max_objective:
                                max_endpoint, max_objective = endpoint, objective
                        prompt_max.append(
                            {
                                "endpoint": max_endpoint,
                                "score": prompt_list[i][
                                    endpoint_to_model(max_endpoint).replace("_pred", "")
                                    + "_score"
                                ],
                            }
                        )

                    router_scores = {}
                    router_counts = {}
                    router_cost = {}
                    router_ttft = {}
                    router_itl = {}
                    for prompt in prompt_max:
                        if prompt["endpoint"] not in router_counts:
                            router_counts[prompt["endpoint"]] = 1
                            router_scores[prompt["endpoint"]] = prompt["score"]
                            router_cost[prompt["endpoint"]] = endpoint_costs[
                                prompt["endpoint"]
                            ]
                            router_ttft[prompt["endpoint"]] = endpoint_ttft[
                                prompt["endpoint"]
                            ]
                            router_itl[prompt["endpoint"]] = endpoint_itl[
                                prompt["endpoint"]
                            ]
                        else:
                            router_counts[prompt["endpoint"]] += 1
                            router_scores[prompt["endpoint"]] += prompt["score"]
                            router_cost[prompt["endpoint"]] += endpoint_costs[
                                prompt["endpoint"]
                            ]
                            router_ttft[prompt["endpoint"]] += endpoint_ttft[
                                prompt["endpoint"]
                            ]
                            router_itl[prompt["endpoint"]] += endpoint_itl[
                                prompt["endpoint"]
                            ]

                    router_scores = {
                        endpoint: router_scores[endpoint] / router_counts[endpoint]
                        for endpoint in router_scores
                    }
                    router_cost = {
                        endpoint: router_cost[endpoint] / router_counts[endpoint]
                        for endpoint in router_cost
                    }
                    router_ttft = {
                        endpoint: router_ttft[endpoint] / router_counts[endpoint]
                        for endpoint in router_ttft
                    }
                    router_itl = {
                        endpoint: router_itl[endpoint] / router_counts[endpoint]
                        for endpoint in router_itl
                    }
                    total_weight = sum(router_counts.values())
                    final_score = (
                        sum(
                            [
                                router_scores[endpoint] * router_counts[endpoint]
                                for endpoint in router_scores
                            ]
                        )
                        / total_weight
                    )
                    final_cost = (
                        sum(
                            [
                                router_cost[endpoint] * router_counts[endpoint]
                                for endpoint in router_cost
                            ]
                        )
                        / total_weight
                    )
                    final_ttft = (
                        sum(
                            [
                                router_ttft[endpoint] * router_counts[endpoint]
                                for endpoint in router_ttft
                            ]
                        )
                        / total_weight
                    )
                    final_itl = (
                        sum(
                            [
                                router_itl[endpoint] * router_counts[endpoint]
                                for endpoint in router_itl
                            ]
                        )
                        / total_weight
                    )
                    to_label = lambda value: "{:.{}e}".format(abs(value), 2)
                    a_label, b_label, c_label = to_label(a), to_label(b), to_label(c)
                    breakdown = {
                        val[0]: val[1]
                        for val in sorted(
                            list(
                                {
                                    endpoint: round(
                                        router_counts[endpoint] / total_weight, 2
                                    )
                                    for endpoint in router_counts
                                }.items()
                            ),
                            key=cmp_to_key(lambda x, y: x[1] > y[1]),
                        )[:10]
                    }
                    router_points.append(
                        {
                            "model": f"router_{a_label}_{b_label}_{c_label}",
                            "quality": final_score,
                            "cost": final_cost,
                            "ttft": final_ttft,
                            "itl": final_itl,
                            "breakdown": breakdown,
                        }
                    )
        final_scores[dataset] = router_points
    return final_scores


def get_point_solutions(data):
    final_scores = dict()

    for dataset in data:
        prompt_list = data[dataset]
        models = [
            key.replace("_score", "")
            for key in prompt_list[0].keys()
            if key != "prompt" and "pred" not in key
        ]
        model_scores = dict()
        model_counts = dict()

        for model in models:
            model_scores[model] = 0
            model_counts[model] = 0
        for prompt in prompt_list:
            for model in models:
                if model + "_score" in prompt:
                    model_scores[model] += prompt[model + "_score"]
                    model_counts[model] += 1

        dataset_scores = []
        for model in models:
            dataset_scores.append(
                {
                    "model": model,
                    "quality": round(model_scores[model] / model_counts[model], 2),
                }
            )

        final_scores[dataset] = dataset_scores

    return final_scores


def get_point_solutions_full(data, _metrics):
    final_scores = dict()
    endpoints = _metrics.keys()
    endpoint_costs = {e: compute_cost(_metrics[e]) for e in endpoints}
    endpoint_ttft = {e: _metrics[e]["ttft"] for e in endpoints}
    endpoint_itl = {e: _metrics[e]["itl"] for e in endpoints}

    for dataset in data:
        prompt_list = data[dataset]
        models = [
            key.replace("_score", "")
            for key in prompt_list[0].keys()
            if key != "prompt" and "pred" not in key
        ]
        model_scores = dict()
        model_counts = dict()

        for model in models:
            model_scores[model] = 0
            model_counts[model] = 0
        for prompt in prompt_list:
            for model in models:
                if model + "_score" in prompt:
                    model_scores[model] += prompt[model + "_score"]
                    model_counts[model] += 1

        dataset_scores = []
        for endpoint in endpoints:
            model, provider = endpoint.split("@")
            if model in model_scores:
                dataset_scores.append(
                    {
                        "model": model,
                        "provider": provider,
                        "quality": round(model_scores[model] / model_counts[model], 2),
                        "cost": endpoint_costs[endpoint],
                        "ttft": endpoint_ttft[endpoint],
                        "itl": endpoint_itl[endpoint],
                    }
                )

        final_scores[dataset] = dataset_scores

    return final_scores


def prune_router_points(router_points):
    final_pruned_points = dict()
    for dataset in router_points:
        points = [
            [point["quality"], point["cost"], point["ttft"], point["itl"]]
            for point in router_points[dataset]
        ]
        if points:
            try:
                convex_hull = scipy.spatial.ConvexHull(points)
                vertices = convex_hull.vertices
                convex_points = [router_points[dataset][vertex] for vertex in vertices]
            except scipy.spatial._qhull.QhullError:
                final_pruned_points[dataset] = []
                continue

            metrics = np.array(
                [
                    [point["quality"], point["cost"], point["ttft"], point["itl"]]
                    for point in convex_points
                ]
            )
            normalized_metrics = (metrics - np.mean(metrics, axis=0)) / np.std(
                metrics, axis=0
            )

            num = normalized_metrics.shape[0]
            dist_matrix = np.zeros((num, num))

            for i in range(num):
                for j in range(num):
                    dist = np.linalg.norm(normalized_metrics[i] - normalized_metrics[j])
                    dist_matrix[i][j] = dist
                    dist_matrix[j][i] = dist

            deleted = []
            threshold = 0.8
            for i in range(num):
                if i not in deleted:
                    for j in range(num):
                        if (
                            dist_matrix[i][j] < threshold
                            and i != j
                            and j not in deleted
                        ):
                            deleted.append(j)

            pruned_points = [
                point for i, point in enumerate(convex_points) if i not in deleted
            ]
        else:
            pruned_points = points
        final_pruned_points[dataset] = pruned_points

    return final_pruned_points


def generate_and_prune_points(dataset_name, raw_responses):
    _metrics, organized_responses = reorganize_data(raw_responses)
    point_solutions = get_point_solutions(organized_responses)
    point_solutions_full = get_point_solutions_full(organized_responses, _metrics)
    router_points = generate_router_points(organized_responses, _metrics)
    chosen_point = None
    if dataset_name == "hermes":
        get_list = lambda p1, p2: (p1.split("_")[1:], p2.split("_")[1:])
        get_dist = lambda p1, p2: sum(
            abs(float(p1_i) - float(p2_i)) for p1_i, p2_i in zip(p1, p2)
        )
        dist = [
            get_dist(*get_list(point["model"], ROUTER_POINT))
            for point in router_points["hermes"]
        ]
        min_idx = np.argmin(dist)
        chosen_point = router_points["hermes"][min_idx]
        chosen_point["model"] = ROUTER_POINT
    pruned_router_points = prune_router_points(router_points)
    if chosen_point:
        pruned_router_points["hermes"].append(chosen_point)
    for dataset in pruned_router_points:
        pruned_router_points[dataset] = pruned_router_points[dataset]
    final_points = {}
    for dataset in pruned_router_points:
        final_points[dataset] = {
            "point_solutions": point_solutions[dataset],
            "point_solutions_full": point_solutions_full[dataset],
            "router": pruned_router_points[dataset],
        }
    return final_points


metrics = {
    "claude-3-haiku@anthropic": {
        "cost": 1.25,
        "ttft": 641.8530669999427,
        "itl": 7.320965663317298,
        "input_cost": 0.25,
        "output_cost": 1.25,
    },
    "claude-3-opus@anthropic": {
        "cost": 75,
        "ttft": 2591.2904499998604,
        "itl": 34.60999395530718,
        "input_cost": 15,
        "output_cost": 75,
    },
    "claude-3-sonnet@anthropic": {
        "cost": 15,
        "ttft": 1151.3890589999392,
        "itl": 12.03211326126186,
        "input_cost": 3,
        "output_cost": 15,
    },
    "deepseek-coder-33b-instruct@together-ai": {
        "cost": 0.8,
        "ttft": 350.50168700001905,
        "itl": 27.84690674999979,
        "input_cost": 0.8,
        "output_cost": 0.8,
    },
    "gemma-7b-it@anyscale": {
        "cost": 0.15,
        "ttft": 1176.8055530000083,
        "itl": 23.80950663414629,
        "input_cost": 0.15,
        "output_cost": 0.15,
    },
    "gemma-7b-it@together-ai": {
        "cost": 0.2,
        "ttft": 355.0849639999569,
        "itl": 10.75794840476246,
        "input_cost": 0.2,
        "output_cost": 0.2,
    },
    "gemma-7b-it@fireworks-ai": {
        "cost": 0.2,
        "ttft": 596.3048590000426,
        "itl": 4.905699351647504,
        "input_cost": 0.2,
        "output_cost": 0.2,
    },
    "gemma-7b-it@lepton-ai": {
        "cost": 0.1,
        "ttft": 1013.7901719999718,
        "itl": 10.638872657407488,
        "input_cost": 0.1,
        "output_cost": 0.1,
    },
    "gemma-7b-it@deepinfra": {
        "cost": 0.13,
        "ttft": 1106.7886140000383,
        "itl": 18.87030848101254,
        "input_cost": 0.13,
        "output_cost": 0.13,
    },
    "gpt-3.5-turbo@openai": {
        "cost": 1.5,
        "ttft": 400.21933599996373,
        "itl": 27.26199600000041,
        "input_cost": 0.5,
        "output_cost": 1.5,
    },
    "gpt-4-turbo@openai": {
        "cost": 30,
        "ttft": 635.7509760000539,
        "itl": 42.31438732535859,
        "input_cost": 10,
        "output_cost": 30,
    },
    "gpt-4@openai": {
        "cost": 45,
        "ttft": 760,
        "itl": 46.05,
        "input_cost": 30,
        "output_cost": 60,
    },
    "gpt-4o@openai": {
        "cost": 7.5,
        "ttft": 589,
        "itl": 20.05,
        "input_cost": 5,
        "output_cost": 15,
    },
    "llama-3-70b-chat@fireworks-ai": {
        "cost": 0.9,
        "ttft": 469.78,
        "itl": 6.58,
        "input_cost": 0.9,
        "output_cost": 0.9,
    },
    "llama-3-70b-chat@together-ai": {
        "cost": 0.9,
        "ttft": 466.28,
        "itl": 5.38,
        "input_cost": 0.9,
        "output_cost": 0.9,
    },
    "llama-3-8b-chat@fireworks-ai": {
        "cost": 0.2,
        "ttft": 355.48,
        "itl": 3.06,
        "input_cost": 0.2,
        "output_cost": 0.2,
    },
    "llama-3-8b-chat@together-ai": {
        "cost": 0.2,
        "ttft": 1035.13,
        "itl": 3.98,
        "input_cost": 0.2,
        "output_cost": 0.2,
    },
    "mistral-large@mistral-ai": {
        "cost": 24,
        "ttft": 439.49507400009225,
        "itl": 54.14005861235942,
        "input_cost": 8,
        "output_cost": 24,
    },
    "mistral-small@mistral-ai": {
        "cost": 6,
        "ttft": 371.52690400000665,
        "itl": 18.000006300000376,
        "input_cost": 2,
        "output_cost": 6,
    },
    "mixtral-8x7b-instruct-v0.1@together-ai": {
        "cost": 0.6,
        "ttft": 405.11531099997455,
        "itl": 4.174361656626742,
        "input_cost": 0.6,
        "output_cost": 0.6,
    },
    "mixtral-8x7b-instruct-v0.1@octoai": {
        "cost": 0.5,
        "ttft": 1164.472783000008,
        "itl": 24.274311994623353,
        "input_cost": 0.3,
        "output_cost": 0.5,
    },
    "mixtral-8x7b-instruct-v0.1@replicate": {
        "cost": 1,
        "ttft": 887.903352999956,
        "itl": 15.394309863636439,
        "input_cost": 0.3,
        "output_cost": 1,
    },
    "mixtral-8x7b-instruct-v0.1@mistral-ai": {
        "cost": 0.7,
        "ttft": 352.0689869999387,
        "itl": 12.773902387097081,
        "input_cost": 0.7,
        "output_cost": 0.7,
    },
    "mixtral-8x7b-instruct-v0.1@anyscale": {
        "cost": 0.5,
        "ttft": 1749.3290439999782,
        "itl": 34.07672297029734,
        "input_cost": 0.5,
        "output_cost": 0.5,
    },
    "mixtral-8x7b-instruct-v0.1@fireworks-ai": {
        "cost": 0.5,
        "ttft": 324.21352400001524,
        "itl": 3.380061226190194,
        "input_cost": 0.5,
        "output_cost": 0.5,
    },
    "mixtral-8x7b-instruct-v0.1@lepton-ai": {
        "cost": 0.5,
        "ttft": 872.5847029999159,
        "itl": 12.631626471590804,
        "input_cost": 0.5,
        "output_cost": 0.5,
    },
    "mixtral-8x7b-instruct-v0.1@deepinfra": {
        "cost": 0.27,
        "ttft": 1130.8457239999825,
        "itl": 15.669842747059405,
        "input_cost": 0.27,
        "output_cost": 0.27,
    },
    "mixtral-8x7b-instruct-v0.1@aws-bedrock": {
        "cost": 0.7,
        "ttft": 713.9613250001275,
        "itl": 15.034942066296473,
        "input_cost": 0.45,
        "output_cost": 0.7,
    },
    "mixtral-8x22b-instruct-v0.1@mistral-ai": {
        "cost": 3,
        "ttft": 135,
        "itl": 12.25,
        "input_cost": 2,
        "output_cost": 6,
    },
    "mixtral-8x22b-instruct-v0.1@fireworks-ai": {
        "cost": 0.9,
        "ttft": 314,
        "itl": 11.63,
        "input_cost": 0.9,
        "output_cost": 0.9,
    },
    "mixtral-8x22b-instruct-v0.1@together-ai": {
        "cost": 1.2,
        "ttft": 840,
        "itl": 21.88,
        "input_cost": 1.2,
        "output_cost": 1.2,
    },
    "mixtral-8x22b-instruct-v0.1@deepinfra": {
        "cost": 0.65,
        "ttft": 950,
        "itl": 19.91,
        "input_cost": 0.65,
        "output_cost": 0.65,
    },
    "pplx-70b-chat@perplexity-ai": {
        "cost": 1.225,
        "ttft": 982,
        "itl": 24.68,
        "input_cost": 0.7,
        "output_cost": 2.8,
    },
    "pplx-7b-chat@perplexity-ai": {
        "cost": 0.1225,
        "ttft": 959.25,
        "itl": 7.78,
        "input_cost": 0.07,
        "output_cost": 0.28,
    },
    "mistral-medium@mistral-ai": {
        "cost": 0.7,  #
        "ttft": 532.12,
        "itl": 41.92,
        "input_cost": 2.7,
        "output_cost": 8.1,
    },
    "gemma-2b-it@together-ai": {
        "cost": 0.1,
        "ttft": 934.97,
        "itl": 11.03,
        "input_cost": 0.1,
        "output_cost": 0.1,
    },
    "gemma-2b-it@together-ai": {
        "cost": 0.1,
        "ttft": 934.97,
        "itl": 11.03,
        "input_cost": 0.1,
        "output_cost": 0.1,
    },
    "yi-34b-chat@together-ai": {
        "cost": 0.8,
        "ttft": 409.41,
        "itl": 14.49,
        "input_cost": 0.8,
        "output_cost": 0.8,
    },
    "yi-34b-chat@deepinfra": {
        "cost": 0.6,
        "ttft": 820.06,
        "itl": 42.03,
        "input_cost": 0.6,
        "output_cost": 0.6,
    },
    "codellama-34b-instruct@deepinfra": {
        "cost": 0.6,
        "ttft": 826.18,
        "itl": 32.75,
        "input_cost": 0.6,
        "output_cost": 0.6,
    },
    "codellama-34b-instruct@fireworks-ai": {
        "cost": 0.9,
        "ttft": 473.06,
        "itl": 10.09,
        "input_cost": 0.9,
        "output_cost": 0.9,
    },
    "codellama-34b-instruct@octoai": {
        "cost": 0.65,  #
        "ttft": 628.3,
        "itl": 8.03,
        "input_cost": 0.5,
        "output_cost": 1,
    },
    "codellama-34b-instruct@together-ai": {
        "cost": 0.8,  #
        "ttft": 1178.87,
        "itl": 23.67,
        "input_cost": 0.8,
        "output_cost": 0.8,
    },
    "codellama-34b-instruct@perplexity-ai": {
        "cost": 0.5,  #
        "ttft": 1005.11,
        "itl": 13.94,
        "input_cost": 0.35,
        "output_cost": 1.4,
    },
    "codellama-34b-instruct@anyscale": {
        "cost": 1,
        "ttft": 1447.77,
        "itl": 31.31,
        "input_cost": 1,
        "output_cost": 1,
    },
    "codellama-13b-instruct@octoai": {
        "cost": 0.65,  #
        "ttft": 630.77,
        "itl": 7.89,
        "input_cost": 0.2,
        "output_cost": 0.5,
    },
    "codellama-13b-instruct@together-ai": {
        "cost": 0.23,
        "ttft": 507.27,
        "itl": 14.39,
        "input_cost": 0.23,
        "output_cost": 0.23,
    },
    "codellama-7b-instruct@octoai": {
        "cost": 0.65,  #
        "ttft": 635.72,
        "itl": 7.69,
        "input_cost": 0.1,
        "output_cost": 0.25,
    },
    "codellama-7b-instruct@together-ai": {
        "cost": 0.2,
        "ttft": 365.82,
        "itl": 13.22,
        "input_cost": 0.2,
        "output_cost": 0.2,
    },
    "mistral-7b-instruct-v0.1@anyscale": {
        "cost": 0.15,
        "ttft": 2103.77,
        "itl": 33.54,
        "input_cost": 0.15,
        "output_cost": 0.15,
    },
    "mistral-7b-instruct-v0.1@together-ai": {
        "cost": 0.2,
        "ttft": 560.2,
        "itl": 6.64,
        "input_cost": 0.2,
        "output_cost": 0.2,
    },
    "mistral-7b-instruct-v0.1@fireworks-ai": {
        "cost": 0.2,
        "ttft": 509.19,
        "itl": 8.7,
        "input_cost": 0.2,
        "output_cost": 0.2,
    },
    "mistral-7b-instruct-v0.2@together-ai": {
        "cost": 0.2,
        "ttft": 936.34,
        "itl": 8.92,
        "input_cost": 0.2,
        "output_cost": 0.2,
    },
    "mistral-7b-instruct-v0.2@mistral-ai": {
        "cost": 0.25,
        "ttft": 698.41,
        "itl": 16.51,
        "input_cost": 0.25,
        "output_cost": 0.25,
    },
    "mistral-7b-instruct-v0.2@replicate": {
        "cost": 0.1,
        "ttft": 889.15,
        "itl": 6.7,
        "input_cost": 0.05,
        "output_cost": 0.25,
    },
    "mistral-7b-instruct-v0.2@octoai": {
        "cost": 0.2,
        "ttft": 804.27,
        "itl": 12.48,
        "input_cost": 0.1,
        "output_cost": 0.25,
    },
    "mistral-7b-instruct-v0.2@fireworks-ai": {
        "cost": 0.2,
        "ttft": 848.85,
        "itl": 4.02,
        "input_cost": 0.2,
        "output_cost": 0.2,
    },
    "llama-2-7b-chat@anyscale": {
        "cost": 0.15,
        "ttft": 1782.82,
        "itl": 51.56,
        "input_cost": 0.15,
        "output_cost": 0.15,
    },
    "llama-2-7b-chat@together-ai": {
        "cost": 0.2,
        "ttft": 390.79,
        "itl": 12.98,
        "input_cost": 0.2,
        "output_cost": 0.2,
    },
    "llama-2-7b-chat@replicate": {
        "cost": 0.1,
        "ttft": 870.3,
        "itl": 2.43,
        "input_cost": 0.05,
        "output_cost": 0.25,
    },
    "llama-2-7b-chat@fireworks-ai": {
        "cost": 0.2,
        "ttft": 436.99,
        "itl": 4.35,
        "input_cost": 0.2,
        "output_cost": 0.2,
    },
    "llama-2-7b-chat@lepton-ai": {
        "cost": 0.1,
        "ttft": 1386.99,
        "itl": 6.31,
        "input_cost": 0.1,
        "output_cost": 0.1,
    },
    "llama-2-7b-chat@deepinfra": {
        "cost": 0.13,
        "ttft": 1194.73,
        "itl": 11.45,
        "input_cost": 0.13,
        "output_cost": 0.13,
    },
    "llama-2-13b-chat@anyscale": {
        "cost": 0.25,
        "ttft": 1828.87,
        "itl": 69.31,
        "input_cost": 0.25,
        "output_cost": 0.25,
    },
    "llama-2-13b-chat@together-ai": {
        "cost": 0.23,
        "ttft": 1031.49,
        "itl": 9.84,
        "input_cost": 0.23,
        "output_cost": 0.23,
    },
    "llama-2-13b-chat@replicate": {
        "cost": 0.2,
        "ttft": 1009.7,
        "itl": 11.77,
        "input_cost": 0.1,
        "output_cost": 0.5,
    },
    "llama-2-13b-chat@fireworks-ai": {
        "cost": 0.2,
        "ttft": 508.26,
        "itl": 5.55,
        "input_cost": 0.2,
        "output_cost": 0.2,
    },
    "llama-2-13b-chat@lepton-ai": {
        "cost": 0.3,
        "ttft": 1164.06,
        "itl": 10.06,
        "input_cost": 0.3,
        "output_cost": 0.3,
    },
    "llama-2-13b-chat@deepinfra": {
        "cost": 0.22,
        "ttft": 675.47,
        "itl": 14.35,
        "input_cost": 0.22,
        "output_cost": 0.22,
    },
    "llama-2-70b-chat@anyscale": {
        "cost": 1,
        "ttft": 931.48,
        "itl": 33.1,
        "input_cost": 1,
        "output_cost": 1,
    },
    "llama-2-70b-chat@together-ai": {
        "cost": 0.9,
        "ttft": 1078.69,
        "itl": 17.94,
        "input_cost": 0.9,
        "output_cost": 0.9,
    },
    "llama-2-70b-chat@replicate": {
        "cost": 1.2,
        "ttft": 876.12,
        "itl": 14.71,
        "input_cost": 0.65,
        "output_cost": 2.75,
    },
    "llama-2-70b-chat@fireworks-ai": {
        "cost": 0.9,
        "ttft": 545.15,
        "itl": 8.3,
        "input_cost": 0.9,
        "output_cost": 0.9,
    },
    "llama-2-70b-chat@lepton-ai": {
        "cost": 0.8,
        "ttft": 1077.08,
        "itl": 30.35,
        "input_cost": 0.8,
        "output_cost": 0.8,
    },
    "llama-2-70b-chat@deepinfra": {
        "cost": 0.7,
        "ttft": 603.45,
        "itl": 24.7,
        "input_cost": 0.7,
        "output_cost": 0.7,
    },
}
