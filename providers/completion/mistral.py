from providers.completion.base_completion_provider import BaseCompletionProvider


class Mistral(BaseCompletionProvider):
    """
    A completion provider that uses the Mistral service.

    Supported models: https://docs.mistral.ai/platform/endpoints
    Pricing is per million tokens: https://docs.mistral.ai/platform/pricing
    """

    def __init__(self, hub_model, custom_api_key=None):
        super().__init__(hub_model, custom_api_key=custom_api_key)
        self.supported_models = supported_models

    @property
    def api_key_var(self) -> str:
        return "ORCHESTRA_MISTRAL_AI_API_KEY"

    @property
    def litellm_api_key_var(self) -> str:
        return "MISTRAL_API_KEY"


supported_models = {
    "mixtral-8x22b-instruct-v0.1": {
        "endpoint": "mistral/open-mixtral-8x22b",
        "context_window": 65536,
        "cost": {"prompt": 2, "completion": 6},
    },
    "mistral-7b-instruct-v0.3": {
        "endpoint": "mistral/open-mistral-7b",
        "context_window": 32768,
        "cost": {"prompt": 0.25, "completion": 0.25},
    },
    "mixtral-8x7b-instruct-v0.1": {
        "endpoint": "mistral/open-mixtral-8x7b",
        "context_window": 32768,
        "cost": {"prompt": 0.7, "completion": 0.7},
    },
    "mistral-small": {
        "endpoint": "mistral/mistral-small-latest",
        "context_window": 32768,
        "cost": {"prompt": 1, "completion": 3},
    },
    "mistral-large": {
        "endpoint": "mistral/mistral-large-latest",
        "context_window": 32768,
        "cost": {"prompt": 3, "completion": 9},
    },
    "mistral-nemo": {
        "endpoint": "mistral/open-mistral-nemo-2407",
        "context_window": 128000,
        "cost": {"prompt": 0.3, "completion": 0.3},
    },
}
