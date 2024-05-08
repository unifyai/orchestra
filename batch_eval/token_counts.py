import os
import json

import tiktoken

def count_tokens(root_dir):
    lines = []
    id_model_to_tokens = {}
    model_response_dir = f'{root_dir}/model_responses'
    for model_response_file in os.listdir(model_response_dir):
        model_response_path = f'{model_response_dir}/{model_response_file}'
        if not os.path.isfile(model_response_path):
            continue
        model_str = model_response_file.split(".jsonl")[0]

        with open(model_response_path) as f:
            for line in f:
                entry = json.loads(line)
                id_ = entry["id_"]
                prompt = entry["prompt"]
                response = entry["model_response"]

                num_prompt_toks = len(enc.encode(prompt))
                num_response_toks = len(enc.encode(response))

                data = {
                    "id_": id_,
                    "model": model_str,
                    "num_toks_in": num_prompt_toks,
                    "num_toks_out": num_response_toks
                }
                lines.append(data)
                id_model_to_tokens[id_, model_str] = {
                    "num_toks_in": num_prompt_toks,
                    "num_toks_out": num_response_toks,
                }

    token_counts_dir = f'{root_dir}/token_counts'
    if not os.path.exists(token_counts_dir):
        os.makedirs(token_counts_dir)

    save_path = f'{token_counts_dir}/tok_counts.jsonl'
    with open(save_path, 'w') as f:
        for line in lines:
            f.write(json.dumps(line)+"\n")
    
    return id_model_to_tokens
