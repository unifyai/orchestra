from providers.completion.base_completion_provider import BaseCompletionProvider


class Deepinfra(BaseCompletionProvider):
    """
    A completion provider that uses the Mistral service.

    Supported models: https://deepinfra.com/pricing
    Pricing is per million tokens: https://deepinfra.com/pricing
    """

    def __init__(self, hub_model, custom_endpoint=None, custom_api_key=None):
        super().__init__(
            hub_model,
            "deepinfra",
            custom_endpoint=custom_endpoint,
            custom_api_key=custom_api_key,
        )
        self.supported_models = supported_models

    @property
    def api_key_var(self) -> str:
        return "ORCHESTRA_DEEPINFRA_API_KEY"

    @property
    def litellm_api_key_var(self) -> str:
        return "DEEPINFRA_API_KEY"


supported_models = {
    "deepseek-v3-0324": {
        "endpoint": "deepinfra/deepseek-ai/DeepSeek-V3-0324",
        "context_window": 160000,
        "cost": {"prompt": 0.34, "completion": 0.88},
    },
    "deepseek-r1": {
        "endpoint": "deepinfra/deepseek-ai/DeepSeek-R1",
        "context_window": 160000,
        "cost": {"prompt": 0.54, "completion": 2.18},
    },
    "deepseek-v3": {
        "endpoint": "deepinfra/deepseek-ai/DeepSeek-V3",
        "context_window": 160000,
        "cost": {"prompt": 0.38, "completion": 0.89},
    },
    "llama-4-maverick-instruct": {
        "endpoint": "deepinfra/meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8",
        "context_window": 1024000,
        "cost": {"prompt": 0.18, "completion": 0.6},
    },
    "llama-4-scout-instruct": {
        "endpoint": "deepinfra/meta-llama/Llama-4-Scout-17B-16E-Instruct",
        "context_window": 320000,
        "cost": {"prompt": 0.08, "completion": 0.3},
    },
    "llama-3.3-70b-chat": {
        "endpoint": "deepinfra/meta-llama/Llama-3.3-70B-Instruct",
        "context_window": 128000,
        "cost": {"prompt": 0.23, "completion": 0.4},
    },
    "llama-3.2-1b-chat": {
        "endpoint": "deepinfra/meta-llama/Llama-3.2-1B-Instruct",
        "context_window": 128000,
        "cost": {"prompt": 0.01, "completion": 0.01},
    },
    "llama-3.2-3b-chat": {
        "endpoint": "deepinfra/meta-llama/Llama-3.2-3B-Instruct",
        "context_window": 128000,
        "cost": {"prompt": 0.015, "completion": 0.025},
    },
    "llama-3.2-11b-chat": {
        "endpoint": "deepinfra/meta-llama/Llama-3.2-11B-Vision-Instruct",
        "context_window": 128000,
        "cost": {"prompt": 0.049, "completion": 0.049},
    },
    "llama-3.2-90b-chat": {
        "endpoint": "deepinfra/meta-llama/Llama-3.2-90B-Vision-Instruct",
        "context_window": 32000,
        "cost": {"prompt": 0.35, "completion": 0.40},
    },
    "llama-3.1-8b-chat": {
        "endpoint": "deepinfra/meta-llama/Meta-Llama-3.1-8B-Instruct",
        "context_window": 128000,
        "cost": {"prompt": 0.03, "completion": 0.05},
    },
    "llama-3.1-70b-chat": {
        "endpoint": "deepinfra/meta-llama/Meta-Llama-3.1-70B-Instruct",
        "context_window": 128000,
        "cost": {"prompt": 0.23, "completion": 0.40},
    },
    "llama-3.1-nemotron-70b-chat": {
        "endpoint": "deepinfra/nvidia/Llama-3.1-Nemotron-70B-Instruct",
        "context_window": 128000,
        "cost": {"prompt": 0.12, "completion": 0.30},
    },
    "llama-3.1-405b-chat": {
        "endpoint": "deepinfra/meta-llama/Meta-Llama-3.1-405B-Instruct",
        "context_window": 32000,
        "cost": {"prompt": 0.8, "completion": 0.8},
    },
    "llama-3-8b-chat": {
        "endpoint": "deepinfra/meta-llama/Meta-Llama-3-8B-Instruct",
        "context_window": 8000,
        "cost": {"prompt": 0.03, "completion": 0.06},
    },
    "llama-3-70b-chat": {
        "endpoint": "deepinfra/meta-llama/Meta-Llama-3-70B-Instruct",
        "context_window": 8000,
        "cost": {"prompt": 0.3, "completion": 0.4},
    },
    "gemma-3-27b-it": {
        "endpoint": "deepinfra/google/gemma-3-27b-it",
        "context_window": 128000,
        "cost": {"prompt": 0.1, "completion": 0.2},
    },
    "gemma-3-12b-it": {
        "endpoint": "deepinfra/google/gemma-3-12b-it",
        "context_window": 128000,
        "cost": {"prompt": 0.05, "completion": 0.1},
    },
    "gemma-3-4b-it": {
        "endpoint": "deepinfra/google/gemma-3-4b-it",
        "context_window": 128000,
        "cost": {"prompt": 0.02, "completion": 0.04},
    },
    "mistral-small": {
        "endpoint": "mistralai/Mistral-Small-24B-Instruct-2501",
        "context_window": 32000,
        "cost": {"prompt": 0.07, "completion": 0.14},
    },
    "mixtral-8x7b-instruct-v0.1": {
        "endpoint": "deepinfra/mistralai/Mixtral-8x7B-Instruct-v0.1",
        "context_window": 32768,
        "cost": {"prompt": 0.24, "completion": 0.24},
    },
    "mistral-7b-instruct-v0.3": {
        "endpoint": "deepinfra/mistralai/Mistral-7B-Instruct-v0.3",
        "context_window": 32768,
        "cost": {"prompt": 0.04, "completion": 0.055},
    },
    "mistral-nemo": {
        "endpoint": "deepinfra/mistralai/Mistral-Nemo-Instruct-2407",
        "context_window": 128000,
        "cost": {"prompt": 0.035, "completion": 0.08},
    },
    "qwen-2.5-coder-32b-instruct": {
        "endpoint": "deepinfra/Qwen/Qwen2.5-Coder-32B-Instruct",
        "context_window": 32768,
        "cost": {"prompt": 0.07, "completion": 0.16},
    },
    "qwen-2.5-7b-instruct": {
        "endpoint": "deepinfra/Qwen/Qwen2.5-7B-Instruct",
        "context_window": 32000,
        "cost": {"prompt": 0.05, "completion": 0.1},
    },
    "qwen-2.5-72b-instruct": {
        "endpoint": "deepinfra/Qwen/Qwen2.5-72B-Instruct",
        "context_window": 32000,
        "cost": {"prompt": 0.12, "completion": 0.39},
    },
    "qwen-qwq-32b": {
        "endpoint": "deepinfra/Qwen/QwQ-32B",
        "context_window": 128000,
        "cost": {"prompt": 0.15, "completion": 0.2},
    },
}
