import json
import math
from statistics import mean
import numpy as np
import scipy

from orchestra.web.api.utils.dynamic_routing import (
    get_endpoints_of,
    get_value_of,
    get_ttl_hash,
    default_models,
    default_providers,
)


ROUTER_POINT = "router_0.00e+00_0.00e+00_0.00e+00"


def reorganize_data(raw_responses):
    datasets = dict()
    for response in raw_responses:
        relevant_info = {
            "prompt": response.prompt,
            "mdl_name": response.mdl_name,
            "score": float(response.gt_score),
            "pred": float(response.score),
        }
        if relevant_info["pred"] and relevant_info["pred"] != "null":
            if response.dataset_name in datasets:
                datasets[response.dataset_name].append(relevant_info)
            else:
                datasets[response.dataset_name] = [relevant_info]

    final_results = dict()
    for dataset in datasets:
        prompts = dict()
        for benchmark in datasets[dataset]:
            if benchmark["prompt"] in prompts:
                prompts[benchmark["prompt"]][f"{benchmark['mdl_name']}_score"] = (
                    benchmark["score"]
                )
                prompts[benchmark["prompt"]][f"{benchmark['mdl_name']}_pred"] = (
                    benchmark["pred"]
                )
            else:
                prompts[benchmark["prompt"]] = {
                    f"{benchmark['mdl_name']}_score": benchmark["score"],
                    f"{benchmark['mdl_name']}_pred": benchmark["pred"],
                }
        dataset_results = []
        for prompt in prompts:
            dataset_results.append({"prompt": prompt, **prompts[prompt]})
        final_results[dataset] = dataset_results

    return final_results


def generate_router_points(data, endpoint_dao, benchmark_run_dao):
    final_scores = dict()
    endpoint_to_model = lambda endpoint: (
        endpoint.split("@")[0].replace("gpt-4-turbo", "gpt-4-0125-preview") + "_pred"
    )
    endpoints = metrics.keys()
    endpoint_costs = {e: metrics[e]["cost"] for e in endpoints}
    endpoint_ttft = {e: metrics[e]["ttft"] for e in endpoints}
    endpoint_itl = {e: metrics[e]["itl"] for e in endpoints}

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
                    router_points.append(
                        {
                            "model": f"router_{a_label}_{b_label}_{c_label}",
                            "quality": final_score,
                            "cost": final_cost,
                            "ttft": final_ttft,
                            "itl": final_itl,
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


def generate_and_prune_points(raw_responses, endpoint_dao, benchmark_run_dao):
    organized_responses = reorganize_data(raw_responses)
    point_solutions = get_point_solutions(organized_responses)
    router_points = generate_router_points(
        organized_responses, endpoint_dao, benchmark_run_dao
    )
    get_list = lambda p1, p2: (p1.split("_")[1:], p2.split("_")[1:])
    get_dist = lambda p1, p2: sum(abs(float(p1_i) - float(p2_i)) for p1_i, p2_i in zip(p1, p2))
    dist = [get_dist(*get_list(point["model"], ROUTER_POINT)) for point in router_points["hermes"]]
    min_idx = np.argmin(dist)
    chosen_point = router_points["hermes"][min_idx]
    chosen_point["model"] = ROUTER_POINT
    pruned_router_points = prune_router_points(router_points)
    pruned_router_points["hermes"].append(chosen_point)
    for dataset in pruned_router_points:
        pruned_router_points[dataset] = pruned_router_points[dataset]
    final_points = {}
    for dataset in pruned_router_points:
        final_points[dataset] = {
            "point_solutions": point_solutions[dataset],
            "router": pruned_router_points[dataset],
        }
    return final_points


metrics = {
    "claude-3-haiku@anthropic": {
        "cost": 1.25,
        "ttft": 641.8530669999427,
        "itl": 7.320965663317298,
    },
    "claude-3-opus@anthropic": {
        "cost": 75,
        "ttft": 2591.2904499998604,
        "itl": 34.60999395530718,
    },
    "claude-3-sonnet@anthropic": {
        "cost": 15,
        "ttft": 1151.3890589999392,
        "itl": 12.03211326126186,
    },
    "deepseek-coder-33b-instruct@together-ai": {
        "cost": 0.8,
        "ttft": 350.50168700001905,
        "itl": 27.84690674999979,
    },
    "gemma-7b-it@anyscale": {
        "cost": 0.15,
        "ttft": 1176.8055530000083,
        "itl": 23.80950663414629,
    },
    "gemma-7b-it@together-ai": {
        "cost": 0.2,
        "ttft": 355.0849639999569,
        "itl": 10.75794840476246,
    },
    "gemma-7b-it@fireworks-ai": {
        "cost": 0.2,
        "ttft": 596.3048590000426,
        "itl": 4.905699351647504,
    },
    "gemma-7b-it@lepton-ai": {
        "cost": 0.1,
        "ttft": 1013.7901719999718,
        "itl": 10.638872657407488,
    },
    "gemma-7b-it@deepinfra": {
        "cost": 0.13,
        "ttft": 1106.7886140000383,
        "itl": 18.87030848101254,
    },
    "gpt-3.5-turbo@openai": {
        "cost": 1.5,
        "ttft": 400.21933599996373,
        "itl": 27.26199600000041,
    },
    "gpt-4-turbo@openai": {
        "cost": 30,
        "ttft": 635.7509760000539,
        "itl": 42.31438732535859,
    },
    "llama-3-70b-chat@fireworks-ai": {"cost": 0.9, "ttft": 469.78, "itl": 6.58},
    "llama-3-70b-chat@together-ai": {"cost": 0.9, "ttft": 466.28, "itl": 5.38},
    "llama-3-8b-chat@fireworks-ai": {"cost": 0.2, "ttft": 355.48, "itl": 3.06},
    "llama-3-8b-chat@together-ai": {"cost": 0.2, "ttft": 1035.13, "itl": 3.98},
    "mistral-large@mistral-ai": {
        "cost": 24,
        "ttft": 439.49507400009225,
        "itl": 54.14005861235942,
    },
    "mistral-small@mistral-ai": {
        "cost": 6,
        "ttft": 371.52690400000665,
        "itl": 18.000006300000376,
    },
    "mixtral-8x7b-instruct-v0.1@together-ai": {
        "cost": 0.6,
        "ttft": 405.11531099997455,
        "itl": 4.174361656626742,
    },
    "mixtral-8x7b-instruct-v0.1@octoai": {
        "cost": 0.5,
        "ttft": 1164.472783000008,
        "itl": 24.274311994623353,
    },
    "mixtral-8x7b-instruct-v0.1@replicate": {
        "cost": 1,
        "ttft": 887.903352999956,
        "itl": 15.394309863636439,
    },
    "mixtral-8x7b-instruct-v0.1@mistral-ai": {
        "cost": 0.7,
        "ttft": 352.0689869999387,
        "itl": 12.773902387097081,
    },
    "mixtral-8x7b-instruct-v0.1@anyscale": {
        "cost": 0.5,
        "ttft": 1749.3290439999782,
        "itl": 34.07672297029734,
    },
    "mixtral-8x7b-instruct-v0.1@fireworks-ai": {
        "cost": 0.5,
        "ttft": 324.21352400001524,
        "itl": 3.380061226190194,
    },
    "mixtral-8x7b-instruct-v0.1@lepton-ai": {
        "cost": 0.5,
        "ttft": 872.5847029999159,
        "itl": 12.631626471590804,
    },
    "mixtral-8x7b-instruct-v0.1@deepinfra": {
        "cost": 0.27,
        "ttft": 1130.8457239999825,
        "itl": 15.669842747059405,
    },
    "mixtral-8x7b-instruct-v0.1@aws-bedrock": {
        "cost": 0.7,
        "ttft": 713.9613250001275,
        "itl": 15.034942066296473,
    },
}
