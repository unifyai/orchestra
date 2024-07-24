import json

def parse_multiple_choice(s, gt):
    return int(s.strip()[0].upper() == gt.upper())


def parse_number(s, gt):
    return int(str(gt) in s)


def automatic_judgements(
    prompt_file, asst_response_file, judge_response_file, parse_type
):
    id_to_gt = {}
    with open(prompt_file) as pf:
        for line in pf:
            data = json.loads(line)
            id_ = data["id_"]
            gt = data["ref_answer"]
            id_to_gt[id_] = gt

    if parse_type == "number":
        parse_fn = parse_number
    elif parse_type == "multiple_choice":
        parse_fn = parse_multiple_choice
    else:
        raise Exception

    id_to_scores = {}
    with open(asst_response_file) as pf:
        for line in pf:
            data = json.loads(line)
            id_ = data["id_"]
            response = data["model_response"]
            score = parse_fn(response, id_to_gt[id_])
            id_to_scores[id_] = score

    with open(judge_response_file, "w") as jf:
        for id_, score in id_to_scores.items():
            jf.write(json.dumps({"id_": id_, "score": score}) + "\n")


if __name__ == "__main__":
    prompt_file = "tmp/prompts.jsonl"
    asst_response_file = "tmp/haiku.jsonl"
    judge_response_file = "tmp/judge.jsonl"
    parse_type = "multiple_choice"
    automatic_judgements(
        prompt_file, asst_response_file, judge_response_file, parse_type
    )
