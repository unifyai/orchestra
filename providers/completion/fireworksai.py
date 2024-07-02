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
        "context_window": 8192,
        "cost": {"prompt": 0.9, "completion": 0.9},
    },
    "llama-3-8b-chat": {
        "endpoint": "accounts/fireworks/models/llama-v3-8b-instruct",
        "context_window": 8192,
        "cost": {"prompt": 0.2, "completion": 0.2},
    },
    "mixtral-8x7b-instruct-v0.1": {
        "endpoint": "accounts/fireworks/models/mixtral-8x7b-instruct",
        "context_window": 32768,
        "cost": {"prompt": 0.5, "completion": 0.5},
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
    "mistral-7b-instruct-v0.3": {
        "endpoint": "accounts/fireworks/models/mistral-7b-instruct-v3",
        "context_window": 32768,
        "cost": {"prompt": 0.2, "completion": 0.2},
    },
    # "codellama-13b-instruct": {
    #     "endpoint": "accounts/fireworks/models/code-llama-13b-instruct",
    #     "context_window": 32768,
    #     "cost": {"prompt": 0.2, "completion": 0.2},
    # },
    # "phind-codellama-34b-v2": {
    #     "endpoint": "accounts/fireworks/models/phind-code-llama-34b-v2",
    #     "context_window": 16384,
    #     "cost": {"prompt": 0.9, "completion": 0.9},
    # },
    # "codellama-70b-instruct": {
    #     "endpoint": "accounts/fireworks/models/llama-v2-70b-code-instruct",
    #     "context_window": 4096,
    #     "cost": {"prompt": 0.9, "completion": 0.9},
    # },
    "qwen-2-72b-instruct": {
        "endpoint": "accounts/fireworks/models/qwen2-72b-instruct",
        "context_window": 32768,
        "cost": {"prompt": 0.9, "completion": 0.9},
    },
}
