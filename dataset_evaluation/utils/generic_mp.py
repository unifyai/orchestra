import asyncio
import datetime
import json
import os

from google.cloud import storage
from utils.request_handling import generic_call


async def call_model(payload):
    ret = await generic_call(payload)
    return ret


async def process_requests(
    unprocessed_prompts: list,
    response_filename,
    batch_size=5,
    tries=5,
    gcp_config=None,
):
    # prompts are a Request object

    aborted = []
    failed = {}
    in_progress = []

    while unprocessed_prompts or in_progress:
        num_new_prompts = 2 * batch_size - len(in_progress)
        new_prompts = unprocessed_prompts[:num_new_prompts]
        unprocessed_prompts = unprocessed_prompts[num_new_prompts:]
        in_progress += new_prompts

        results = await asyncio.gather(*[call_model(p) for p in in_progress])

        complete_results = []
        cur_incomplete = []
        for prompt, result in zip(in_progress, results):
            result_success, result = result
            if result_success:
                assert result["id_"] == prompt.id_
                complete_results.append(result)
            else:
                if prompt.id_ in failed:
                    if failed[prompt.id_] >= tries:
                        print(f"failed {prompt.id_}")
                        aborted.append(prompt.id_)
                        try:
                            print(result)
                        except:
                            pass
                        continue
                    failed[prompt.id_] += 1
                else:
                    failed[prompt.id_] = 1
                cur_incomplete.append(prompt)

        # write complete results
        with open(response_filename, "a") as file:
            for result in complete_results:
                file.write(json.dumps(result) + "\n")

        if gcp_config:
            bucket_name = gcp_config.get("bucket_name")
            response_blob_name = gcp_config.get("response_blob_name")
            progress_blob_name = gcp_config.get("progress_blob_name")
            on_prem = os.environ.get("ON_PREM")
            shared_volume = os.environ.get("SHARED_VOLUME")

            # upload responses
            if on_prem:
                response_file_path = os.path.join(
                    shared_volume,
                    bucket_name,
                    response_blob_name,
                )
                os.makedirs(
                    os.sep.join(response_file_path.split(os.sep)[:-1]),
                    exist_ok=True,
                )
                with open(response_filename, "rb") as f:
                    response = f.read()
                with open(response_file_path, "wb") as f:
                    f.write(response)
            else:
                blob = storage.Client().bucket(bucket_name).blob(response_blob_name)
                blob.upload_from_filename(response_filename)

            # upload progress
            num_responses = sum(1 for i in open(response_filename, "rb"))
            num_left = len(cur_incomplete) + len(unprocessed_prompts)
            num_failed = len(aborted)
            progress_str = json.dumps(
                {
                    "num_processed": num_responses,
                    "num_remaining": num_left,
                    "num_failed": num_failed,
                    "last_updated": str(datetime.datetime.now()),
                },
            )
            if on_prem:
                progress_file_path = os.path.join(
                    shared_volume,
                    bucket_name,
                    progress_blob_name,
                )
                os.makedirs(
                    os.sep.join(progress_file_path.split(os.sep)[:-1]),
                    exist_ok=True,
                )
                with open(progress_file_path, "w") as f:
                    f.write(progress_str)
            else:
                blob = storage.Client().bucket(bucket_name).blob(progress_blob_name)
                blob.upload_from_string(progress_str)

        # retry incomplete results
        in_progress = cur_incomplete
