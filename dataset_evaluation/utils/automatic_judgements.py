import json


def parse_multiple_choice(s, gt):
    return int(s.strip()[0].upper() == gt.upper())


def parse_number(s, gt):
    return int(str(gt) in s)


def automatic_judgements(
    prompt_file, asst_response_file, judge_response_file, parse_type
):
    prompt_idto_gt = {}
    with open(prompt_file) as pf:
        for line in pf:
            data = json.loads(line)
            prompt_id = data["prompt_id"]
            gt = data["ref_answer"]
            prompt_idto_gt[prompt_id] = gt

    if parse_type == "number":
        parse_fn = parse_number
    elif parse_type == "multiple_choice":
        parse_fn = parse_multiple_choice
    else:
        raise Exception

    prompt_idto_scores = {}
    with open(asst_response_file) as pf:
        for line in pf:
            data = json.loads(line)
            prompt_id = data["prompt_id"]
            response = data["model_response"]
            score = parse_fn(response, prompt_idto_gt[prompt_id])
            id_to_scores[prompt_id] = score

    with open(judge_response_file, "w") as jf:
        for prompt_id, score in id_to_scores.items():
            jf.write(json.dumps({"prompt_id": prompt_id, "score": score}) + "\n")
