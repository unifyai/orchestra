import json
import re

import numpy as np
import torch
from torch.utils.data import Dataset


def extract_json(text):
    json_text = re.search(
        '\{[\n\r\s]+"assistant_rating":.*?\}',
        text,
        flags=re.DOTALL | re.MULTILINE,
    )
    if json_text is None:
        return json_text
    else:
        return json_text.group(0)


def clean_judge_responses(response):
    response = response.replace("assistant\\_a\\_rating", "assistant_a_rating")
    response = response.replace("assistant\\_b\\_rating", "assistant_b_rating")
    response = response.replace(",", "")
    return response


def ratings_from_sample(sample, score_mapping=None):
    if isinstance(sample, dict) and "score" in sample.keys():
        return sample["score"]
    response_key = "judge_response" if "judge_response" in sample else "model_response"
    clean_sample = clean_judge_responses(
        sample[response_key] if hasattr(sample, "keys") else sample,
    )
    judge_response = extract_json(clean_sample)
    if judge_response is None:
        return np.nan
    try:
        rating = json.loads(judge_response)["assistant_rating"]

        if isinstance(rating, list):
            rating = rating[0]
        if score_mapping is not None:
            try:
                rating = score_mapping[rating.lower()]
            except:
                return 0.0
        return rating

    except:
        return np.nan


class CoMPDataset(Dataset):
    def __init__(
        self,
        data,
        tokenizer,
        model_mapping,
        score_mapping,
        max_length=512,
        device="cpu",
        ordinal=False,
        num_classes=None,
        semantic_emb=False,
    ):
        # assume list(dict)
        self.data = data
        self.tokenizer = tokenizer
        self.max_length = max_length
        self.model_mapping = model_mapping
        self.score_mapping = score_mapping
        self.device = device
        self.ordinal = ordinal
        self.num_classes = num_classes
        if semantic_emb:
            self.semantic_emb = True

    def __getitem__(self, idx):
        sample = self.data[idx]
        prompt = sample["prompt"]
        prompt = prompt.strip()
        tokenized_inputs = self.tokenizer(
            prompt,
            max_length=self.max_length,
            truncation=True,
            return_tensors="pt",
        )
        prompt_id = tokenized_inputs["input_ids"].squeeze()
        attn_mask = tokenized_inputs["attention_mask"].squeeze()
        model_id = self.model_mapping[sample["model_provider"]]
        score = ratings_from_sample(sample, self.score_mapping)
        if self.ordinal:
            score = [1] * (score + 1) + [0] * (self.num_classes - score - 1)

        out = {
            "input_ids": prompt_id,
            "attention_mask": attn_mask,
            "model_id": torch.tensor(model_id),
            "target_score": torch.tensor(score, dtype=torch.float),
        }
        return out

    def __len__(self):
        return len(self.data)
