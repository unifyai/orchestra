import json
import re

default_cfg = [
    {"label": "excellent", "score": 1.0},
    {"label": "very_good", "score": 0.8},
    {"label": "good", "score": 0.5},
    {"label": "bad", "score": 0.0},
    {"label": "irrelevant", "score": 0.0},
]


def extract_json(text):
    json_text = re.search(
        '\{[\n\r\s]*"assistant_rating":.*?\}', text, flags=re.DOTALL | re.MULTILINE
    ).group(0)
    return json_text


def ratings_from_sample(sample, cfg=default_cfg):
    try:
        score_mapping = {e["label"]: e["score"] for e in cfg}
        judge_response = json.loads(extract_json(sample))
        rating = judge_response["assistant_rating"]
        if rating in score_mapping:
            score = score_mapping[rating]
        elif rating.lower() in score_mapping:  # TODO: more comprehensive
            score = score_mapping[rating.lower()]
        return float(score)
    except Exception as e:
        return -1
