from providers.completion.base_completion_provider import BaseCompletionProvider


class FireworksAI(BaseCompletionProvider):
    """
    A completion provider that uses the Mistral service.

    Supported models: https://fireworks.ai/models
    Pricing is per million tokens: https://fireworks.ai/pricing
    """

    def __init__(self, hub_model, custom_endpoint=None, custom_api_key=None):
        super().__init__(
            hub_model,
            "fireworks_ai",
            custom_endpoint=custom_endpoint,
            custom_api_key=custom_api_key,
        )
        self.supported_models = supported_models

    @property
    def api_key_var(self) -> str:
        return "ORCHESTRA_FIREWORKS_AI_API_KEY"

    @property
    def litellm_api_key_var(self) -> str:
        return "FIREWORKS_AI_API_KEY"


supported_models = {
    "llama-3.3-70b-chat": {
        "endpoint": "fireworks_ai/accounts/fireworks/models/llama-v3p3-70b-instruct",
        "context_window": 128000,
        "cost": {"prompt": 0.9, "completion": 0.9},
    },
    "llama-3.2-3b-chat": {
        "endpoint": "fireworks_ai/accounts/fireworks/models/llama-v3p2-3b-instruct",
        "context_window": 131072,
        "cost": {"prompt": 0.1, "completion": 0.1},
    },
    "llama-3.2-11b-chat": {
        "endpoint": "fireworks_ai/accounts/fireworks/models/llama-v3p2-11b-vision-instruct",
        "context_window": 131072,
        "cost": {"prompt": 0.2, "completion": 0.2},
    },
    "llama-3.2-90b-chat": {
        "endpoint": "fireworks_ai/accounts/fireworks/models/llama-v3p2-90b-vision-instruct",
        "context_window": 131072,
        "cost": {"prompt": 0.9, "completion": 0.9},
    },
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
    "qwen-2.5-coder-32b-instruct": {
        "endpoint": "fireworks_ai/accounts/fireworks/models/qwen2p5-coder-32b-instruct",
        "context_window": 32768,
        "cost": {"prompt": 0.9, "completion": 0.9},
    },
    "qwen-qwq-32b-preview": {
        "endpoint": "fireworks_ai/accounts/fireworks/models/qwen-qwq-32b-preview",
        "context_window": 32768,
        "cost": {"prompt": 0.9, "completion": 0.9},
    },
    "qwen-2.5-72b-instruct": {
        "endpoint": "fireworks_ai/accounts/fireworks/models/qwen-v2p5-72b-instruct",
        "context_window": 32768,
        "cost": {"prompt": 0.9, "completion": 0.9},
    },
}
