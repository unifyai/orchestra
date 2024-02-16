import os
import replicate
import providers.completion.replicate_run as r8r
from datetime import datetime
from typing import Any, List, Optional
from providers.completion.base_completion_provider import (
    BaseCompletionProvider,
    SyncGeneratorWrapper,
    AsyncGeneratorWrapper,
)


class Replicate(BaseCompletionProvider):
    """
    Initializes with list of few OSS models as example.

    Source: https://replicate.com/explore
    Pricing has dual pricing: either pay for time it takes to process your request or
    per million tokens: https://replicate.com/pricing
    """

    def __init__(self, hub_model):
        super().__init__(hub_model)
        self.supported_models = supported_models
        os.environ["REPLICATE_API_TOKEN"] = self.api_key

    @property
    def api_key_var(self) -> str:
        return "ORCHESTRA_REPLICATE_API_KEY"

    @property
    def base_url(self):
        return f"https://api.replicate.com/v1/models/{self.provider_endpoint}"

    @staticmethod
    def usage_from_response(response):
        return {
            "prompt_tokens": response.metrics["input_token_count"],
            "completion_tokens": response.metrics["output_token_count"],
            "total_tokens": response.metrics["input_token_count"]
            + response.metrics["output_token_count"],
        }

    @staticmethod
    def str_to_ts(str):
        parsed = datetime.strptime(str, "%Y-%m-%dT%H:%M:%S.%fZ")
        return int(parsed.timestamp())

    def response_to_chat_completion(self, response):
        created_at = self.str_to_ts(response.created_at)
        return dict(
            id=response.id,
            choices=[
                dict(
                    finish_reason="length",  # TODO
                    index=0,
                    message=dict(
                        content=" ".join(response.output),
                        role="assistant",
                    ),
                    logprobs=None,
                ),
            ],
            created=created_at,
            model=self.hub_model,
            object="chat.completion",
            usage=self.usage_from_response(response),
        )

    def __call__(
        self, messages: List, stream: bool = False, **kwargs: Any
    ) -> Optional[Any]:
        # TODO: Ensure that messages is only one message long
        # TODO: system prompt is not supported in all models
        # TODO: Deal with rate limits
        # TODO: Get inputs and outputs from every model:
        # TODO: Add exceptions
        # TODO: kwargs need to be cleaned depending on the model
        # TODO: Prompt factory needs to be model specific (family)
        # https://replicate.com/docs/reference/http#models.get
        _messages = messages[:]
        r8_kwargs = {}
        if _messages[0]["role"] == "system":
            r8_kwargs["system_prompt"] = _messages[0]["content"]
            _messages.pop(0)
        prompt = self.prompt_factory(_messages)
        if stream:
            response = replicate.stream(
                self.provider_endpoint,
                input={"prompt": prompt},
                **r8_kwargs,
                # **kwargs, # TODO
            )
            return (R8SyncGeneratorWrapper(self, response, messages), None)
        else:
            response = r8r.run(
                replicate.default_client,
                self.provider_endpoint,
                input={"prompt": prompt},
                **r8_kwargs,
                # **kwargs, TODO
            )
            return (
                self.response_to_chat_completion(response),
                self.compute_cost(
                    response.metrics["input_token_count"],
                    response.metrics["output_token_count"],
                ),
            )

    def __call_async__(
        self, messages: List, stream: bool = False, **kwargs: Any
    ) -> Optional[Any]:
        _messages = messages[:]
        r8_kwargs = {}
        if _messages[0]["role"] == "system":
            r8_kwargs["system_prompt"] = _messages[0]["content"]
            _messages.pop(0)
        prompt = self.prompt_factory(_messages)
        if stream:
            response = replicate.async_stream(
                self.provider_endpoint,
                input={"prompt": prompt},
                # **r8_kwargs,
                # **kwargs, # TODO
            )
            return (R8AsyncGeneratorWrapper(self, response, messages), None)
        else:
            response = r8r.async_run(
                replicate.default_client,
                self.provider_endpoint,
                input={"prompt": prompt},
                **r8_kwargs,
                # **kwargs, TODO
            )
            return (
                self.response_to_chat_completion(response),
                self.compute_cost(
                    response.metrics["input_token_count"],
                    response.metrics["output_token_count"],
                ),
            )

    def prompt_factory(self, messages):
        return "\n".join(
            (
                f"[INST] {message['content']} [/INST]"
                if message["role"] == "user"
                else message["content"]
            )
            for message in messages
        )


class R8SyncGeneratorWrapper(SyncGeneratorWrapper):
    def generator_iteration(self, part, whole):
        return sse_to_part_dict(part, whole), False


class R8AsyncGeneratorWrapper(AsyncGeneratorWrapper):
    def generator_iteration(self, part, whole):
        return sse_to_part_dict(part, whole), False


def sse_to_part_dict(part, whole):
    part_dict = {
        "id": part.id,
        "object": "chat.completion.chunk",
        "created": int(part.id.split(":")[0]),
        "choices": [
            {
                "index": 0,
                "delta": {"content": part.data},
                "logprobs": None,  # TODO?
                "finish_reason": None,  # TODO
            }
        ],
    }
    if part.data == "":
        return None
    if not whole:
        whole.extend([""])
    whole[0] += part.data
    return part_dict


supported_models = {
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
    "llama-2-70b-chat": {
        "endpoint": "meta/llama-2-70b-chat",
        "context_window": 4096,
        "cost": {"prompt": 0.65, "completion": 2.75},
    },
    "llama-2-13b-chat": {
        "endpoint": "meta/llama-2-13b-chat",
        "context_window": 4096,
        "cost": {"prompt": 0.10, "completion": 0.50},  # noqa: WPS339
    },
    "llama-2-7b-chat": {
        "endpoint": "meta/llama-2-7b-chat",
        "context_window": 4096,
        "cost": {"prompt": 0.05, "completion": 0.25},
    },
}
