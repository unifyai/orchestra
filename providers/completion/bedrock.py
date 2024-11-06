import os
from typing import Any, List

from providers.completion.base_completion_provider import BaseCompletionProvider


class AWSBedrock(BaseCompletionProvider):  # noqa: WPS338
    """
    A completion provider that uses the AWS Bedrock service.

    Source: https://docs.aws.amazon.com/bedrock/latest/userguide/model-ids.html
    Pricing is per thousand tokens: https://aws.amazon.com/bedrock/pricing/
    """

    def __init__(self, hub_model, custom_endpoint=None, custom_api_key=None):
        super().__init__(
            hub_model,
            "bedrock",
            custom_endpoint=custom_endpoint,
            custom_api_key=custom_api_key,
        )
        self.supported_models = supported_models

    @property
    def api_key_var(self) -> str:
        return "AWS_SECRET_ACCESS_KEY"

    @property
    def litellm_api_key_var(self) -> str:
        return "AWS_SECRET_ACCESS_KEY"

    def get_region(self):
        region = os.environ.get("AWS_REGION")
        if os.environ.get("ON_PREM") and region:
            return region
        if (
            "anthropic" in self.provider_endpoint
            and not "opus" in self.provider_endpoint
            and not "sonnet-20240620" not in self.provider_endpoint
        ):
            return "us-east-1"
        return "us-west-2"

    def __call__(
        self,
        messages: List,
        stream: bool = False,
        **kwargs: Any,
    ) -> Any:  # noqa: WPS210
        kwargs_region = kwargs.pop("region", None)
        region = kwargs_region if kwargs_region else self.get_region()
        kwargs["aws_region_name"] = region
        return super().__call__(messages, stream, **kwargs)

    def __call_async__(
        self,
        messages: List,
        stream: bool = False,
        **kwargs: Any,
    ) -> Any:
        kwargs_region = kwargs.pop("region", None)
        region = kwargs_region if kwargs_region else self.get_region()
        kwargs["aws_region_name"] = region
        return super().__call__(messages, stream, **kwargs)


supported_models = {
    "llama-3.2-1b-chat": {
        "endpoint": "bedrock/us.meta.llama3-2-1b-instruct-v1:0",
        "context_window": 131072,
        "cost": {"prompt": 0.1, "completion": 0.1},
    },
    "llama-3.2-3b-chat": {
        "endpoint": "bedrock/us.meta.llama3-2-3b-instruct-v1:0",
        "context_window": 131072,
        "cost": {"prompt": 0.15, "completion": 0.15},
    },
    "llama-3.1-8b-chat": {
        "endpoint": "bedrock/meta.llama3-1-8b-instruct-v1:0",
        "context_window": 128000,
        "cost": {"prompt": 0.22, "completion": 0.22},
    },
    "llama-3.1-70b-chat": {
        "endpoint": "bedrock/meta.llama3-1-70b-instruct-v1:0",
        "context_window": 128000,
        "cost": {"prompt": 0.99, "completion": 0.99},
    },
    "llama-3.1-405b-chat": {
        "endpoint": "bedrock/meta.llama3-1-405b-instruct-v1:0",
        "context_window": 128000,
        "cost": {"prompt": 5.32, "completion": 16},
    },
    "llama-3-8b-chat": {
        "endpoint": "bedrock/meta.llama3-8b-instruct-v1:0",
        "context_window": 8192,
        "cost": {"prompt": 0.3, "completion": 0.6},
    },
    "llama-3-70b-chat": {
        "endpoint": "bedrock/meta.llama3-70b-instruct-v1:0",
        "context_window": 8192,
        "cost": {"prompt": 2.65, "completion": 3.5},
    },
    "mistral-7b-instruct-v0.2": {
        "endpoint": "bedrock/mistral.mistral-7b-instruct-v0:2",
        "context_window": 32768,
        "cost": {"prompt": 0.15, "completion": 0.2},
    },
    "mixtral-8x7b-instruct-v0.1": {
        "endpoint": "bedrock/mistral.mixtral-8x7b-instruct-v0:1",
        "context_window": 32768,
        "cost": {"prompt": 0.45, "completion": 0.7},
    },
    "mistral-large": {
        "endpoint": "bedrock/mistral.mistral-large-2407-v1:0",
        "context_window": 128000,
        "cost": {"prompt": 2, "completion": 6},
    },
    "command-r-plus": {
        "endpoint": "bedrock/cohere.command-r-plus-v1:0",
        "context_window": 128000,
        "cost": {"prompt": 3, "completion": 15},
    },
    "claude-3-haiku": {
        "endpoint": "bedrock/us.anthropic.claude-3-haiku-20240307-v1:0",
        "context_window": 200000,
        "cost": {"prompt": 0.25, "completion": 1.25},
    },
    "claude-3-sonnet": {
        "endpoint": "bedrock/us.anthropic.claude-3-sonnet-20240229-v1:0",
        "context_window": 200000,
        "cost": {"prompt": 3, "completion": 15},
    },
    "claude-3-opus": {
        "endpoint": "bedrock/us.anthropic.claude-3-opus-20240229-v1:0",
        "context_window": 200000,
        "cost": {"prompt": 15, "completion": 75},
    },
    "claude-3.5-sonnet": {
        "endpoint": "bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0",
        "context_window": 200000,
        "cost": {"prompt": 3, "completion": 15},
    },
    "claude-3.5-sonnet-20240620": {
        "endpoint": "bedrock/us.anthropic.claude-3-5-sonnet-20240620-v1:0",
        "context_window": 200000,
        "cost": {"prompt": 3, "completion": 15},
    },
    "claude-3.5-haiku": {
        "endpoint": "bedrock/us.anthropic.claude-3-5-haiku-20241022-v1:0",
        "context_window": 200000,
        "cost": {"prompt": 1, "completion": 5},
    },
}
