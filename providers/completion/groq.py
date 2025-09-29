from providers.completion.base_completion_provider import BaseCompletionProvider


class Groq(BaseCompletionProvider):
    """
    A completion provider that uses the Groq service.

    Supported models: https://console.groq.com/docs/models
    Pricing is per million tokens: https://console.groq.com/settings/billing
    """

    def __init__(self, hub_model, custom_endpoint=None, custom_api_key=None):
        super().__init__(
            hub_model,
            "groq",
            custom_endpoint=custom_endpoint,
            custom_api_key=custom_api_key,
        )
        self.supported_models = supported_models

    @property
    def api_key_var(self) -> str:
        return "ORCHESTRA_GROQ_API_KEY"

    @property
    def litellm_api_key_var(self) -> str:
        return "GROQ_API_KEY"


supported_models = {
    "llama-3.1-8b-chat": {
        "endpoint": "groq/llama-3.1-8b-instant",
        "context_window": 131072,
        "cost": {"prompt": 0.05, "completion": 0.08},
    },
    "llama-3.3-70b-chat": {
        "endpoint": "groq/llama-3.3-70b-versatile",
        "context_window": 128000,
        "cost": {"prompt": 0.59, "completion": 0.79},
    },
    "llama-4-maverick-instruct": {
        "endpoint": "groq/meta-llama/llama-4-maverick-17b-128e-instruct",
        "context_window": 131072,
        "cost": {"prompt": 0.2, "completion": 0.6},
    },
    "llama-4-scout-instruct": {
        "endpoint": "groq/meta-llama/llama-4-scout-17b-16e-instruct",
        "context_window": 131072,
        "cost": {"prompt": 0.11, "completion": 0.34},
    },
    "gpt-oss-20b": {
        "endpoint": "groq/openai/gpt-oss-20b",
        "context_window": 128000,
        "cost": {"prompt": 0.1, "completion": 0.5},
    },
    "gpt-oss-120b": {
        "endpoint": "groq/openai/gpt-oss-120b",
        "context_window": 128000,
        "cost": {"prompt": 0.15, "completion": 0.75},
    },
}
