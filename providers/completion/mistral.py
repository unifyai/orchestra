from providers.completion.base_completion_provider import BaseCompletionProvider


class Mistral(BaseCompletionProvider):
    """
    A completion provider that uses the Mistral service.

    Supported models: https://docs.mistral.ai/platform/endpoints
    Pricing is per million tokens: https://docs.mistral.ai/platform/pricing
    """

    def __init__(self, hub_model, custom_endpoint=None, custom_api_key=None):
        super().__init__(
            hub_model,
            "mistral",
            custom_endpoint=custom_endpoint,
            custom_api_key=custom_api_key,
        )
        self.supported_models = supported_models

    @property
    def api_key_var(self) -> str:
        return "ORCHESTRA_MISTRAL_AI_API_KEY"

    @property
    def litellm_api_key_var(self) -> str:
        return "MISTRAL_API_KEY"


supported_models = {
    "mistral-medium": {
        "endpoint": "mistral/mistral-medium-latest",
        "context_window": 128000,
        "cost": {"prompt": 0.4, "completion": 2},
    },
    "mistral-small": {
        "endpoint": "mistral/mistral-small-latest",
        "context_window": 128000,
        "cost": {"prompt": 0.1, "completion": 0.3},
    },
    "mistral-large": {
        "endpoint": "mistral/mistral-large-latest",
        "context_window": 128000,
        "cost": {"prompt": 0.5, "completion": 1.5},
    },
}
