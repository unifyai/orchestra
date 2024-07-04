import json
import asyncio

from utils.request_handling import generic_call


async def call_model(payload):
    ret = await generic_call(payload)
    return ret


async def process_requests(
    unprocessed_prompts: list,
    response_filename,
    batch_size=5,
    tries=5,
):
    # prompts are a Request object

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

        # retry incomplete results
        in_progress = cur_incomplete
