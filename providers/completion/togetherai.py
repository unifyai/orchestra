from providers.completion.base_completion_provider import BaseCompletionProvider


class TogetherAI(BaseCompletionProvider):
    """
    A completion provider that uses the TogetherAI service.

    Supported models: https://docs.together.ai/docs/inference-models
    Pricing is per million tokens: https://www.together.ai/pricing
    """

    def __init__(self, hub_model, custom_api_key=None):
        super().__init__(hub_model, custom_api_key=custom_api_key)
        self.supported_models = supported_models

    @property
    def api_key_var(self) -> str:
        return "ORCHESTRA_TOGETHER_AI_API_KEY"

    @property
    def litellm_api_key_var(self) -> str:
        return "TOGETHERAI_API_KEY"


supported_models = {
    "llama-3.1-8b-chat": {
        "endpoint": "together_ai/meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo",
        "context": 8192,
        "cost": {"prompt": 0.18, "completion": 0.18},
    },
    "llama-3.1-70b-chat": {
        "endpoint": "together_ai/meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
        "context": 8192,
        "cost": {"prompt": 0.88, "completion": 0.88},
    },
    "llama-3.1-405b-chat": {
        "endpoint": "together_ai/meta-llama/Meta-Llama-3.1-405B-Instruct-Turbo",
        "context": 32768,
        "cost": {"prompt": 5, "completion": 5},
    },
    "llama-3-70b-chat": {
        "endpoint": "together_ai/meta-llama/Llama-3-70b-chat-hf",
        "context_window": 8192,
        "cost": {"prompt": 0.9, "completion": 0.9},
    },
    "llama-3-8b-chat": {
        "endpoint": "together_ai/meta-llama/Llama-3-8b-chat-hf",
        "context_window": 8192,
        "cost": {"prompt": 0.2, "completion": 0.2},
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
    "qwen-2-72b-instruct": {
        "endpoint": "together_ai/Qwen/Qwen2-72B-Instruct",
        "context_window": 32768,
        "cost": {"prompt": 0.9, "completion": 0.9},
    },
}
