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
    "gpt-oss-20b": {
        "endpoint": "together_ai/openai/gpt-oss-20b",
        "context_window": 131072,
        "cost": {"prompt": 0.05, "completion": 0.2},
    },
    "gpt-oss-120b": {
        "endpoint": "together_ai/openai/gpt-oss-120b",
        "context_window": 131072,
        "cost": {"prompt": 0.15, "completion": 0.6},
    },
    "deepseek-v3.1": {
        "endpoint": "together_ai/deepseek-ai/DeepSeek-V3.1",
        "context_window": 160000,
        "cost": {"prompt": 0.6, "completion": 1.7},
    },
    "deepseek-r1": {
        "endpoint": "together_ai/deepseek-ai/DeepSeek-R1",
        "context_window": 128000,
        "cost": {"prompt": 3, "completion": 7},
    },
    "deepseek-v3": {
        "endpoint": "together_ai/deepseek-ai/DeepSeek-V3",
        "context_window": 16384,
        "cost": {"prompt": 1.25, "completion": 1.25},
    },
    "llama-4-maverick-instruct": {
        "endpoint": "together_ai/meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8",
        "context_window": 1048576,
        "cost": {"prompt": 0.27, "completion": 0.85},
    },
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
    "mistral-small": {
        "endpoint": "mistralai/Mistral-Small-24B-Instruct-2501",
        "context_window": 32000,
        "cost": {"prompt": 0.8, "completion": 0.8},
    },
    "qwen-3-235b-a22b-instruct": {
        "endpoint": "together_ai/Qwen/Qwen3-235B-A22B-fp8-tput",
        "context_window": 128000,
        "cost": {"prompt": 0.2, "completion": 0.6},
    },
    "qwen-2.5-7b-instruct": {
        "endpoint": "together_ai/Qwen/Qwen2.5-7B-Instruct-Turbo",
        "context_window": 32768,
        "cost": {"prompt": 0.3, "completion": 0.3},
    },
    "qwen-qwq-32b": {
        "endpoint": "together_ai/Qwen/QwQ-32B",
        "context_window": 32768,
        "cost": {"prompt": 1.2, "completion": 1.2},
    },
    "qwen-2.5-72b-instruct": {
        "endpoint": "together_ai/Qwen/Qwen2.5-72B-Instruct-Turbo",
        "context_window": 32768,
        "cost": {"prompt": 1.2, "completion": 1.2},
    },
}
