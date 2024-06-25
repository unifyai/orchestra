from providers.completion.base_completion_provider import BaseCompletionProvider


class OctoAI(BaseCompletionProvider):
    """
    A completion provider that uses the OctoAI service.

    Supported models: https://docs.octoai.cloud/docs/text-generation
    Pricing: https://docs.octoai.cloud/docs/pricing (below are per million tokens)
    """

    def __init__(self, hub_model):
        super().__init__(hub_model)
        self.supported_models = supported_models

    @property
    def api_key_var(self) -> str:
        return "ORCHESTRA_OCTOAI_API_KEY"

    @property
    def base_url(self):
        return "https://text.octoai.run/v1/"


supported_models = {
    "mistral-7b-instruct-v0.2": {
        "endpoint": "mistral-7b-instruct",
        "context_window": 32768,
        "cost": {"prompt": 0.15, "completion": 0.15},
    },
    "mixtral-8x7b-instruct-v0.1": {
        "endpoint": "mixtral-8x7b-instruct",
        "context_window": 32768,
        "cost": {"prompt": 0.45, "completion": 0.45},
    },
    "mixtral-8x22b-instruct-v0.1": {
        "endpoint": "mixtral-8x22b-instruct",
        "context_window": 65536,
        "cost": {"prompt": 1.2, "completion": 1.2},
    },
    "llama-3-8b-chat": {
        "endpoint": "meta-llama-3-8b-instruct",
        "context_window": 8192,
        "cost": {"prompt": 0.15, "completion": 0.15},
    },
    "llama-3-70b-chat": {
        "endpoint": "meta-llama-3-70b-instruct",
        "context_window": 8192,
        "cost": {"prompt": 0.9, "completion": 0.9},
    },
}
