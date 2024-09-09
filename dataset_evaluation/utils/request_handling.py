import asyncio
import json

import aiohttp
import requests


class Request:
    def __init__(
        self,
        id: int,
        payload: dict,
        url,
        headers,
        client,
        prompt: str,
        response_type,
        extra_kwargs={},
        score_fn=None,
    ):
        self.id = id
        self.payload = payload
        self.url = url
        self.headers = headers
        self.prompt = prompt
        self.response_type = response_type
        self.client = client
        self.extra_kwargs = extra_kwargs
        self.score_fn = score_fn

    async def execute(self):
        ret = await post_data(self.payload, self.url, self.headers, self.client)
        return ret


async def generic_call(request: Request):
    for tries in range(5):
        try:
            resp_json = await request.execute()
            model_response = resp_json["choices"][0]["message"]["content"]
            model_provider = resp_json["model"]
            if request.score_fn is not None:
                score = request.score_fn(sample=model_response)
                request.extra_kwargs["score"] = score
            return (
                True,
                {
                    "id": request.id,
                    "prompt": request.prompt,
                    request.response_type: model_response,
                    **request.extra_kwargs,
                },
            )

        except Exception as e:
            try:
                print(resp_json)
                print(request.payload)
            except:
                pass
            print(e)
            print(f"Error with {request.payload}")
            if tries < 2:
                await asyncio.sleep(2)
            else:
                print("waiting for 60s")
                await asyncio.sleep(60)

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
        "max_tokens": 512,  # might have to edit this for some models
    }
    return payload


async def post_data(payload, url, headers, client):
    ret = await client.post(url, json=payload, headers=headers)
    return ret.json()
