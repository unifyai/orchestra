import json
import requests
from dataclasses import dataclass
from typing import Callable
import aiohttp


class Request:
    def __init__(
        self,
        id_: int,
        payload: dict,
        url,
        headers,
        prompt: str,
        response_type,
        model_name="",
    ):
        self.id_ = id_
        self.payload = payload
        self.url = url
        self.headers = headers
        self.prompt = prompt
        self.response_type = response_type
        self.model_name = model_name

    async def execute(self):
        ret = await post_data(self.payload, self.url, self.headers)
        return ret


async def generic_call(request: Request):
    response = await request.execute()
    if True:
        try:
            resp_json = json.loads(response)
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
        except Exception as e:
            print(e)

    # if something went wrong
    try:
        print(response)
        return (False, response)
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


async def post_data(payload, url, headers):
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as response:
            return await response.text()
