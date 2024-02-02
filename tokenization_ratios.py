# import pandas as pd
from datasets import load_dataset, concatenate_datasets
import tiktoken
import numpy as np
import csv
import os
from transformers import AutoTokenizer

def get_dataset(dataset_name, dir=None):
    try:
        if dir:
                dataset_dict = load_dataset(dataset_name, dir)
        else:
            dataset_dict = load_dataset(dataset_name)
        datasets = [v for k, v in dataset_dict.items()]
        return concatenate_datasets(datasets)
    except Exception as e:
        print('exception: ', e)
        pass
    
# TODO: Find COPA, DS-1000
# Check for openbook QA and why there are more than 500 on HF. Discrepancy with mosaic ML website
# check for winogrande, everything on HF is much larger than has been mentioned on the mosaic ML website
# check pubmed qa, seems to have only 500 rows when it should have a 1000
# 
dataset_names = ["jeopardy", "lukaemon/mmlu", "tasksource/bigbench", "piqa", "openbookqa", \
            "lambada", "Rowan/hellaswag", "winograd_wsc", "winogrande", "math_qa", \
                "lucasmccabe/logiqa", "hagara/labelled-PubMedQA", "squad", \
                "google/boolq", "openai_humaneval", "codeparrot/apps", "mbpp"]
datasets_dir  = {"lukaemon/mmlu": ['abstract_algebra', 'anatomy', 'astronomy', 'business_ethics', 
                                   'clinical_knowledge', 'college_biology', 'college_chemistry', 
                                   'college_computer_science', 'college_mathematics', 'college_medicine', 
                                   'college_physics', 'computer_security', 'conceptual_physics', 'econometrics', 
                                   'electrical_engineering', 'elementary_mathematics', 'formal_logic', 'global_facts',
                                     'high_school_biology', 'high_school_chemistry', 'high_school_computer_science', 
                                     'high_school_european_history', 'high_school_geography',
                                       'high_school_government_and_politics', 'high_school_macroeconomics', 
                                       'high_school_mathematics', 'high_school_microeconomics', 'high_school_physics', 
                                       'high_school_psychology', 'high_school_statistics', 'high_school_us_history', 
                                       'high_school_world_history', 'human_aging', 'human_sexuality', 'international_law', 
                                       'jurisprudence', 'logical_fallacies', 'machine_learning', 'management', 'marketing', 
                                       'medical_genetics', 'miscellaneous', 'moral_disputes', 'moral_scenarios', 'nutrition', 
                                       'philosophy', 'prehistory', 'professional_accounting', 'professional_law', 
                                       'professional_medicine', 'professional_psychology', 'public_relations', 'security_studies', 
                                       'sociology', 'us_foreign_policy', 'virology', 'world_religions'], \
                 "tasksource/bigbench": ["qa_wikidata", "misconceptions", "strategyqa", \
                                         "strange_stories", "novel_concepts", \
                                            "language_identification", \
                                            "conceptual_combinations", \
                                            "conlang_translation", \
                                            "elementary_math_qa", "dyck_languages", \
                                            "cs_algorithms", "logical_deduction", \
                                            "operators", "repeat_copy_logic", \
                                            "understanding_fables"],
                  "allenai/ai2_arc": ["ARC-Challenge", "ARC-Easy"],
                  "openbookqa": ["main", "additional"],
                  "winograd_wsc": ["wsc273"],
                  "winogrande": ["winogrande_xs"], \
                  "codeparrot/apps": ["all"], \
                  "mbpp": ["full"]}
tokenizers = ["meta-llama/Llama-2-13b-chat-hf", "openai"]
num_cores = os.cpu_count()
# AutoTokenizer.from_pretrained("meta-llama/Llama-2-13b-chat-hf", token="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")
def encode_openai(batch):
    encoding = tiktoken.get_encoding("cl100k_base")
    batch['knt'] = [len(item) if item is not None else 0 for item in encoding.encode_batch(batch['text'])]
    return batch
def encode(batch, tokenizer):
    encoded_batch = tokenizer.batch_encode_plus(batch['text'], return_length=True)
    batch['knt'] = encoded_batch['length']
    return batch
def concatenate_columns(dataset):
    text = ' '.join([str(value) for value in dataset.values()])
    return {'text': text}
def get_tokens(dataset, tokenizer):
    if tokenizer == "openai":
        dataset = dataset.map(encode_openai, batched=True, num_proc=num_cores)
    else:
        tokenizer = AutoTokenizer.from_pretrained("meta-llama/Llama-2-13b-chat-hf", token="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")
        dataset = dataset.map(lambda batch: encode(batch, tokenizer), batched=True, num_proc=num_cores)

    token_counts = dataset['knt']
    

    return np.sum(token_counts)

tokenizer_tokens = {tokenizer: 0 for tokenizer in tokenizers}
for dataset_name in dataset_names:
    if dataset_name in datasets_dir:
        for dir in datasets_dir[dataset_name]:
            dataset = get_dataset(dataset_name, dir=dir)
            dataset = dataset.map(concatenate_columns)
            for tokenizer in tokenizers:
                tokens = get_tokens(dataset, tokenizer)
                tokenizer_tokens[tokenizer] += tokens
                print(dataset_name, dir, tokenizer, tokens)
    else:
        dataset = get_dataset(dataset_name)
        dataset = dataset.map(concatenate_columns)
        for tokenizer in tokenizers:
            tokens = get_tokens(dataset, tokenizer)
            tokenizer_tokens[tokenizer] += tokens
            print(dataset_name, dir, tokenizer, tokens)
    print("Done with ", dataset_name)

print('tokenizer tokens', tokenizer_tokens)