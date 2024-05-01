import json
import math
from statistics import mean
import numpy as np
import scipy


def reorganize_data(raw_responses):
    datasets = dict()
    for response in raw_responses:
        relevant_info = {
            "prompt": response["prompt"],
            "mdl_name": response["mdl_name"],
            "score": response["gt_score"],
            "pred": response["score"],
        }
        if relevant_info["pred"] and relevant_info["pred"] != "null":
            if response["dataset_name"] in datasets:
                datasets[response["dataset_name"]].append(relevant_info)
            else:
                datasets[response["dataset_name"]] = [relevant_info]

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


def generate_router_points(data):
    final_scores = dict()
    endpoint_to_model = lambda endpoint: (
        endpoint.split("@")[0].replace("gpt-4-turbo", "gpt-4-0125-preview") + "_pred"
    )
    with open("metrics.json") as f:
        metrics = json.load(f)
    endpoints = metrics.keys()
    print(f"endpoints: {endpoints}")
    endpoint_costs = {endpoint: metrics[endpoint]["cost"] for endpoint in endpoints}
    endpoint_ttft = {endpoint: metrics[endpoint]["ttft"] for endpoint in endpoints}
    endpoint_itl = {endpoint: metrics[endpoint]["itl"] for endpoint in endpoints}

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
        endpoint_scores = {endpoint: get_mean(endpoint) for endpoint in endpoints}
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
                    print(f"{a_idx}_{b_idx}_{c_idx}")
                    print(f"{a} {b} {c}")
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
                    router_points.append(
                        {
                            "model": f"router_{abs(round(a, 3))}_{abs(round(b, 3))}_{abs(round(c, 3))}",
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
        print(dataset)
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

            print(len(deleted), num)
            pruned_points = [
                point for i, point in enumerate(convex_points) if i not in deleted
            ]
            print(len(pruned_points))
        else:
            pruned_points = points
        final_pruned_points[dataset] = pruned_points

    return final_pruned_points


def generate_and_prune_points(raw_responses):
    organized_responses = reorganize_data(raw_responses)
    point_solutions = get_point_solutions(organized_responses)
    router_points = generate_router_points(organized_responses)
    pruned_router_points = prune_router_points(router_points)
    final_points = {}
    for dataset in pruned_router_points:
        final_points[dataset] = {
            "point_solutions": point_solutions[dataset],
            "router": pruned_router_points[dataset],
        }
    return final_points


# raw_responses = json.load(open("temp_data.json"))
# final_points = generate_and_prune_points(raw_responses)
# json.dump(final_points, open("final_temp_results.json", "w"))
