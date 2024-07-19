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


blob_name = f"/0/dataset.jsonl"


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
        return 0


def calc_quality(judgements_path, cfg=default_cfg):
    scores = []
    with open(judgements_path) as f:
        for line in f:
            entry = json.loads(line)
            prompt_score = ratings_from_sample(entry["judge_response"], cfg)
            scores.append(prompt_score)
    print(scores)
    return sum(scores) / len(scores)


if __name__ == "__main__":
    cfg = [
        {
            "label": "Excellent",
            "score": 1.0,
            "description": "A perfect answer with no factual mistakes",
        },
        {"label": "Good", "score": 0.5},
        {
            "label": "Bad",
            "score": 0.0,
            "description": "An incorrect answer, containing a significant factual mistake",
        },
    ]
    score = calc_quality(
        "/home/tje/work/orchestra/dataset_evaluation/save_files/clwq7wcn00006o7rt5nea9ktt/SwifScore_5/model_judgements/llama-3-8b-chat___together-ai___gpt-4o___openai.jsonl",
        cfg,
    )
    print(score)
