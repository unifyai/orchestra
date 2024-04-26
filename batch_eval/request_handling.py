import json
import requests
from dataclasses import dataclass
from typing import Callable


class Request:
    def __init__(
        self,
        id_: int,
        payload: dict,
        api_fn: Callable[[dict], dict],
        prompt: str,
        response_type,
        model_name="",
    ):
        self.id_ = id_
        self.payload = payload
        self.api_fn = api_fn
        self.prompt = prompt
        self.response_type = response_type
        self.model_name = model_name

    def execute(self):
        return self.api_fn(self.payload)


def generic_call(request: Request):
    response = request.execute()
    if response.status_code == 200:
        try:
            resp_json = json.loads(response.text)
            model_response = resp_json["choices"][0]["message"]["content"]
            model_provider = resp_json["model"]
            if request.response_type == "judge_response":
                return (
                    True,
                    {
                        "id_": request.id_,
                        "prompt": request.prompt,
                        "model_provider": request.model_name,
                        "judge_model": model_provider,
                        request.response_type: model_response,
                    },
                )
            else:
                return (
                    True,
                    {
                        "id_": request.id_,
                        "prompt": request.prompt,
                        "model_provider": model_provider,
                        request.response_type: model_response,
                    },
                )
        except:
            pass

    # if something went wrong
    try:
        print(response.text)
        return (False, response.text)
    except:
        return (False, 0)


def create_payload(model_tag, prompt):
    messages = [
        {"role": "user", "content": prompt},
    ]
    payload = {
        "model": f"{model_tag}",
        "messages": messages,
        "max_tokens": 4096,  # might have to edit this for some models
    }
    return payload


def call_api(payload, url, headers):
    response = requests.post(url, json=payload, headers=headers)
    return response
