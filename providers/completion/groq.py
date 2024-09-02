from providers.completion.base_completion_provider import BaseCompletionProvider


class Groq(BaseCompletionProvider):
    """
    A completion provider that uses the Groq service.

    Supported models: https://console.groq.com/docs/models
    Pricing is per million tokens: https://console.groq.com/settings/billing
    """

    def __init__(self, hub_model, custom_api_key=None):
        super().__init__(hub_model, custom_api_key=custom_api_key)
        self.supported_models = supported_models

    @property
    def api_key_var(self) -> str:
        return "ORCHESTRA_GROQ_API_KEY"

    @property
    def litellm_api_key_var(self) -> str:
        return "GROQ_API_KEY"


supported_models = {
    "gemma-2-9b-it": {
        "endpoint": "groq/gemma2-9b-it",
        "context_window": 8192,
        "cost": {"prompt": 0.2, "completion": 0.2},
    },
    "gemma-7b-it": {
        "endpoint": "groq/gemma-7b-it",
        "context_window": 8192,
        "cost": {"prompt": 0.07, "completion": 0.07},
    },
    "mixtral-8x7b-instruct-v0.1": {
        "endpoint": "groq/mixtral-8x7b-32768",
        "context_window": 32768,
        "cost": {"prompt": 0.24, "completion": 0.24},
    },
    "llama-3-8b-chat": {
        "endpoint": "groq/llama3-8b-8192",
        "context_window": 8192,
        "cost": {"prompt": 0.05, "completion": 0.08},
    },
    "llama-3-70b-chat": {
        "endpoint": "groq/llama3-70b-8192",
        "context_window": 8192,
        "cost": {"prompt": 0.59, "completion": 0.79},
    },
    "llama-3.1-8b-chat": {
        "endpoint": "groq/llama-3.1-8b-instant",
        "context_window": 131072,
        "cost": {"prompt": 0.05, "completion": 0.08},
    },
    "llama-3.1-70b-chat": {
        "endpoint": "groq/llama-3.1-70b-versatile",
        "context_window": 131072,
        "cost": {"prompt": 0.59, "completion": 0.79},
    },
}
