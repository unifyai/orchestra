import time
import uuid
from datetime import datetime
from typing import Any, List, Optional

import requests
from fastapi import HTTPException
from litellm.llms.prompt_templates.factory import prompt_factory
from litellm.utils import (
    Choices,
    Delta,
    Message,
    ModelResponse,
    StreamingChoices,
    Usage,
)
from providers.completion.base_completion_provider import (
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
            "endpoint": "replicate/mistralai/mistral-7b-instruct-v0.1:83b6a56e7c828e667f21fd596c338fd4f0039b46bcfa18d973e8e70e455fda70",  # noqa: E501
            "context_window": 16384,
            "cost": {"hardware": "a40", "per_second": True},
        },
        "mistral-7b-instruct-v0.2": {
            "endpoint": "replicate/mistralai/mistral-7b-instruct-v0.2",
            "context_window": 16384,
            "cost": {"prompt": 0.05, "completion": 0.25},
        },
        "mixtral-8x7b-instruct-v0.1": {
            "endpoint": "replicate/mistralai/mixtral-8x7b-instruct-v0.1",
            "context_window": 16384,
            "cost": {"prompt": 0.30, "completion": 1.00},  # noqa: WPS339
        },
        "mistral-7b-v0.1": {
            "endpoint": "replicate/mistralai/mistral-7b-v0.1",
            "context_window": 4096,
            "cost": {"prompt": 0.05, "completion": 0.25},
        },
        "llama-2-70b": {
            "endpoint": "meta/llama-2-70b",
            "context_window": 4096,
            "cost": {"prompt": 0.65, "completion": 2.75},
        },
        "llama-2-70b-chat": {
            "endpoint": "replicate/meta/llama-2-70b-chat",
            "context_window": 4096,
            "cost": {"prompt": 0.65, "completion": 2.75},
        },
        "gpt-j-6b": {
            "endpoint": "replicate/gpt-j-6b:b3546aeec6c9891f0dd9929c2d3bedbf013c12e02e7dd0346af09c37e008c827",  # noqa: E501
            "context_window": 2048,
            "cost": {"hardware": "a100-40gb", "per_second": True},
        },
        "llama-2-13b": {
            "endpoint": "replicate/meta/llama-2-13b",
            "context_window": 4096,
            "cost": {"prompt": 0.10, "completion": 0.50},  # noqa: WPS339
        },
        "llama-2-13b-chat": {
            "endpoint": "replicate/meta/llama-2-13b-chat",
            "context_window": 4096,
            "cost": {"prompt": 0.10, "completion": 0.50},  # noqa: WPS339
        },
        "llama-2-7b": {
            "endpoint": "meta/llama-2-7b",
            "context_window": 4096,
            "cost": {"prompt": 0.05, "completion": 0.25},
        },
        "llama-2-7b-chat": {
            "endpoint": "replicate/meta/llama-2-7b-chat",
            "context_window": 4096,
            "cost": {"prompt": 0.05, "completion": 0.25},
        },
        "codellama-7b-instruct-gguf": {
            "endpoint": "e0c796e98861b1e30f43ad63071936875d6c88351093dc036180472d968dac5e",  # noqa: E501
            "context_window": 16384,
            "cost": {"hardware": "a40", "per_second": True},
        },
        "llama-2-13b-chat-gguf": {
            "endpoint": "60ec5dda9ff9ee0b6f786c9d1157842e6ab3cc931139ad98fe99e08a35c5d4d4",  # noqa: E501
            "context_window": 16384,
            "cost": {"hardware": "a40", "per_second": True},
        },
        "llama-2-70b-chat-gguf": {
            "endpoint": "51b87745820e6a8de6ad7bceb340bb6ba85f7ba6dab8e02bb7e2de0853425f4c",  # noqa: E501
            "context_window": 16384,
            "cost": {"hardware": "a40", "per_second": True},
        },
        "llama-2-13b-gguf": {
            "endpoint": "f705c8ea4ab595d627754bbdb4c3a8c1344eab9a0082e31d553692fa0532eb07",  # noqa: E501
            "context_window": 16384,
            "cost": {"hardware": "a40", "per_second": True},
        },
        "wizardcoder-python-34b-v1-gguf": {
            "endpoint": "67eed332a5389263b8ede41be3ee7dc119fa984e2bde287814c4abed19a45e54",  # noqa: E501
            "context_window": 16384,
            "cost": {"hardware": "a40", "per_second": True},
        },
        "codellama-34b-instruct-gguf": {
            "endpoint": "f1091fa795c142a018268b193c9eea729e0a3f4d55d723df0b69f17b863bf5ea",  # noqa: E501
            "context_window": 16384,
            "cost": {"hardware": "a40", "per_second": True},
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
            "max_tokens": max_tokens,
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
                    self.handle_prediction_response_streaming(prediction_url),
                    model,
                    messages,
                    compute_cost_streaming=self.compute_cost_streaming,
                    compute_cost=self.compute_cost,
                ),
                None,
            )

        response = self.handle_prediction_response(prediction_url)
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
        return 0

    def handle_prediction_response_streaming(  # noqa: D102, E501, WPS210, WPS231
        self,
        prediction_url,
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
            time.sleep(0.5)  # prevent being rate limited by replicate
            response = requests.get(prediction_url, headers=headers)  # noqa: S113
            if response.status_code == 200:  # noqa: WPS432
                response_data = response.json()
                status = response_data["status"]
                if "output" in response_data:
                    output_string = "".join(response_data["output"])  # noqa: WPS529
                    new_output = output_string[len(previous_output) :]
                    finish_reason = "stop" if status == "succeeded" else None
                    choices = [
                        StreamingChoices(
                            finish_reason=finish_reason,
                            delta=Delta(content=new_output, role="assistant"),
                        ),
                    ]
                    created = time.time()

                    usage = Usage()
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
                        usage = Usage(  # noqa: WPS220
                            prompt_tokens,
                            completion_tokens,
                            total_tokens,
                            predict_time=predict_time,
                            time_to_first_token=time_to_first_token,
                            tokens_per_second=tokens_per_second,
                        )
                    yield {"choices": choices, "created": created, "usage": usage}
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
        start_time = datetime.strptime(
            response_data["started_at"],
            "%Y-%m-%dT%H:%M:%S.%fZ",
        )
        end_time = datetime.strptime(
            response_data["completed_at"],
            "%Y-%m-%dT%H:%M:%S.%fZ",
        )
        prompt_tokens = response_data["metrics"]["input_token_count"]
        completion_tokens = response_data["metrics"]["output_token_count"]
        total_tokens = prompt_tokens + completion_tokens
        predict_time = response_data["metrics"]["predict_time"]
        time_to_first_token = response_data["metrics"]["time_to_first_token"]
        tokens_per_second = response_data["metrics"]["tokens_per_second"]
        usage = Usage(
            prompt_tokens,
            completion_tokens,
            total_tokens,
            predict_time=predict_time,
            time_to_first_token=time_to_first_token,
            tokens_per_second=tokens_per_second,
        )

        response_ms = (end_time - start_time).total_seconds() * 1000

        response = ModelResponse(
            id=f"chatcmpl-{str(uuid.uuid4())}",  # noqa: WPS237
            choices=[Choices(message=Message(), index=0, finish_reason="stop")],
            created=created,
            response_ms=response_ms,
            usage=usage,
            object="chat.completion",
        )
        response["choices"][0]["message"]["content"] = output_string
        return response
