from providers.completion.base_completion_provider import BaseCompletionProvider


class TogetherAI(BaseCompletionProvider):
    """
    A completion provider that uses the TogetherAI service.

    Supported models: https://docs.together.ai/docs/inference-models
    Pricing is per million tokens: https://www.together.ai/pricing
    """

    def __init__(self, hub_model, custom_endpoint=None, custom_api_key=None):
        super().__init__(
            hub_model,
            "together_ai",
            custom_endpoint=custom_endpoint,
            custom_api_key=custom_api_key,
        )
        self.supported_models = supported_models

    @property
    def api_key_var(self) -> str:
        return "ORCHESTRA_TOGETHER_AI_API_KEY"

    @property
    def litellm_api_key_var(self) -> str:
        return "TOGETHERAI_API_KEY"


supported_models = {
    "llama-3.3-70b-chat": {
        "endpoint": "together_ai/meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "context_window": 131072,
        "cost": {"prompt": 0.88, "completion": 0.88},
    },
    "llama-3.2-3b-chat": {
        "endpoint": "together_ai/meta-llama/Llama-3.2-3B-Instruct-Turbo",
        "context_window": 131072,
        "cost": {"prompt": 0.06, "completion": 0.06},
    },
    "llama-3.2-11b-chat": {
        "endpoint": "together_ai/meta-llama/Llama-3.2-11B-Vision-Instruct-Turbo",
        "context_window": 131072,
        "cost": {"prompt": 0.18, "completion": 0.18},
    },
    "llama-3.2-90b-chat": {
        "endpoint": "together_ai/meta-llama/Llama-3.2-90B-Vision-Instruct-Turbo",
        "context_window": 131072,
        "cost": {"prompt": 1.2, "completion": 1.2},
    },
    "llama-3.1-8b-chat": {
        "endpoint": "together_ai/meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo",
        "context_window": 8192,
        "cost": {"prompt": 0.18, "completion": 0.18},
    },
    "llama-3.1-70b-chat": {
        "endpoint": "together_ai/meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
        "context_window": 8192,
        "cost": {"prompt": 0.88, "completion": 0.88},
    },
    "llama-3.1-405b-chat": {
        "endpoint": "together_ai/meta-llama/Meta-Llama-3.1-405B-Instruct-Turbo",
        "context_window": 130815,
        "cost": {"prompt": 3.5, "completion": 3.5},
    },
    "llama-3-70b-chat": {
        "endpoint": "together_ai/meta-llama/Meta-Llama-3-70B-Instruct-Turbo",
        "context_window": 8192,
        "cost": {"prompt": 0.88, "completion": 0.88},
    },
    "llama-3-8b-chat": {
        "endpoint": "together_ai/meta-llama/Meta-Llama-3-8B-Instruct-Turbo",
        "context_window": 8192,
        "cost": {"prompt": 0.18, "completion": 0.18},
    },
    "gemma-2-9b-it": {
        "endpoint": "together_ai/google/gemma-2-9b-it",
        "context_window": 8192,
        "cost": {"prompt": 0.3, "completion": 0.3},
    },
    "gemma-2-27b-it": {
        "endpoint": "together_ai/google/gemma-2-27b-it",
        "context_window": 8192,
        "cost": {"prompt": 0.8, "completion": 0.8},
    },
    "mixtral-8x22b-instruct-v0.1": {
        "endpoint": "together_ai/mistralai/Mixtral-8x22B-Instruct-v0.1",
        "context_window": 65536,
        "cost": {"prompt": 1.2, "completion": 1.2},
    },
    "mistral-7b-instruct-v0.3": {
        "endpoint": "together_ai/mistralai/Mistral-7B-Instruct-v0.3",
        "context_window": 32768,
        "cost": {"prompt": 0.2, "completion": 0.2},
    },
    "mixtral-8x7b-instruct-v0.1": {
        "endpoint": "together_ai/mistralai/Mixtral-8x7B-Instruct-v0.1",
        "context_window": 32768,
        "cost": {"prompt": 0.6, "completion": 0.6},
    },
    "qwen-2.5-7b-instruct": {
        "endpoint": "together_ai/Qwen/Qwen2.5-7B-Instruct-Turbo",
        "context_window": 131072,
        "cost": {"prompt": 0.3, "completion": 0.3},
    },
    "qwen-2.5-coder-32b-instruct": {
        "endpoint": "together_ai/Qwen/Qwen2.5-Coder-32B-Instruct",
        "context_window": 131072,
        "cost": {"prompt": 0.8, "completion": 0.8},
    },
    "qwen-qwq-32b-preview": {
        "endpoint": "together_ai/Qwen/QwQ-32B-Preview",
        "context_window": 32768,
        "cost": {"prompt": 1.2, "completion": 1.2},
    },
    "qwen-2.5-72b-instruct": {
        "endpoint": "together_ai/Qwen/Qwen2.5-72B-Instruct-Turbo",
        "context_window": 131072,
        "cost": {"prompt": 1.2, "completion": 1.2},
    },
    "qwen-2-72b-instruct": {
        "endpoint": "together_ai/Qwen/Qwen2-72B-Instruct",
        "context_window": 32768,
        "cost": {"prompt": 0.9, "completion": 0.9},
    },
}
