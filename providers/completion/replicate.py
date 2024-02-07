import asyncio
import time
from datetime import datetime
from typing import Any, List, Optional

import requests
from fastapi import HTTPException
from litellm.llms.prompt_templates.factory import prompt_factory
from openai.types.chat.chat_completion import ChatCompletion
from openai.types.chat.chat_completion import Choice as ChatCompletionChoice
from openai.types.chat.chat_completion_chunk import (
    ChatCompletionChunk,
    Choice,
    ChoiceDelta,
)
from openai.types.chat.chat_completion_message import ChatCompletionMessage
from openai.types.completion_usage import CompletionUsage
from providers.completion.base_completion_provider import (
    PRICING_PER_TOKENS,
    AsyncGeneratorWrapper,
    BaseCompletionProvider,
)


class Replicate(BaseCompletionProvider):
    """
    Initializes with list of few OSS models as example.

    Source: https://replicate.com/explore
    Pricing has dual pricing: either pay for time it takes to process your request or
    per million tokens: https://replicate.com/pricing
    """

    hardware_pricing_per_sec = {
        "cpu": 0.000100,  # noqa: WPS339
        "t4": 0.000225,
        "a40": 0.000575,
        "a40-large": 0.000725,
        "a100-40gb": 0.001150,  # noqa: WPS339
        "a100-80gb": 0.001400,  # noqa: WPS339
        "8xa40": 0.005800,  # noqa: WPS339
    }
    supported_models = {
        "mistral-7b-instruct-v0.1": {
            "endpoint": "mistralai/mistral-7b-instruct-v0.1:83b6a56e7c828e667f21fd596c338fd4f0039b46bcfa18d973e8e70e455fda70",  # noqa: E501
            "context_window": 16384,
            "cost": {"hardware": "a40", "per_second": True},
        },
        "mistral-7b-instruct-v0.2": {
            "endpoint": "mistralai/mistral-7b-instruct-v0.2",
            "context_window": 16384,
            "cost": {"prompt": 0.05, "completion": 0.25},
        },
        "mixtral-8x7b-instruct-v0.1": {
            "endpoint": "mistralai/mixtral-8x7b-instruct-v0.1",
            "context_window": 16384,
            "cost": {"prompt": 0.30, "completion": 1.00},  # noqa: WPS339
        },
        "mistral-7b-v0.1": {
            "endpoint": "mistralai/mistral-7b-v0.1",
            "context_window": 4096,
            "cost": {"prompt": 0.05, "completion": 0.25},
        },
        "llama-2-70b": {
            "endpoint": "meta/llama-2-70b",
            "context_window": 4096,
            "cost": {"prompt": 0.65, "completion": 2.75},
        },
        "llama-2-70b-chat": {
            "endpoint": "meta/llama-2-70b-chat",
            "context_window": 4096,
            "cost": {"prompt": 0.65, "completion": 2.75},
        },
        "gpt-j-6b": {
            "endpoint": "gpt-j-6b:b3546aeec6c9891f0dd9929c2d3bedbf013c12e02e7dd0346af09c37e008c827",  # noqa: E501
            "context_window": 2048,
            "cost": {"hardware": "a100-40gb", "per_second": True},
        },
        "llama-2-13b": {
            "endpoint": "meta/llama-2-13b",
            "context_window": 4096,
            "cost": {"prompt": 0.10, "completion": 0.50},  # noqa: WPS339
        },
        "llama-2-13b-chat": {
            "endpoint": "meta/llama-2-13b-chat",
            "context_window": 4096,
            "cost": {"prompt": 0.10, "completion": 0.50},  # noqa: WPS339
        },
        "llama-2-7b": {
            "endpoint": "meta/llama-2-7b",
            "context_window": 4096,
            "cost": {"prompt": 0.05, "completion": 0.25},
        },
        "llama-2-7b-chat": {
            "endpoint": "meta/llama-2-7b-chat",
            "context_window": 4096,
            "cost": {"prompt": 0.05, "completion": 0.25},
        },
    }

    def set_api_key(self, api_key: str) -> None:  # noqa: D102
        self.api_key = api_key

    def start_prediction(  # noqa: D102
        self,
        version_id,
        input_data,
        api_token,
        api_base,
    ):
        base_url = api_base

        headers = {
            "Authorization": f"Token {api_token}",
            "Content-Type": "application/json",
        }

        initial_prediction_data = {
            "input": input_data,
        }
        if version_id:
            initial_prediction_data["version"] = version_id

        response = requests.post(  # noqa: S113
            f"{base_url}/predictions",
            json=initial_prediction_data,
            headers=headers,
        )
        if response.status_code == 201:  # noqa: WPS432
            response_data = response.json()
            return response_data.get("urls", {}).get("get")

        raise HTTPException(
            response.status_code,
            f"Failed to start prediction {response.text}",
        )

    def complete(  # noqa: D102, WPS211, C901, WPS231
        self,
        model: str,
        messages: List,  # type: ignore
        max_tokens: Optional[int] = 512,
        temperature: Optional[float] = 0.9,
        stream: Optional[bool] = False,
    ) -> Optional[Any]:
        endpoint = self.supported_models[model]["endpoint"]

        if "hardware" in self.supported_models[model]["cost"]:
            api_base = "https://api.replicate.com/v1/"
        else:
            api_base = f"https://api.replicate.com/v1/models/{endpoint}"

        self.prompt = prompt_factory(model=endpoint, messages=messages)
        input_data = {
            "prompt": self.prompt,
            "max_new_tokens": max_tokens,
            "temperature": temperature,
        }
        prediction_url = self.start_prediction(
            endpoint,
            input_data,
            self.api_key,
            api_base,
        )

        if stream:
            return (
                AsyncGeneratorWrapper(
                    self.handle_prediction_response_streaming(
                        prediction_url,
                        model,
                        endpoint,
                    ),
                    model,
                    messages,
                    compute_cost_streaming=self.compute_cost_streaming,
                    compute_cost=self.compute_cost,
                ),
                None,
            )

        response = self.handle_prediction_response(model, endpoint, prediction_url)
        return response, self.compute_cost(
            model,
            [item["content"] for item in messages],
            response,
        )

    def get_cost_max(self, model_name: str) -> float:  # noqa: D102
        if model_name not in self.supported_models:
            raise ValueError("Model not supported")
        cost_data = self.supported_models[model_name]["cost"]
        # Defined constant used to approximate maximum cost.
        # Represents the maximum time a server might take to process a request.
        max_runtime_secs = 100
        if "hardware" in cost_data:
            return (
                self.hardware_pricing_per_sec[
                    cost_data["hardware"]  # noqa: WPS529, E501
                ]
                * max_runtime_secs
            )
        return (
            self.supported_models[model_name]["cost"]["completion"]
            * self.supported_models[model_name]["context_window"]
            / PRICING_PER_TOKENS
        )

    async def handle_prediction_response_streaming(  # noqa: D102, E501, WPS210, WPS231
        self,
        prediction_url,
        model,
        endpoint,
    ):
        previous_output = ""
        output_string = ""

        headers = {
            "Authorization": f"Token {self.api_key}",
            "Content-Type": "application/json",
        }
        status = ""
        while True and (  # noqa: WPS352
            status not in ["succeeded", "failed", "canceled"]  # noqa: WPS510, E501
        ):
            await asyncio.sleep(0.5)  # prevent being rate limited by replicate
            response = requests.get(prediction_url, headers=headers)  # noqa: S113
            if response.status_code == 200:  # noqa: WPS432
                response_data = response.json()
                status = response_data["status"]
                if "output" in response_data:
                    output_string = "".join(response_data["output"])  # noqa: WPS529
                    new_output = output_string[len(previous_output) :]
                    finish_reason = "stop" if status == "succeeded" else None
                    choices = [
                        Choice(
                            finish_reason=finish_reason,
                            delta=ChoiceDelta(content=new_output, role="assistant"),
                            index=0,
                        ),
                    ]
                    created = int(time.time())

                    usage = None
                    if status == "succeeded":
                        prompt_tokens = response_data["metrics"][  # noqa: WPS220, E501
                            "input_token_count"
                        ]
                        completion_tokens = response_data["metrics"][  # noqa: WPS220
                            "output_token_count"
                        ]
                        total_tokens = prompt_tokens + completion_tokens  # noqa: WPS220
                        predict_time = response_data["metrics"][  # noqa: WPS220, E501
                            "predict_time"
                        ]
                        time_to_first_token = response_data["metrics"][  # noqa: WPS220
                            "time_to_first_token"
                        ]
                        tokens_per_second = response_data["metrics"][  # noqa: WPS220
                            "tokens_per_second"
                        ]
                        usage = CompletionUsage(  # noqa: WPS220
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                            total_tokens=total_tokens,
                            predict_time=predict_time,
                            time_to_first_token=time_to_first_token,
                            tokens_per_second=tokens_per_second,
                        )
                    yield ChatCompletionChunk(
                        choices=choices,
                        created=created,
                        model=model,
                        usage=usage,
                        id=endpoint,
                        object="chat.completion.chunk",
                    )
                    previous_output = output_string
                status = response_data["status"]
                if status == "failed":
                    replicate_error = response_data.get("error", "")
                    raise HTTPException(
                        status_code=400,  # noqa: WPS432
                        message=f"Error: {replicate_error}",
                    )

    def handle_prediction_response(  # noqa: D102, E501, WPS210, WPS231
        self,
        model,
        endpoint,
        prediction_url,
    ):
        output_string = ""
        headers = {
            "Authorization": f"Token {self.api_key}",
            "Content-Type": "application/json",
        }

        status = ""
        while True and (  # noqa: WPS352
            status not in ["succeeded", "failed", "canceled"]  # noqa: WPS510, E501
        ):
            time.sleep(0.5)
            response = requests.get(prediction_url, headers=headers)  # noqa: S113
            if response.status_code == 200:  # noqa: WPS432
                response_data = response.json()
                if "output" in response_data:
                    output_string = "".join(response_data["output"])  # noqa: WPS529
                status = response_data.get("status", None)
                if status == "failed":
                    replicate_error = response_data.get("error", "")
                    raise HTTPException(
                        status_code=400,  # noqa: WPS432
                        message=f"Error: {replicate_error}",
                    )
        timestamp = datetime.strptime(
            response_data["created_at"],
            "%Y-%m-%dT%H:%M:%S.%fZ",
        )
        created = time.mktime(timestamp.timetuple())
        prompt_tokens = response_data["metrics"]["input_token_count"]
        completion_tokens = response_data["metrics"]["output_token_count"]
        total_tokens = prompt_tokens + completion_tokens
        predict_time = response_data["metrics"]["predict_time"]
        time_to_first_token = response_data["metrics"]["time_to_first_token"]
        tokens_per_second = response_data["metrics"]["tokens_per_second"]
        usage = CompletionUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            predict_time=predict_time,
            time_to_first_token=time_to_first_token,
            tokens_per_second=tokens_per_second,
        )

        return ChatCompletion(
            id=endpoint,
            choices=[
                ChatCompletionChoice(
                    finish_reason="length",
                    index=0,
                    message=ChatCompletionMessage(
                        content=output_string,
                        role="assistant",
                    ),
                    logprobs=None,
                ),
            ],
            created=created,
            model=model,
            object="chat.completion",
            usage=usage,
            _response_ms=predict_time * 1000,
        )
