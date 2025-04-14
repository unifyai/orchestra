import logging

from providers.completion.base_completion_provider import BaseCompletionProvider

logger = logging.getLogger(__name__)


class Replicate(BaseCompletionProvider):
    """
    Initializes with list of few OSS models as example.

    Source: https://replicate.com/explore
    Pricing has dual pricing: either pay for time it takes to process your request or
    per million tokens: https://replicate.com/pricing
    """

    def __init__(self, hub_model, custom_endpoint=None, custom_api_key=None):
        super().__init__(
            hub_model,
            "replicate",
            custom_endpoint=custom_endpoint,
            custom_api_key=custom_api_key,
        )
        self.supported_models = supported_models

    @property
    def api_key_var(self) -> str:
        return "ORCHESTRA_REPLICATE_API_KEY"

    @property
    def litellm_api_key_var(self) -> str:
        return "REPLICATE_API_KEY"


supported_models = {
    "llama-4-maverick-instruct": {
        "endpoint": "replicate/meta/llama-4-maverick-instruct",
        "context_window": 1000000,
        "cost": {"prompt": 0.25, "completion": 0.95},
    },
    "llama-4-scout-instruct": {
        "endpoint": "replicate/meta/llama-4-scout-instruct",
        "context_window": 10000000,
        "cost": {"prompt": 0.17, "completion": 0.65},
    },
    "llama-3-8b-chat": {
        "endpoint": "replicate/meta/meta-llama-3-8b-instruct",
        "context_window": 8192,
        "cost": {"prompt": 0.05, "completion": 0.25},
    },
    "llama-3-70b-chat": {
        "endpoint": "replicate/meta/meta-llama-3-70b-instruct",
        "context_window": 8192,
        "cost": {"prompt": 0.65, "completion": 2.75},
    },
    "llama-3.1-405b-chat": {
        "endpoint": "replicate/meta/meta-llama-3.1-405b-instruct",
        "context_window": 131072,
        "cost": {"prompt": 9.5, "completion": 9.5},
    },
    "deepseek-v3": {
        "endpoint": "replicate/deepseek-ai/deepseek-v3",
        "context_window": 64000,
        "cost": {"prompt": 1.45, "completion": 1.45},
    },
    "deepseek-r1": {
        "endpoint": "replicate/deepseek-ai/deepseek-r1",
        "context_window": 64000,
        "cost": {"prompt": 3.75, "completion": 10},
    },
    "claude-3.5-sonnet": {
        "endpoint": "replicate/anthropic/claude-3.5-sonnet",
        "context_window": 200000,
        "cost": {"prompt": 3.75, "completion": 18.75},
    },
    "claude-3.5-haiku": {
        "endpoint": "replicate/anthropic/claude-3.5-haiku",
        "context_window": 200000,
        "cost": {"prompt": 1, "completion": 5},
    },
    "claude-3.7-sonnet": {
        "endpoint": "replicate/anthropic/claude-3.7-sonnet",
        "context_window": 200000,
        "cost": {"prompt": 3, "completion": 15},
    },
}
