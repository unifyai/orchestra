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
    "mistral-7b-instruct-v0.2": {
        "endpoint": "mistral-tiny",
        "context_window": 32768,
        "cost": {"prompt": 0.15, "completion": 0.46},
    },
    "mixtral-8x7b-instruct-v0.1": {
        "endpoint": "mistral-small",
        "context_window": 32768,
        "cost": {"prompt": 0.66, "completion": 1.97},
    },
    "mistral-medium": {
        "endpoint": "mistral-medium",
        "context_window": 32768,
        "cost": {"prompt": 2.74, "completion": 8.21},
    },
}
