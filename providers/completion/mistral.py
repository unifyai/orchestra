from providers.completion.base_completion_provider import BaseCompletionProvider


class Mistral(BaseCompletionProvider):
    """
    A completion provider that uses the Mistral service.

    Supported models: https://docs.mistral.ai/platform/endpoints
    Pricing is per million tokens: https://docs.mistral.ai/platform/pricing
    """

    def __init__(self, hub_model):
        super().__init__(hub_model)
        self.supported_models = supported_models

    @property
    def api_key_var(self) -> str:
        return "ORCHESTRA_MISTRAL_AI_API_KEY"

    @property
    def base_url(self):
        return "https://api.mistral.ai/v1"


supported_models = {
    "mixtral-8x22b-instruct-v0.1": {
        "endpoint": "open-mixtral-8x22b",
        "context_window": 65536,
        "cost": {"prompt": 2, "completion": 6},
    },
    "mistral-7b-instruct-v0.2": {
        "endpoint": "open-mistral-7b",
        "context_window": 32768,
        "cost": {"prompt": 0.25, "completion": 0.25},
    },
    "mixtral-8x7b-instruct-v0.1": {
        "endpoint": "open-mixtral-8x7b",
        "context_window": 32768,
        "cost": {"prompt": 0.7, "completion": 0.7},
    },
    "mistral-small": {
        "endpoint": "mistral-small-latest",
        "context_window": 32768,
        "cost": {"prompt": 2, "completion": 6},
    },
    "mistral-medium": {
        "endpoint": "mistral-medium-latest",
        "context_window": 32768,
        "cost": {"prompt": 2.7, "completion": 8.1},
    },
    "mistral-large": {
        "endpoint": "mistral-large-latest",
        "context_window": 32768,
        "cost": {"prompt": 8, "completion": 24},
    },
}
