# import pandas as pd
import csv
import json
import os
import random

import numpy as np
import tiktoken
from datasets import load_dataset


# READ THE bookcorpus dataset from https://huggingface.co/datasets/bookcorpus into a panda df, don't split into train/test
def generate_inputs():
    bookcorpus = load_dataset("bookcorpus", split="train")
    # bookcorpus_df = pd.DataFrame(bookcorpus)
    num_cores = os.cpu_count()

    def encode(batch):
        encoding = tiktoken.get_encoding("cl100k_base")
        batch["length"] = [
            len(item) if item is not None else 0
            for item in encoding.encode_batch(batch["text"])
        ]

        return batch

    bookcorpus = bookcorpus.map(encode, batched=True, num_proc=num_cores)

    lengths = bookcorpus["length"]

    average_length = np.mean(lengths)
    stddev_length = np.std(lengths)
    max_length = np.max(lengths)
    min_length = np.min(lengths)
    print(f"Average length: {average_length}")
    print(f"Standard deviation: {stddev_length}")
    print(f"Maximum length: {max_length}")
    print(f"Minimum length: {min_length}")

    prompt_lengths = np.random.normal(205, 21, 150).astype(int)

    prompts = []
    current_prompt = ""
    current_length = 0
    sentences = iter(bookcorpus)
    sentences_by_length = {}

    def process_sentence(s):
        try:
            if s["length"] not in sentences_by_length:
                sentences_by_length[s["length"]] = []
            sentences_by_length[s["length"]].append(s)
        except:
            pass

    from itertools import islice

    first_million_rows = list(islice(bookcorpus, 0, 1000000))
    list(map(process_sentence, first_million_rows))

    def add_random_sentence(length):
        nonlocal current_prompt, current_length
        # Use the dictionary to look up a random sentence of the given length
        if length in sentences_by_length:
            random_sentence = random.choice(sentences_by_length[length])
            current_prompt += "\n" + random_sentence["text"]
            current_length += random_sentence["length"]

    for row in sentences:
        # If adding the current row does not exceed the length of the current prompt
        if (
            current_length + row["length"]
            <= prompt_lengths[min(len(prompts), len(prompt_lengths) - 1)]
        ):
            # Add the current row to the current prompt
            current_prompt += "\n" + row["text"]
            current_length += row["length"]
        else:
            # Split the remaining length into three
            remaining_length = (
                prompt_lengths[min(len(prompts), len(prompt_lengths) - 1)]
                - current_length
            )

            split_lengths = [remaining_length // 8] * 8
            remaining = remaining_length % 8
            for i in range(remaining):
                split_lengths[i] += 1

            # Filter for random sentences of that length and add them to the prompt
            list(map(add_random_sentence, split_lengths))

            # Add the current prompt to the list of prompts and reset the current prompt and length
            prompts.append(current_prompt)
            current_prompt = ""
            current_length = 0
            if len(prompts) == 150:
                break

    import json

    with open("prompts_short.json", "w") as file:
        encoding = tiktoken.get_encoding("cl100k_base")
        json_prompts = [
            {"prompt": prompt, "length": len(encoding.encode(prompt))}
            for prompt in prompts
        ]
        json.dump(json_prompts, file)

    prompts = []
    prompt_lengths = np.random.normal(1000, 100, 150).astype(int)
    current_prompt = ""
    current_length = 0
    sentences = iter(bookcorpus)
    for row in sentences:
        # If adding the current row does not exceed the length of the current prompt
        if (
            current_length + row["length"]
            <= prompt_lengths[min(len(prompts), len(prompt_lengths) - 1)]
        ):
            # Add the current row to the current prompt
            current_prompt += "\n" + row["text"]
            current_length += row["length"]
        else:
            # Split the remaining length into three
            remaining_length = (
                prompt_lengths[min(len(prompts), len(prompt_lengths) - 1)]
                - current_length
            )

            split_lengths = [remaining_length // 40] * 40
            remaining = remaining_length % 40
            for i in range(remaining):
                split_lengths[i] += 1

            # Filter for random sentences of that length and add them to the prompt
            list(map(add_random_sentence, split_lengths))

            # Add the current prompt to the list of prompts and reset the current prompt and length
            prompts.append(current_prompt)
            current_prompt = ""
            current_length = 0
            if len(prompts) == 150:
                break

    with open("prompts_long.json", "w") as file:
        encoding = tiktoken.get_encoding("cl100k_base")
        json_prompts = [
            {"prompt": prompt, "length": len(encoding.encode(prompt))}
            for prompt in prompts
        ]
        json.dump(json_prompts, file)

    return bookcorpus


generate_inputs()


# encoding = tiktoken.get_encoding("cl100k_base")
# encoded_prompts = [encoding.encode(prompt.replace("\n", "")) for prompt in prompts]
# prompt_lengths = [len(x) for x in encoded_prompts]
# np.mean(prompt_lengths)
# np.std(prompt_lengths)
