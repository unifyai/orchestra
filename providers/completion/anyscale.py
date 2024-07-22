from typing import Dict

from providers.completion.base_completion_provider import BaseCompletionProvider


class Anyscale(BaseCompletionProvider):
    """
    A completion provider that uses the Anyscale service.

    Source: https://docs.anyscale.com/endpoints/overview#supported-models
    Pricing is per million tokens: https://docs.endpoints.anyscale.com/pricing
    """

    def __init__(self, hub_model, custom_api_key=None):
        super().__init__(hub_model, custom_api_key=custom_api_key)
        self.supported_models = supported_models

    @property
    def api_key_var(self) -> str:
        return "ORCHESTRA_ANYSCALE_API_KEY"

    @property
    def base_url(self):
        return "https://api.endpoints.anyscale.com/v1"

    def _modify_output(self, out: Dict, **kwargs) -> Dict:
        stream = kwargs.get("stream", False)
        output = super()._modify_output(out, **kwargs)
        output["object"] = "chat.completion"
        if stream:
            output["object"] = "chat.completion.chunk"
        return output


supported_models = {
    "gemma-7b-it": {
        "endpoint": "google/gemma-7b-it",
        "context_window": 8192,
        "cost": {"prompt": 0.15, "completion": 0.15},
    },
    "mistral-7b-instruct-v0.1": {
        "endpoint": "mistralai/Mistral-7B-Instruct-v0.1",
        "context_window": 16384,
        "cost": {"prompt": 0.15, "completion": 0.15},
    },
    "mixtral-8x7b-instruct-v0.1": {
        "endpoint": "mistralai/Mixtral-8x7B-Instruct-v0.1",
        "context_window": 32768,
        "cost": {"prompt": 0.5, "completion": 0.5},
    },
    "mixtral-8x22b-instruct-v0.1": {
        "endpoint": "mistralai/Mixtral-8x22B-Instruct-v0.1",
        "context_window": 65536,
        "cost": {"prompt": 0.9, "completion": 0.9},
    },
    "llama-3-8b-chat": {
        "endpoint": "meta-llama/Meta-Llama-3-8B-Instruct",
        "context_window": 8192,
        "cost": {"prompt": 0.15, "completion": 0.15},
    },
    "llama-3-70b-chat": {
        "endpoint": "meta-llama/Meta-Llama-3-70B-Instruct",
        "context_window": 8192,
        "cost": {"prompt": 1, "completion": 1},
    },
    "codellama-70b-instruct": {
        "endpoint": "codellama/CodeLlama-70b-Instruct-hf",
        "context_window": 4096,
        "cost": {"prompt": 1, "completion": 1},
    },
}
