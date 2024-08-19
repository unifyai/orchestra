import os
import time
from datetime import datetime
from typing import Any, List

import aioboto3
import boto3
from providers.completion.base_completion_provider import (
    BaseCompletionProvider,
    SyncGeneratorWrapper,
)


class AWSBedrock(BaseCompletionProvider):  # noqa: WPS338
    """
    A completion provider that uses the AWS Bedrock service.

    Source: https://docs.aws.amazon.com/bedrock/latest/userguide/model-ids.html
    Pricing is per thousand tokens: https://aws.amazon.com/bedrock/pricing/
    """

    def __init__(self, hub_model, custom_api_key=None):
        super().__init__(hub_model, custom_api_key=custom_api_key)
        self.supported_models = supported_models

    # TODO Same as replicate, move to utils
    @staticmethod
    def str_to_ts(str):  # noqa: D102, WPS125
        parsed = datetime.strptime(str, "%a, %d %b %Y %H:%M:%S %Z")
        return int(parsed.timestamp())

    @staticmethod
    def usage_from_response(response):
        prompt_tokens = int(response["x-amzn-bedrock-input-token-count"])
        completion_tokens = int(response["x-amzn-bedrock-output-token-count"])
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }

    def client(self, region_name="us-west-2"):
        return boto3.client(service_name="bedrock-runtime", region_name=region_name)

    def __call__(
        self,
        messages: List,
        stream: bool = False,
        **kwargs: Any,
    ) -> Any:  # noqa: WPS210
        region = _get_region(self.provider_endpoint)
        client = self.client(region)

        system_prompts, messages = _format_messages_for_converse(messages)
        inference_config, additional_model_fields = _format_kwargs_for_converse(kwargs)

        if stream:
            response = client.converse_stream(
                modelId=self.provider_endpoint,
                messages=messages,
                system=system_prompts,
                inferenceConfig=inference_config,
                additionalModelRequestFields=additional_model_fields,
            )

            return (BedrockSyncGeneratorWrapper(self, response, messages), None)

        response = client.converse(
            modelId=self.provider_endpoint,
            messages=messages,
            system=system_prompts,
            inferenceConfig=inference_config,
            additionalModelRequestFields=additional_model_fields,
        )
        return (
            self.response_to_chat_completion(response),
            self.compute_cost(
                int(
                    response["usage"]["inputTokens"],
                ),
                int(
                    response["usage"]["outputTokens"],
                ),
            ),
        )

    def __call_async__(
        self,
        messages: List,
        stream: bool = False,
        **kwargs: Any,
    ) -> Any:
        return (
            BedrockAsyncGeneratorWrapper(self, None, messages, kwargs),
            None,
        )

    def response_to_chat_completion(self, response):
        metadata = response["ResponseMetadata"]["HTTPHeaders"]
        created_at = self.str_to_ts(metadata["date"])
        finish_reason = response["stopReason"]
        usage = {
            "prompt_tokens": response["usage"]["inputTokens"],
            "completion_tokens": response["usage"]["outputTokens"],
            "total_tokens": response["usage"]["totalTokens"],
        }
        content = response["output"]["message"]["content"][0]["text"]

        return dict(
            id=metadata["x-amzn-requestid"],
            choices=[
                dict(
                    finish_reason=finish_reason,  # TODO: check if these are the actual finish reason strings, else modify output
                    index=0,
                    message=dict(
                        content=content,
                        role="assistant",
                    ),
                    logprobs=None,
                ),
            ],
            created=created_at,
            model=self.hub_model,
            object="chat.completion",
            usage=usage,
        )


class BedrockSyncGeneratorWrapper(SyncGeneratorWrapper):
    def generator_iteration(self, part, whole):
        return sse_to_part_dict(part, whole, self.provider.provider_endpoint)

    def generator(self):  # noqa: D102, C901, WPS210, WPS231
        whole = []
        self.prompt_tokens = 0
        self.completion_tokens = 0
        stream = self._response.get("stream")
        for event in stream:
            if "messageStart" in event:
                pass  # TODO: for tool use
            if "contentBlockDelta" in event:
                content = event["contentBlockDelta"]["delta"]["text"]
            else:
                content = ""
            if "messageStop" in event:
                finish_reason = event["messageStop"]["stopReason"]
            else:
                finish_reason = ""

            if "metadata" in event:
                usage_raw = event["metadata"]["usage"]
                tc = self.provider.compute_cost(
                    int(
                        usage_raw["inputTokens"],
                    ),
                    int(
                        usage_raw["outputTokens"],
                    ),
                )
                self.total_cost = tc
                usage = {
                    "cost": tc,
                    "prompt_tokens": usage_raw["inputTokens"],
                    "completion_tokens": usage_raw["outputTokens"],
                    "total_tokens": usage_raw["totalTokens"],
                }
            else:
                usage = {}
            part_dict = {
                "id": str(id(event)),
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": content},
                        "logprobs": None,  # TODO?
                        "finish_reason": finish_reason,
                    },
                ],
                "usage": usage,
            }
            yield part_dict


class BedrockAsyncGeneratorWrapper(SyncGeneratorWrapper):
    def __init__(self, provider, response, messages, body):
        super().__init__(provider, response, messages)
        self._body = body

    def generator_iteration(self, part, whole):
        return sse_to_part_dict(part, whole, self.provider.provider_endpoint)

    async def generator(self):  # noqa: D102, C901, WPS210, WPS231
        whole = []
        self.prompt_tokens = 0
        self.completion_tokens = 0

    async def generator(self):  # noqa: D102, C901, WPS210, WPS231
        whole = []

        session = aioboto3.Session()
        system_prompts, messages = _format_messages_for_converse(messages)
        inference_config, additional_model_fields = _format_kwargs_for_converse(
            self._body,
        )
        async with session.client(
            service_name="bedrock-runtime",
            region_name=_get_region(self.provider.provider_endpoint),
        ) as client:
            self._response = await client.converse_stream(
                modelId=self.provider_endpoint,
                messages=messages,
                system=system_prompts,
                inferenceConfig=inference_config,
                additionalModelRequestFields=additional_model_fields,
            )

            stream = self._response.get("stream")

            async for event in stream:
                if "messageStart" in event:
                    pass  # TODO: for tool use
                if "contentBlockDelta" in event:
                    content = event["contentBlockDelta"]["delta"]["text"]
                else:
                    content = ""
                if "messageStop" in event:
                    finish_reason = event["messageStop"]["stopReason"]
                else:
                    finish_reason = ""

                if "metadata" in event:
                    usage_raw = event["metadata"]["usage"]
                    usage = {
                        "cost": self.provider.compute_cost(
                            int(
                                usage_raw["inputTokens"],
                            ),
                            int(
                                usage_raw["outputTokens"],
                            ),
                        ),
                        "prompt_tokens": usage_raw["inputTokens"],
                        "completion_tokens": usage_raw["outputTokens"],
                        "total_tokens": usage_raw["totalTokens"],
                    }
                else:
                    usage = {}
                part_dict = {
                    "id": str(id(event)),
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": content},
                            "logprobs": None,  # TODO?
                            "finish_reason": finish_reason,
                        },
                    ],
                    "usage": usage,
                }
                yield part_dict


def sse_to_part_dict(part, whole, endpoint):
    if "mistral" in endpoint:
        finish_reason = part["outputs"][0]["stop_reason"]
        data = part["outputs"][0]["text"]
    elif "llama" in endpoint:
        finish_reason = part["stop_reason"]
        data = part["generation"]
    else:
        finish_reason = part["finish_reason"] if part["is_finished"] else ""
        data = part["text"] if "text" in part else ""

    # TODO Handle usage properly after the refactor
    if "amazon-bedrock-invocationMetrics" in part:
        metrics = part["amazon-bedrock-invocationMetrics"]
        prompt_tokens = int(metrics["inputTokenCount"])
        completion_tokens = int(metrics["outputTokenCount"])
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
    # TODO This returns usage in every stream, but with llama 13b, stop reason is not returend as well. have to handle this in the refactor
    elif "llama" in endpoint:
        prompt_tokens = part["prompt_token_count"]
        completion_tokens = part["generation_token_count"]
        prompt_tokens = 0 if prompt_tokens is None else prompt_tokens
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
    else:
        usage = {}
    part_dict = {
        "id": part["id"],
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "choices": [
            {
                "index": 0,
                "delta": {"content": data},
                "logprobs": None,  # TODO?
                "finish_reason": finish_reason,  # TODO: check if the str of the reaosn provided is OAI
            },
        ],
        "usage": usage,
    }

    if not whole:
        whole.extend([""])
    whole[0] += data
    return part_dict


def _get_region(provider_endpoint):
    region = os.environ.get("AWS_REGION")
    if os.environ.get("ON_PREM") and region:
        return region
    if "anthropic" in provider_endpoint and not "opus" in provider_endpoint:
        return "us-east-1"
    return "us-west-2"


def _format_messages_for_converse(messages):
    system_prompts = []
    new_messages = []
    for msg in messages:
        txt = msg["content"]
        if msg["role"] == "system":
            system_prompts.append({"text": msg["content"]})
        else:
            msg["content"] = [{"text": txt}]
            new_messages.append(msg)

    return system_prompts, new_messages


def _format_kwargs_for_converse(kwargs):
    """takes a dict of kwargs and returns the two dictionaries to pass to the
    converse api.
    """
    converse_api_param_map = {
        "maxTokens": "max_tokens",
        "stopSequences": "stop",
        "temperature": "temperature",
        "topP": "top_p",
    }
    additional_model_params = [
        "top_k",
    ]  # TODO: add more of these

    inference_config = {}
    for param_name in converse_api_param_map:
        if converse_api_param_map[param_name] in kwargs:
            inference_config[param_name] = kwargs[converse_api_param_map[param_name]]

    additional_model_fields = {}
    for param_name in additional_model_params:
        if param_name in kwargs:
            additional_model_fields[param_name] = kwargs[param_name]
    return inference_config, additional_model_fields


supported_models = {
    "llama-3.1-8b-chat": {
        "endpoint": "meta.llama3-1-8b-instruct-v1:0",
        "context_window": 128000,
        "cost": {"prompt": 0.22, "completion": 0.22},
    },
    "llama-3.1-70b-chat": {
        "endpoint": "meta.llama3-1-70b-instruct-v1:0",
        "context_window": 128000,
        "cost": {"prompt": 0.99, "completion": 0.99},
    },
    "llama-3.1-405b-chat": {
        "endpoint": "meta.llama3-1-405b-instruct-v1:0",
        "context_window": 128000,
        "cost": {"prompt": 5.32, "completion": 16},
    },
    "llama-3-8b-chat": {
        "endpoint": "meta.llama3-8b-instruct-v1:0",
        "context_window": 8192,
        "cost": {"prompt": 0.3, "completion": 0.6},
    },
    "llama-3-70b-chat": {
        "endpoint": "meta.llama3-70b-instruct-v1:0",
        "context_window": 8192,
        "cost": {"prompt": 2.65, "completion": 3.5},
    },
    "mistral-7b-instruct-v0.2": {
        "endpoint": "mistral.mistral-7b-instruct-v0:2",
        "context_window": 32768,
        "cost": {"prompt": 0.15, "completion": 0.2},
    },
    "mixtral-8x7b-instruct-v0.1": {
        "endpoint": "mistral.mixtral-8x7b-instruct-v0:1",
        "context_window": 32768,
        "cost": {"prompt": 0.45, "completion": 0.7},
    },
    "mistral-large": {
        "endpoint": "mistral.mistral-large-2402-v1:0",
        "context_window": 32000,
        "cost": {"prompt": 4, "completion": 12},
    },
    "command-r-plus": {
        "endpoint": "cohere.command-r-plus-v1:0",
        "context_window": 128000,
        "cost": {"prompt": 3, "completion": 15},
    },
    "claude-3-haiku": {
        "endpoint": "anthropic.claude-3-haiku-20240307-v1:0",
        "context_window": 200000,
        "cost": {"prompt": 0.25, "completion": 1.25},
    },
    "claude-3-sonnet": {
        "endpoint": "anthropic.claude-3-sonnet-20240229-v1:0",
        "context_window": 200000,
        "cost": {"prompt": 3, "completion": 15},
    },
    "claude-3-opus": {
        "endpoint": "anthropic.claude-3-opus-20240229-v1:0",
        "context_window": 200000,
        "cost": {"prompt": 15, "completion": 75},
    },
    "claude-3.5-sonnet": {
        "endpoint": "anthropic.claude-3-5-sonnet-20240620-v1:0",
        "context_window": 200000,
        "cost": {"prompt": 3, "completion": 15},
    },
}
