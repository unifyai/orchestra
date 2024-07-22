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
    def base_url(self):
        return "https://api.together.xyz/v1/"


supported_models = {
    "llama-3-70b-chat": {
        "endpoint": "meta-llama/Llama-3-70b-chat-hf",
        "context_window": 8192,
        "cost": {"prompt": 0.9, "completion": 0.9},
    },
    "llama-3-8b-chat": {
        "endpoint": "meta-llama/Llama-3-8b-chat-hf",
        "context_window": 8192,
        "cost": {"prompt": 0.2, "completion": 0.2},
    },
    "mixtral-8x22b-instruct-v0.1": {
        "endpoint": "mistralai/Mixtral-8x22B-Instruct-v0.1",
        "context_window": 65536,
        "cost": {"prompt": 1.2, "completion": 1.2},
    },
    "gemma-2b-it": {
        "endpoint": "google/gemma-2b-it",
        "context_window": 8192,
        "cost": {"prompt": 0.1, "completion": 0.1},
    },
    "gemma-7b-it": {
        "endpoint": "google/gemma-7b-it",
        "context_window": 8192,
        "cost": {"prompt": 0.2, "completion": 0.2},
    },
    "codellama-13b-instruct": {
        "endpoint": "togethercomputer/CodeLlama-13b-Instruct",
        "context_window": 16384,
        "cost": {"prompt": 0.3, "completion": 0.3},
    },
    "codellama-7b-instruct": {
        "endpoint": "togethercomputer/CodeLlama-7b-Instruct",
        "context_window": 16384,
        "cost": {"prompt": 0.2, "completion": 0.2},
    },
    "codellama-70b-instruct": {
        "endpoint": "codellama/CodeLlama-70b-Instruct-hf",
        "content": 4096,
        "cost": {"prompt": 0.9, "completion": 0.9},
    },
    "deepseek-coder-33b-instruct": {
        "endpoint": "deepseek-ai/deepseek-coder-33b-instruct",
        "context_window": 16384,
        "cost": {"prompt": 0.8, "completion": 0.8},
    },
    "mistral-7b-instruct-v0.3": {
        "endpoint": "mistralai/Mistral-7B-Instruct-v0.3",
        "context_window": 32768,
        "cost": {"prompt": 0.2, "completion": 0.2},
    },
    "mixtral-8x7b-instruct-v0.1": {
        "endpoint": "mistralai/Mixtral-8x7B-Instruct-v0.1",
        "context_window": 32768,
        "cost": {"prompt": 0.6, "completion": 0.6},
    },
    "phind-codellama-34b-v2": {
        "endpoint": "Phind/Phind-CodeLlama-34B-v2",
        "context_window": 16384,
        "cost": {"prompt": 0.8, "completion": 0.8},
    },
    "qwen-2-72b-instruct": {
        "endpoint": "Qwen/Qwen2-72B-Instruct",
        "context_window": 32768,
        "cost": {"prompt": 0.9, "completion": 0.9},
    },
}
