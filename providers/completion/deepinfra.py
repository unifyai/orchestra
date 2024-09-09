from providers.completion.base_completion_provider import BaseCompletionProvider


class Deepinfra(BaseCompletionProvider):
    """
    A completion provider that uses the Mistral service.

    Supported models: https://deepinfra.com/pricing
    Pricing is per million tokens: https://deepinfra.com/pricing
    """

    def __init__(self, hub_model, custom_api_key=None):
        super().__init__(hub_model, custom_api_key=custom_api_key)
        self.supported_models = supported_models

    @property
    def api_key_var(self) -> str:
        return "ORCHESTRA_DEEPINFRA_API_KEY"

    @property
    def litellm_api_key_var(self) -> str:
        return "DEEPINFRA_API_KEY"


supported_models = {
    "llama-3.1-8b-chat": {
        "endpoint": "deepinfra/meta-llama/Meta-Llama-3.1-8B-Instruct",
        "context_window": 128000,
        "cost": {"prompt": 0.055, "completion": 0.055},
    },
    "llama-3.1-70b-chat": {
        "endpoint": "deepinfra/meta-llama/Meta-Llama-3.1-70B-Instruct",
        "context_window": 128000,
        "cost": {"prompt": 0.35, "completion": 0.4},
    },
    "llama-3.1-405b-chat": {
        "endpoint": "deepinfra/meta-llama/Meta-Llama-3.1-405B-Instruct",
        "context_window": 32000,
        "cost": {"prompt": 1.79, "completion": 1.79},
    },
    "llama-3-8b-chat": {
        "endpoint": "deepinfra/meta-llama/Meta-Llama-3-8B-Instruct",
        "context_window": 8000,
        "cost": {"prompt": 0.055, "completion": 0.055},
    },
    "llama-3-70b-chat": {
        "endpoint": "deepinfra/meta-llama/Meta-Llama-3-70B-Instruct",
        "context_window": 8000,
        "cost": {"prompt": 0.35, "completion": 0.4},
    },
    "gemma-2-9b-it": {
        "endpoint": "deepinfra/google/gemma-2-9b-it",
        "context_window": 8000,
        "cost": {"prompt": 0.06, "completion": 0.06},
    },
    "gemma-2-27b-it": {
        "endpoint": "deepinfra/google/gemma-2-27b-it",
        "context_window": 8000,
        "cost": {"prompt": 0.27, "completion": 0.27},
    },
    "mixtral-8x22b-instruct-v0.1": {
        "endpoint": "deepinfra/mistralai/Mixtral-8x22B-Instruct-v0.1",
        "context_window": 65536,
        "cost": {"prompt": 0.65, "completion": 0.65},
    },
    "mixtral-8x7b-instruct-v0.1": {
        "endpoint": "deepinfra/mistralai/Mixtral-8x7B-Instruct-v0.1",
        "context_window": 32768,
        "cost": {"prompt": 0.24, "completion": 0.24},
    },
    "mistral-7b-instruct-v0.3": {
        "endpoint": "deepinfra/mistralai/Mistral-7B-Instruct-v0.3",
        "context_window": 32768,
        "cost": {"prompt": 0.055, "completion": 0.055},
    },
    "mistral-nemo": {
        "endpoint": "mistralai/Mistral-Nemo-Instruct-2407",
        "context_window": 128000,
        "cost": {"prompt": 0.13, "completion": 0.13},
    },
    "qwen-2-7b-instruct": {
        "endpoint": "deepinfra/Qwen/Qwen2-72B-Instruct",
        "context_window": 32000,
        "cost": {"prompt": 0.055, "completion": 0.055},
    },
    "qwen-2-72b-instruct": {
        "endpoint": "deepinfra/Qwen/Qwen2-7B-Instruct",
        "context_window": 32000,
        "cost": {"prompt": 0.35, "completion": 0.40},
    },
}
