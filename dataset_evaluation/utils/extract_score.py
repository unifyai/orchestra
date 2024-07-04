import json
import re


def extract_json(text):
    json_text = re.search(
        '\{[\n\r\s]*"assistant_rating":.*?\}', text, flags=re.DOTALL | re.MULTILINE
    ).group(0)
    return json_text


SCORE_MAPPING = {
    "irrelevant": 0.0,
    "very_bad": 0.0,
    "very_good": 0.8,
    "very bad": 0.0,
    "bad": 0.0,
    "good": 0.5,
    "satisfactory": 0.5,
    "very good": 0.8,
    "excellent": 1.0,
}


def ratings_from_sample(sample, score_mapping=SCORE_MAPPING):
    try:
        judge_response = json.loads(extract_json(sample))
        rating = judge_response["assistant_rating"]
        if isinstance(rating, list):
            rating = rating[0]
        if isinstance(rating, int):
            return rating
        elif isinstance(rating, float):
            return rating
        score = score_mapping[rating.lower().replace("_", " ")]
        return float(score)
    except Exception as e:
        return -1
