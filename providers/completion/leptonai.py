import time
from typing import Dict

from providers.completion.base_completion_provider import BaseCompletionProvider


class LeptonAI(BaseCompletionProvider):
    """
    A completion provider that uses the Mistral service.

    Supported models: https://www.lepton.ai/playground
    Pricing is per million tokens: https://www.lepton.ai/pricing
    """

    def __init__(self, hub_model):
        super().__init__(hub_model)
        self.supported_models = supported_models

    @property
    def api_key_var(self) -> str:
        return "ORCHESTRA_LEPTON_AI_API_KEY"

    @property
    def base_url(self):
        return "https://{0}.lepton.run/api/v1/".format(self.provider_endpoint)

    def _modify_output(self, out: Dict, stream: bool) -> Dict:
        out["created"] = int(time.time())
        out["object"] = "chat.completion"
        if stream:
            out["object"] = "chat.completion.chunk"
        return out


supported_models = {
    "gemma-7b-it": {
        "endpoint": "gemma-7b",
        "context_window": 8192,
        "cost": {"prompt": 0.1, "completion": 0.1},
    },
    "mixtral-8x7b-instruct-v0.1": {
        "endpoint": "mixtral-8x7b",
        "context_window": 32768,
        "cost": {"prompt": 0.5, "completion": 0.5},
    },
    "llama-2-7b-chat": {
        "endpoint": "llama2-7b",
        "context_window": 4096,
        "cost": {"prompt": 0.1, "completion": 0.1},
    },
    "llama-2-13b-chat": {
        "endpoint": "llama2-13b",
        "context_window": 4096,
        "cost": {"prompt": 0.3, "completion": 0.3},
    },
    "llama-2-70b-chat": {
        "endpoint": "llama2-70b",
        "context_window": 4096,
        "cost": {"prompt": 0.8, "completion": 0.8},
    },
}
