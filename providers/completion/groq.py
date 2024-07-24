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
    def base_url(self):
        return "https://api.groq.com/openai/v1"


supported_models = {
    "gemma-2-9b-it": {
        "endpoint": "gemma2-9b-it",
        "context_window": 8192,
        "cost": {"prompt": 0.2, "completion": 0.2},
    },
    "gemma-7b-it": {
        "endpoint": "gemma-7b-it",
        "context_window": 8192,
        "cost": {"prompt": 0.1, "completion": 0.1},
    },
    "mixtral-8x7b-instruct-v0.1": {
        "endpoint": "mixtral-8x7b-32768",
        "context_window": 32768,
        "cost": {"prompt": 0.27, "completion": 0.27},
    },
    "llama-3-8b-chat": {
        "endpoint": "llama3-8b-8192",
        "context_window": 8192,
        "cost": {"prompt": 0.05, "completion": 0.1},
    },
    "llama-3-70b-chat": {
        "endpoint": "llama3-70b-8192",
        "context_window": 8192,
        "cost": {"prompt": 0.59, "completion": 0.79},
    },
}
