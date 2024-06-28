import os
from collections import defaultdict
from datetime import datetime


import yaml
import pandas as pd
from utils.data_utils import ratings_from_sample

# import wandb


def load_train_config(path):
    """
    path: path to yaml file containig training config
    """
    with open(path, "r") as f:
        config = yaml.load(f, Loader=yaml.FullLoader)
    return config


class MetricTracker:
    """
    Tracks the average of arbitrary metrics
    """

    def __init__(self):
        self.metric_stats = defaultdict(lambda: {"running_sum": 0, "count": 0})

    def update(self, metric_name, val):
        self.metric_stats[metric_name]["running_sum"] += val
        self.metric_stats[metric_name]["count"] += 1

    def reset(self):
        self.metric_stats = defaultdict(lambda: {"running_sum": 0, "count": 0})

    def return_averages(self):
        avgs = {}
        for metric, stats in self.metric_stats.items():
            avgs[metric] = stats["running_sum"] / stats["count"]
        return avgs


def load_datasets(paths):
    """
    paths: paths for jsonl datasets
    """
    dfs = []
    for path in paths:
        print(path)
        dfs.append(pd.read_json(path, lines=True))

    df = pd.concat(dfs)
    if "score" not in df.columns:
        response_key = (
            "judge_response" if "judge_response" in df.columns else "model_response"
        )
        df["score"] = df[response_key].map(ratings_from_sample)
    df = df.dropna(subset=["score"]).reset_index(drop=True)
    return df


def log_to_wandb(metrics, step, type="train"):
    for k, v in metrics.items():
        wandb.log({f"{type}/{k}": v}, step=step)


def get_classes():
    pass


def create_dirs(exp_name=None):
    dir_name = str(datetime.now()).replace(" ", "_")
    if exp_name:
        dir_name = exp_name + "_" + dir_name
    os.mkdir(f"artifacts/{dir_name}")
    for d in ["datasets", "models"]:
        os.mkdir(f"artifacts/{dir_name}/{d}")
    os.mkdir(f"artifacts/{dir_name}/models/epochs")
    return f"artifacts/{dir_name}"


METRICS = {
    "accuracy": ...,
    "precision": ...,
}
