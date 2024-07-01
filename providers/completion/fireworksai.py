from providers.completion.base_completion_provider import BaseCompletionProvider


class FireworksAI(BaseCompletionProvider):
    """
    A completion provider that uses the Mistral service.

    Supported models: https://fireworks.ai/models
    Pricing is per million tokens: https://fireworks.ai/pricing
    """

    def __init__(self, hub_model):
        super().__init__(hub_model)
        self.supported_models = supported_models

    @property
    def api_key_var(self) -> str:
        return "ORCHESTRA_FIREWORKS_AI_API_KEY"

    @property
    def base_url(self):
        return "https://api.fireworks.ai/inference/v1"


supported_models = {
    "llama-3-70b-chat": {
        "endpoint": "accounts/fireworks/models/llama-v3-70b-instruct",
        "context_window": 8000,
        "cost": {"prompt": 0.9, "completion": 0.9},
    },
    "llama-3-8b-chat": {
        "endpoint": "accounts/fireworks/models/llama-v3-8b-instruct",
        "context_window": 8000,
        "cost": {"prompt": 0.2, "completion": 0.2},
    },
    "mixtral-8x22b-instruct-v0.1": {
        "endpoint": "accounts/fireworks/models/mixtral-8x22b-instruct",
        "context_window": 65536,
        "cost": {"prompt": 1.2, "completion": 1.2},
    },
    "gemma-7b-it": {
        "endpoint": "accounts/fireworks/models/gemma-7b-it",
        "context_window": 8192,
        "cost": {"prompt": 0.2, "completion": 0.2},
    },
    "llama-2-7b-chat": {
        "endpoint": "accounts/fireworks/models/llama-v2-7b-chat",
        "context_window": 4096,
        "cost": {"prompt": 0.2, "completion": 0.2},
    },
    "llama-2-13b-chat": {
        "endpoint": "accounts/fireworks/models/llama-v2-13b-chat",
        "context_window": 4096,
        "cost": {"prompt": 0.2, "completion": 0.2},
    },
    "llama-2-70b-chat": {
        "endpoint": "accounts/fireworks/models/llama-v2-70b-chat",
        "context_window": 4096,
        "cost": {"prompt": 0.9, "completion": 0.9},
    },
    "mistral-7b-instruct-v0.1": {
        "endpoint": "accounts/fireworks/models/mistral-7b-instruct-4k",
        "context_window": 16384,
        "cost": {"prompt": 0.2, "completion": 0.2},
    },
    "mistral-7b-instruct-v0.2": {
        "endpoint": "accounts/fireworks/models/mistral-7b-instruct-v0p2",
        "context_window": 32768,
        "cost": {"prompt": 0.2, "completion": 0.2},
    },
    "mixtral-8x7b-instruct-v0.1": {
        "endpoint": "accounts/fireworks/models/mixtral-8x7b-instruct",
        "context_window": 32768,
        "cost": {"prompt": 0.5, "completion": 0.5},
    },
    "codellama-70b-instruct": {
        "endpoint": "accounts/fireworks/models/llama-v2-70b-code-instruct",
        "context_window": 4096,
        "cost": {"prompt": 0.9, "completion": 0.9},
    },
    "codellama-34b-instruct": {
        "endpoint": "accounts/fireworks/models/llama-v2-34b-code-instruct",
        "context_window": 16384,
        "cost": {"prompt": 0.9, "completion": 0.9},
    },
    "codellama-13b-instruct": {
        "endpoint": "accounts/fireworks/models/llama-v2-13b-code-instruct",
        "context_window": 4096,
        "cost": {"prompt": 0.2, "completion": 0.2},
    },
}
