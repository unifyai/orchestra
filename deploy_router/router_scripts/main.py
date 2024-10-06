import json

import torch
import yaml
from dcn import DCN
from transformers import AutoTokenizer

DEVICE = "cuda"

config_path = "config.yaml"
with open(config_path, "r") as f:
    config = yaml.load(f, Loader=yaml.FullLoader)

model_mapping_path = "model_mapping.json"
with open(model_mapping_path) as f:
    MODEL_MAPPING = json.load(f)

num_models = len(MODEL_MAPPING)
model = DCN(
    num_models=num_models,
    embed_dim=config["model"]["embed_dim"],
    dropout=config["model"]["dropout"],
    device=DEVICE,
    model_name=config["model"]["prompt_encoder"],
).to(DEVICE)

model_weights_path = "model.pth"
model.load_state_dict(torch.load(model_weights_path, map_location=torch.device(DEVICE)))
model.eval()
tokenizer = AutoTokenizer.from_pretrained(config["model"]["prompt_encoder"])


@torch.inference_mode()
def run_inference(model, tokenizer, prompt, model_id, max_length):
    tok = tokenizer(
        [prompt],
        max_length=max_length,
        truncation=True,
        return_tensors="pt",
    ).to(DEVICE)
    n = model_id.size(0)
    datum_id = tok["input_ids"].repeat(n, 1)
    attn_mask = tok["attention_mask"].repeat(n, 1)
    ret = model.forward(datum_id=datum_id, model_id=model_id, attn_mask=attn_mask)
    return ret.flatten()


import os
from typing import Dict, List

from fastapi import FastAPI, Request
from pydantic import BaseModel

app = FastAPI(title="Neural scoring function")

AIP_HEALTH_ROUTE = os.environ.get("AIP_HEALTH_ROUTE", "/health")
AIP_PREDICT_ROUTE = os.environ.get("AIP_PREDICT_ROUTE", "/predict")


class Prediction(BaseModel):
    scores: Dict[str, float]


class Predictions(BaseModel):
    predictions: List[Prediction]


@app.get(AIP_HEALTH_ROUTE, status_code=200)
async def health():
    return {"health": "ok"}


@app.post(
    AIP_PREDICT_ROUTE,
    response_model=Predictions,
    response_model_exclude_unset=True,
)
async def predict(request: Request):
    body = await request.json()

    instances = body["instances"]
    prompt = instances[0]["prompt"]

    ret = (
        run_inference(
            model,
            tokenizer,
            prompt,
            torch.arange(num_models, device=DEVICE),
            config["train"]["max_num_tokens"],
        )
        .detach()
        .cpu()
        .tolist()
    )

    scores = Prediction(
        scores={model_name: score for model_name, score in zip(MODEL_MAPPING, ret)},
    )

    return Predictions(predictions=[scores])


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=80)
