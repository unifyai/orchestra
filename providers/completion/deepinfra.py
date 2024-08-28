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
        "cost": {"prompt": 0.06, "completion": 0.06},
    },
    "llama-3.1-70b-chat": {
        "endpoint": "deepinfra/meta-llama/Meta-Llama-3.1-70B-Instruct",
        "context_window": 128000,
        "cost": {"prompt": 0.52, "completion": 0.75},
    },
    "llama-3.1-405b-chat": {
        "endpoint": "deepinfra/meta-llama/Meta-Llama-3.1-405B-Instruct",
        "context_window": 32000,
        "cost": {"prompt": 2.7, "completion": 2.7},
    },
    "llama-3-8b-chat": {
        "endpoint": "deepinfra/meta-llama/Meta-Llama-3-8B-Instruct",
        "context_window": 8000,
        "cost": {"prompt": 0.06, "completion": 0.06},
    },
    "llama-3-70b-chat": {
        "endpoint": "deepinfra/meta-llama/Meta-Llama-3-70B-Instruct",
        "context_window": 8000,
        "cost": {"prompt": 0.52, "completion": 0.75},
    },
    "gemma-2-9b-it": {
        "endpoint": "deepinfra/google/gemma-2-9b-it",
        "context_window": 4000,
        "cost": {"prompt": 0.09, "completion": 0.09},
    },
    "gemma-2-27b-it": {
        "endpoint": "deepinfra/google/gemma-2-27b-it",
        "context_window": 4000,
        "cost": {"prompt": 0.27, "completion": 0.27},
    },
    "gemma-7b-it": {
        "endpoint": "deepinfra/google/gemma-7b-it",
        "context_window": 8192,
        "cost": {"prompt": 0.07, "completion": 0.07},
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
        "cost": {"prompt": 0.06, "completion": 0.06},
    },
    "phind-codellama-34b-v2": {
        "endpoint": "deepinfra/Phind/Phind-CodeLlama-34B-v2",
        "context_window": 16384,
        "cost": {"prompt": 0.6, "completion": 0.6},
    },
    "qwen-2-7b-instruct": {
        "endpoint": "deepinfra/Qwen/Qwen2-72B-Instruct",
        "context_window": 32000,
        "cost": {"prompt": 0.07, "completion": 0.07},
    },
    "qwen-2-72b-instruct": {
        "endpoint": "deepinfra/Qwen/Qwen2-7B-Instruct",
        "context_window": 32000,
        "cost": {"prompt": 0.56, "completion": 0.77},
    },
    "phi-3-medium-4k-instruct": {
        "endpoint": "deepinfra/microsoft/Phi-3-medium-4k-instruct",
        "context_window": 4000,
        "cost": {"prompt": 0.14, "completion": 0.14},
    },
    "nemotron-4-340b-instruct": {
        "endpoint": "deepinfra/nvidia/Nemotron-4-340B-Instruct",
        "context_window": 4000,
        "cost": {"prompt": 4.2, "completion": 4.2},
    },
}
