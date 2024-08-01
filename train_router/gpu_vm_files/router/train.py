# we need a yaml config (done)
# train step should calculate some metrics and return them along side the loss

import random
import json
import os
import argparse
import warnings
import shutil
import math
from functools import cache

import torch

# import wandb
from torch.utils.data import DataLoader
from torch.optim.lr_scheduler import LambdaLR
from transformers import AutoTokenizer, DataCollatorWithPadding
from google.cloud import storage

os.environ["TOKENIZERS_PARALLELISM"] = "true"
warnings.filterwarnings("ignore")

from utils.train_utils import (
    METRICS,
    load_datasets,
    load_train_config,
    MetricTracker,
    log_to_wandb,
    create_dirs,
)
from utils.data_utils import CoMPDataset
from models.load_model import load_model

parser = argparse.ArgumentParser(prog="train", description="trains a router model")
parser.add_argument(
    "-cf", "--config_file", help="yaml config file for the training run"
)
parser.add_argument("-s", "--seed", default=0)
parser.add_argument("-tn", "--train_num", type=int, default=100000, required=False)


warmup_steps = 50
cooldown_threshold = 1200
cooldown_steps = 8000


def warmup_lambda(current_step):
    if current_step < warmup_steps:
        return current_step / warmup_steps
    # if current_step > cooldown_threshold:
    #     return (cooldown_threshold - min(current_step, cooldown_threshold + cooldown_steps))/cooldown_steps + 1
    return 1.0


def train(model, optimizer, train_dataloader, val_dataloader, config):
    scaler = torch.cuda.amp.GradScaler()
    train_iters = len(train_dataloader)
    best_val_loss = math.inf
    current_step = 0
    scheduler = LambdaLR(optimizer, lr_lambda=warmup_lambda)
    for epoch in range(1, config["train"]["epochs"]):
        for i, (prompt_id, attn_mask, model_id, target_score) in enumerate(
            train_dataloader
        ):
            with torch.autocast(
                device_type=config["train"]["device"], dtype=torch.float16
            ):
                model.train()
                loss, metrics = train_step(
                    model, prompt_id, attn_mask, model_id, target_score, config
                )
            # print(loss.shape, loss)
            scaler.scale(loss).backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            if (i + 1) % config["train"]["gradient_acc_steps"] == 0:
                scaler.step(optimizer)
                scaler.update()
                optimizer.zero_grad()

            metrics["loss"] = loss.item()
            # log_to_wandb(metrics, step=current_step, type="train")
            if current_step % config["train"]["eval_steps"] == 0:
                model.eval()

                # contains loss
                # val_metrics = validate(model, val_dataloader, config)
                # log_to_wandb(val_metrics, step=current_step, type="val")

                ### eval
                if current_step % 2 * config["train"]["eval_steps"] == 0:
                    val_path = config["data"]["validation_path"]
                    eval_metrics = validate_all_models(model, val_path)
                    # log_to_wandb(eval_metrics, step=current_step, type="val")

                # save model here

                # if val_metrics["loss"] < best_val_loss and config["train"]["save_eval"]:
                #     print(f"NEW BEST LOSS {best_val_loss} -> {val_metrics['loss']}, SAVING MODEL")
                #     best_val_loss = val_metrics["loss"]
                #     torch.save(model.state_dict(), os.path.join(config["train"]["created_dir"],
                #                                                 "models",
                #                                                 f"model_{current_step // config['train']['eval_steps']}_{val_metrics['loss']:.2f}.pth")

                #     )

            current_step += 1
            scheduler.step()

        if train_iters % config["train"]["gradient_acc_steps"] != 0:
            scaler.step(optimizer)
            scaler.update()
            optimizer.zero_grad()

        if config["train"]["save_epochs"]:
            torch.save(
                model.state_dict(),
                os.path.join(
                    config["train"]["created_dir"],
                    "models",
                    "epochs",
                    f"model_epoch_{epoch}.pth",
                ),
            )

        print(f"EPOCH {epoch} DONE")


def train_step(model, prompt_id, attn_mask, model_id, target_score, config):
    train_metrics = {}

    score = model(prompt_id, model_id, attn_mask)

    loss = config["train"]["loss_fn"](
        score.view(-1), target_score.float().view(-1).to(config["train"]["device"])
    )

    with torch.no_grad():
        for metric_name in config["train"]["metrics"]:
            metric_val = METRICS[metric_name](score, target_score)
            train_metrics[metric_name] = metric_val
    return loss, train_metrics


@torch.no_grad()
def validate(model, val_dataloader, config):
    val_metric_tracker = MetricTracker()
    for prompt_id, attn_mask, model_id, target_score in val_dataloader:
        score = model(prompt_id, model_id, attn_mask)
        loss = config["train"]["loss_fn"](
            score.view(-1), target_score.float().view(-1).to(config["train"]["device"])
        )

        val_metric_tracker.update("loss", loss.item())

        for metric_name in config["validation"]["metrics"]:
            metric_value = METRICS[metric_name](score, target_score)
            val_metric_tracker.update(metric_name, metric_value)

        for media in config["validation"]["media"]:
            # create images and log them to wandb
            pass
    # val_metrics["val/confusion matrix"] = get_confusion_matrix(preds, targets)
    return val_metric_tracker.return_averages()


##
# val set specification
# needs to be jsonl
# with entries
# id, prompt, gt
# the gt is
# model_name: predicted score


@cache
def create_eval_set(val_path):
    id_to_prompt = {}
    id_to_gt = {}
    with open(val_path) as f:
        for line in f:
            entry = json.loads(line)
            id_ = entry["id_"]
            prompt = entry["prompt"]
            # for entry in score
            # gt = {key: entry[key] for key in entry if key not in ["id_", "prompt_"]}
            # gt = entry["gt"]
            # this is a dict: model_name to score
            gt_list = [entry["scores"][f"{model_name}"] for model_name in MODEL_MAPPING]
            # this is a list of model scores, in the order of model mapping
            id_to_prompt[id_] = prompt
            id_to_gt[id_] = gt_list
    return id_to_prompt, id_to_gt


@torch.inference_mode()
def run_batch_inference(model, tokenizer, prompts: list, max_length, num_models):
    toks = tokenizer(
        prompts,
        max_length=max_length,
        padding="max_length",
        truncation=True,
        return_tensors="pt",
    ).to("cuda")
    embs = model.get_prompt_embs(
        prompt_id=toks["input_ids"], attn_mask=toks["attention_mask"]
    )
    model_emb = model.get_latent_rep(torch.arange(num_models, device="cuda"))
    stacked_embs = embs.unsqueeze(1).repeat(1, model_emb.size(0), 1)
    model_emb = model_emb.unsqueeze(0).repeat(stacked_embs.size(0), 1, 1)
    preds = model.embs_to_preds(stacked_embs, model_emb)
    return preds


def validate_all_models(model, val_path):
    id_to_prompt, id_to_gt = create_eval_set(val_path)
    loss_fn = torch.nn.functional.mse_loss
    num_models = len(MODEL_MAPPING)

    loss = 0
    vec_loss = torch.zeros(len(MODEL_MAPPING), device="cuda")

    ids_ = []
    prompts = []
    for id_, prompt in id_to_prompt.items():
        ids_.append(id_)
        prompts.append(prompt)

    BSZ = 64
    for batch_ix in range(len(ids_) // BSZ + 1):
        b_ids = ids_[BSZ * batch_ix : BSZ * (batch_ix + 1)]
        b_prompts = prompts[BSZ * batch_ix : BSZ * (batch_ix + 1)]
        if not b_prompts:
            continue
        batch_ret = run_batch_inference(
            model, tokenizer, b_prompts, config["train"]["max_num_tokens"], num_models
        ).squeeze(-1)
        gts = [id_to_gt[bi] for bi in b_ids]
        ground_truth = torch.tensor(gts, device="cuda")
        loss += loss_fn(batch_ret, ground_truth, reduction="sum").item()
        vec_loss += (batch_ret - ground_truth).pow(2).sum(dim=0)

    loss = loss / (len(MODEL_MAPPING) * len(prompts))
    ret = {"eval": loss}
    for ix, model in enumerate(MODEL_MAPPING):
        ret[f"{model}"] = vec_loss[ix].item() / (len(prompts))

    return ret


if __name__ == "__main__":
    args = parser.parse_args()
    config = load_train_config(args.config_file)
    # wandb.init(
    #    # set the wandb project where this run will be logged
    #    # mode="disabled",
    #    project="new_router",
    #    name=config["experiment"]["run_name"],
    #    config={**config},
    # )

    # load datasets
    df = load_datasets(config["data"]["data_paths"])

    # models = df["model_provider"].str.split("@").str[0].unique().tolist()
    models = df["model_provider"].unique().tolist()
    print(models)
    data = df.to_dict("records")

    MODEL_MAPPING = {k: v for v, k in enumerate(models)}
    ID2MODEL = {v: k for k, v in MODEL_MAPPING.items()}
    print(MODEL_MAPPING.keys())
    TOKENIZER = config["model"]["prompt_encoder"]
    torch.manual_seed(args.seed)
    random.seed(123)
    random.shuffle(data)
    data = data[: config["data"].get("num_training_samples", -1)]

    tokenizer = AutoTokenizer.from_pretrained(TOKENIZER)
    pad_collator = DataCollatorWithPadding(tokenizer=tokenizer, padding="longest")

    collator = lambda x: tuple(pad_collator(x).values())
    train_split = int(config["data"]["train_split_ratio"] * len(data))
    train_data = data[:train_split]
    val_data = data[train_split:]

    train_data = train_data[: args.train_num]

    loss_type = config["train"]["loss_type"]
    train_dataset = CoMPDataset(
        train_data,
        tokenizer=tokenizer,
        model_mapping=MODEL_MAPPING,
        score_mapping=config["train"]["score_mapping"],
        max_length=config["train"]["max_num_tokens"],
        ordinal=loss_type == "ordinal_regression",
        num_classes=5 if loss_type == "ordinal_regression" else None,
    )

    train_dataloader = DataLoader(
        train_dataset,
        batch_size=config["train"]["batch_size"],
        shuffle=True,
        collate_fn=collator,
        num_workers=4,
    )

    val_dataset = CoMPDataset(
        val_data,
        tokenizer=tokenizer,
        model_mapping=MODEL_MAPPING,
        score_mapping=config["train"]["score_mapping"],
        max_length=config["train"]["max_num_tokens"],
        ordinal=loss_type == "ordinal_regression",
        num_classes=5 if loss_type == "ordinal_regression" else None,
    )

    val_dataloader = DataLoader(
        val_dataset,
        batch_size=config["validation"]["batch_size"],
        shuffle=False,
        collate_fn=collator,
    )

    comp = load_model(config, num_models=len(MODEL_MAPPING))

    prompt_encoder_params = [
        p for name, p in comp.named_parameters() if "prompt_encoder" in name
    ]
    num_p = sum(p.numel() for p in prompt_encoder_params)
    print(f"Num prompt encoder params: {num_p/1e9:.2f}B")
    rest_params = [
        p for name, p in comp.named_parameters() if "prompt_encoder" not in name
    ]
    num_p = sum(p.numel() for p in rest_params)
    print(f"Num rest params: {num_p/1e6:.2f}M")

    loss_fn = (
        torch.nn.functional.binary_cross_entropy_with_logits
        if loss_type == "ordinal_regression"
        else torch.nn.functional.mse_loss
    )
    config["train"]["loss_fn"] = loss_fn

    optimizer = torch.optim.AdamW(
        [
            {
                "params": prompt_encoder_params,
                "lr": float(config["train"]["optimizer"]["prompt_encoder_lr"]),
            },
            {
                "params": rest_params,
                "lr": float(config["train"]["optimizer"]["rest_lr"]),
            },
        ]
    )

    comp = comp.to(config["train"]["device"])
    train_iters = len(train_dataloader)
    val_iters = len(val_dataloader)

    print(f"Num training samples: {len(train_data)}")
    print(f"Num val samples: {len(val_data)}")

    print(f"Num Train Steps: {train_iters}")
    print(f"Num Val Steps: {val_iters}")
    print(f"Grad Acc Steps: {config['train']['gradient_acc_steps']}")
    print("Begin Train...\n\n")

    # create dirs to save stuff to
    created_dir = create_dirs(exp_name=config["experiment"]["run_name"])

    # save model mapping
    with open(os.path.join(created_dir, "model_mapping.json"), "w") as f:
        json.dump(MODEL_MAPPING, f)

    # save used datasets for later error analysis
    for d in ["train", "val"]:
        with open(os.path.join(created_dir, "datasets", f"{d}.jsonl"), "w") as f:
            dataset = train_data if d == "train" else val_data
            for line in dataset:
                f.write(json.dumps(line) + "\n")

    # save yaml file used
    shutil.copyfile(args.config_file, os.path.join(created_dir, "config.yaml"))

    config["train"]["created_dir"] = created_dir

    # start train
    train(comp, optimizer, train_dataloader, val_dataloader, config)

    # what to do with the weights at the end ??
    # upload weights

    # get weights

    weight_path = os.path.join(
        config["train"]["created_dir"],
        "models",
        "epochs",
        f"model_epoch_5.pth",
    )

    config_path = os.path.join(config["train"]["created_dir"], "config.yaml")
    model_mapping_path = os.path.join(
        config["train"]["created_dir"], "model_mapping.json"
    )
    # upload weights to bucket

    assert False, "need user_id and name"
    bucket_name = "custom_router_data"
    directory = f"custom_router/{user_id}/{name}/"

    file_path = weight_path
    file_name = "model_epoch_5.pth"

    for file_path, file_name in zip([weight_path, config_path, model_mapping_path], ["model.pth", "config.yaml", "model_mapping.json"]):
        blob_name = directory + file_name
        blob = storage.Client().bucket(bucket_name).blob(blob_name)
        blob.upload_from_filename(file_path)
