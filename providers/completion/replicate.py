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

    def __init__(self, hub_model, custom_api_key=None):
        super().__init__(hub_model, custom_api_key=custom_api_key)
        self.supported_models = supported_models

    @property
    def api_key_var(self) -> str:
        return "ORCHESTRA_REPLICATE_API_KEY"

    @property
    def litellm_api_key_var(self) -> str:
        return "REPLICATE_API_KEY"


supported_models = {
    "mixtral-8x7b-instruct-v0.1": {
        "endpoint": "replicate/mistralai/mixtral-8x7b-instruct-v0.1",
        "context_window": 16384,
        "cost": {"prompt": 0.3, "completion": 1},
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
}
