import json
import os
import shutil
import sys
from collections import defaultdict

from google.cloud import storage


def refresh_scores_for_dataset(
    user_id,
    dataset,
    root_path="save_files/",
):
    shared_volume = os.environ.get("SHARED_VOLUME")
    os.makedirs(root_path, exist_ok=True)
    bucket_name = "uploaded_datasets"
    prefix = os.path.join(user_id, dataset, "0")
    if os.environ.get("ON_PREM"):
        prefix_folder_path = os.path.join(shared_volume, bucket_name, prefix)
        blobs = []
        for root, _, files in os.walk(prefix_folder_path):
            for file in files:
                blobs.append(os.path.join(root, file))
    else:
        storage_client = storage.Client()
        blobs = storage_client.list_blobs(bucket_name, prefix=prefix)

    # format is {eval_id: {endpoint : {judge : score
    results = defaultdict(lambda: defaultdict(defaultdict))  # TODO nicer way?
    for b in blobs:
        name = b if os.environ.get("ON_PREM") else b.name
        if name.endswith("_judged.jsonl"):
            name_parts = name.split("/")
            judge_model = (
                name_parts[-1].replace("_judged.jsonl", "").replace("___", "@")
            )
            eval_id = name_parts[-2]
            endpoint = name.split("/")[-3]
            contents = (
                open(os.path.join(prefix_folder_path, name), "rb").read()
                if os.environ.get("ON_PREM")
                else b.download_as_bytes()
            )
            contents = contents.decode("utf-8").split("\n")
            scores = []
            for entry in contents:
                if not entry:
                    continue
                entry = json.loads(entry)
                if "score" in entry:
                    scores.append(float(entry["score"]))
                else:
                    print("score not found...")
            avg_score = sum(scores) / len(scores)
            results[eval_id][endpoint][judge_model] = avg_score

    save_path = os.path.join(root_path, "scores.json")
    with open(save_path, "w") as f:
        json.dump(results, f)

    blob_name = os.path.join(user_id, dataset, "0", "scores.json")
    if os.environ.get("ON_PREM"):
        file_path = os.path.join(shared_volume, bucket_name, blob_name)
        shutil.copy(save_path, file_path)
    else:
        blob = storage.Client().bucket(bucket_name).blob(blob_name)
        blob.upload_from_filename(save_path)


def list_datasets(user_id, shared_volume):
    shared_volume = os.environ.get("SHARED_VOLUME")
    bucket_name = "uploaded_datasets"
    prefix = user_id
    if os.environ.get("ON_PREM"):
        dir_path = os.path.join(shared_volume, bucket_name, prefix)
        blobs = []
        for root, _, files in os.walk(dir_path):
            for file in files:
                blobs.append(
                    os.path.join(root, file).replace(
                        os.path.join(shared_volume, bucket_name),
                        "",
                    ),
                )
        dirs = set([b.split("/")[2] for b in blobs])
        dirs = {d for d in dirs if not d.endswith(".jsonl")}
    else:
        bucket = storage.Client().bucket(bucket_name)
        blobs = list(bucket.list_blobs(prefix=prefix))

        dirs = set(
            [b.id.split("/")[2] for b in blobs],
        )
        dirs = {d for d in dirs if not d.endswith(".jsonl")}
    dirs.discard("evaluation_configs")
    return list(dirs)


def refresh_scores_for_user(user_id, root_path="save_files/"):
    # TODO: Fix this
    for dataset in list_datasets(user_id):
        refresh_scores_for_dataset(
            user_id,
            dataset,
            root_path=root_path,
        )


if __name__ == "__main__":
    msg_raw = sys.argv[1]
    message = json.loads(msg_raw)
    user_id = message["user_id"]
    refresh_scores_for_user(user_id)
