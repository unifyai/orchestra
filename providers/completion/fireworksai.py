from providers.completion.base_completion_provider import BaseCompletionProvider


class FireworksAI(BaseCompletionProvider):
    """
    A completion provider that uses the Mistral service.

    Supported models: https://fireworks.ai/models
    Pricing is per million tokens: https://fireworks.ai/pricing
    """

    def __init__(self, hub_model, custom_api_key=None):
        super().__init__(hub_model, custom_api_key=custom_api_key)
        self.supported_models = supported_models

    @property
    def api_key_var(self) -> str:
        return "ORCHESTRA_FIREWORKS_AI_API_KEY"

    @property
    def litellm_api_key_var(self) -> str:
        return "FIREWORKS_AI_API_KEY"


supported_models = {
    "llama-3.1-8b-chat": {
        "endpoint": "fireworks_ai/accounts/fireworks/models/llama-v3p1-8b-instruct",
        "context_window": 131072,
        "cost": {"prompt": 0.2, "completion": 0.2},
    },
    "llama-3.1-70b-chat": {
        "endpoint": "fireworks_ai/accounts/fireworks/models/llama-v3p1-70b-instruct",
        "context_window": 131072,
        "cost": {"prompt": 0.9, "completion": 0.9},
    },
    "llama-3.1-405b-chat": {
        "endpoint": "fireworks_ai/accounts/fireworks/models/llama-v3p1-405b-instruct",
        "context_window": 131072,
        "cost": {"prompt": 3, "completion": 3},
    },
    "llama-3-70b-chat": {
        "endpoint": "fireworks_ai/accounts/fireworks/models/llama-v3-70b-instruct",
        "context_window": 8192,
        "cost": {"prompt": 0.9, "completion": 0.9},
    },
    "llama-3-8b-chat": {
        "endpoint": "fireworks_ai/accounts/fireworks/models/llama-v3-8b-instruct",
        "context_window": 8192,
        "cost": {"prompt": 0.2, "completion": 0.2},
    },
    "gemma-2-9b-it": {
        "endpoint": "fireworks_ai/accounts/fireworks/models/gemma2-9b-it",
        "context_window": 8192,
        "cost": {"prompt": 0.2, "completion": 0.2},
    },
    "mistral-7b-instruct-v0.2": {
        "endpoint": "fireworks_ai/accounts/fireworks/models/mistral-7b-v0p2",
        "context_window": 32768,
        "cost": {"prompt": 0.2, "completion": 0.2},
    },
    "mistral-nemo": {
        "endpoint": "fireworks_ai/accounts/fireworks/models/mistral-nemo-instruct-2407",
        "context_window": 128000,
        "cost": {"prompt": 0.2, "completion": 0.2},
    },
    "mixtral-8x22b-instruct-v0.1": {
        "endpoint": "fireworks_ai/accounts/fireworks/models/mixtral-8x22b-instruct",
        "context_window": 65536,
        "cost": {"prompt": 1.2, "completion": 1.2},
    },
    "mixtral-8x7b-instruct-v0.1": {
        "endpoint": "fireworks_ai/accounts/fireworks/models/mixtral-8x7b-instruct",
        "context_window": 32768,
        "cost": {"prompt": 0.5, "completion": 0.5},
    },
}
