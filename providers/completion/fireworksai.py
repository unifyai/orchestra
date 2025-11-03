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
    "gpt-oss-20b": {
        "endpoint": "fireworks_ai/accounts/fireworks/models/gpt-oss-20b",
        "context_window": 131072,
        "cost": {"prompt": 0.07, "completion": 0.3},
    },
    "gpt-oss-120b": {
        "endpoint": "fireworks_ai/accounts/fireworks/models/gpt-oss-120b",
        "context_window": 131072,
        "cost": {"prompt": 0.15, "completion": 0.6},
    },
    "deepseek-v3.1": {
        "endpoint": "fireworks_ai/accounts/fireworks/models/deepseek-v3p1",
        "context_window": 160000,
        "cost": {"prompt": 0.56, "completion": 1.68},
    },
    "deepseek-v3-0324": {
        "endpoint": "fireworks_ai/accounts/fireworks/models/deepseek-v3-0324",
        "context_window": 160000,
        "cost": {"prompt": 0.9, "completion": 0.9},
    },
    "deepseek-r1": {
        "endpoint": "fireworks_ai/accounts/fireworks/models/deepseek-r1-0528",
        "context_window": 160000,
        "cost": {"prompt": 1.35, "completion": 5.4},
    },
    "deepseek-v3": {
        "endpoint": "fireworks_ai/accounts/fireworks/models/deepseek-v3",
        "context_window": 131072,
        "cost": {"prompt": 0.9, "completion": 0.9},
    },
    "llama-4-maverick-instruct": {
        "endpoint": "fireworks_ai/accounts/fireworks/models/llama4-maverick-instruct-basic",
        "context_window": 1048576,
        "cost": {"prompt": 0.22, "completion": 0.88},
    },
    "llama-3.3-70b-chat": {
        "endpoint": "fireworks_ai/accounts/fireworks/models/llama-v3p3-70b-instruct",
        "context_window": 128000,
        "cost": {"prompt": 0.9, "completion": 0.9},
    },
    "llama-3.1-8b-chat": {
        "endpoint": "fireworks_ai/accounts/fireworks/models/llama-v3p1-8b-instruct",
        "context_window": 131072,
        "cost": {"prompt": 0.2, "completion": 0.2},
    },
    "llama-3.1-405b-chat": {
        "endpoint": "fireworks_ai/accounts/fireworks/models/llama-v3p1-405b-instruct",
        "context_window": 131072,
        "cost": {"prompt": 3, "completion": 3},
    },
    "qwen-3-235b-a22b-instruct": {
        "endpoint": "fireworks_ai/accounts/fireworks/models/qwen3-235b-a22b-instruct-2507",
        "context_window": 256000,
        "cost": {"prompt": 0.22, "completion": 0.88},
    },
}
