from providers.completion.base_completion_provider import BaseCompletionProvider


class OctoAI(BaseCompletionProvider):
    """
    A completion provider that uses the OctoAI service.

    Supported models: https://docs.octoai.cloud/docs/text-generation
    Pricing: https://docs.octoai.cloud/docs/pricing (below are per million tokens)
    """

    def __init__(self, hub_model, custom_endpoint=None, custom_api_key=None):
        super().__init__(
            hub_model,
            "",
            custom_endpoint=custom_endpoint,
            custom_api_key=custom_api_key,
        )
        self.supported_models = supported_models

    @property
    def api_key_var(self) -> str:
        return "ORCHESTRA_OCTOAI_API_KEY"

    @property
    def litellm_api_key_var(self) -> str:
        return ""

    @property
    def base_url(self):
        return "https://text.octoai.run/v1/"


supported_models = {
    "mistral-7b-instruct-v0.3": {
        "endpoint": "mistral-7b-instruct",
        "context_window": 32768,
        "cost": {"prompt": 0.15, "completion": 0.15},
    },
    "mixtral-8x7b-instruct-v0.1": {
        "endpoint": "mixtral-8x7b-instruct",
        "context_window": 32768,
        "cost": {"prompt": 0.45, "completion": 0.45},
    },
    "mistral-nemo": {
        "endpoint": "mistral-nemo-instruct",
        "context_window": 128000,
        "cost": {"prompt": 0.2, "completion": 0.2},
    },
    "llama-3-70b-chat": {
        "endpoint": "meta-llama-3-70b-instruct",
        "context_window": 8192,
        "cost": {"prompt": 0.9, "completion": 0.9},
    },
    "llama-3.1-8b-chat": {
        "endpoint": "meta-llama-3.1-8b-instruct",
        "context_window": 131072,
        "cost": {"prompt": 0.15, "completion": 0.15},
    },
    "llama-3.1-70b-chat": {
        "endpoint": "meta-llama-3.1-70b-instruct",
        "context_window": 131072,
        "cost": {"prompt": 0.9, "completion": 0.9},
    },
    "llama-3.1-405b-chat": {
        "endpoint": "meta-llama-3.1-405b-instruct",
        "context_window": 131072,
        "cost": {"prompt": 3, "completion": 9},
    },
}
