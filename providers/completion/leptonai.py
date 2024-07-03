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

    def _modify_output(self, out: Dict, **kwargs) -> Dict:
        stream = kwargs.get("stream", False)
        output = super()._modify_output(out, **kwargs)
        output["created"] = int(time.time())
        output["object"] = "chat.completion"
        if stream:
            output["object"] = "chat.completion.chunk"
        return output


supported_models = {
    "gemma-7b-it": {
        "endpoint": "gemma-7b",
        "context_window": 8192,
        "cost": {"prompt": 0.07, "completion": 0.07},
    },
    "mixtral-8x7b-instruct-v0.1": {
        "endpoint": "mixtral-8x7b",
        "context_window": 32768,
        "cost": {"prompt": 0.5, "completion": 0.5},
    },
    "llama-3-8b-chat": {
        "endpoint": "llama3-8b",
        "context_window": 8192,
        "cost": {"prompt": 0.07, "completion": 0.07},
    },
    "llama-3-70b-chat": {
        "endpoint": "llama3-70b",
        "context_window": 8192,
        "cost": {"prompt": 0.8, "completion": 0.8},
    },
    "mistral-7b-instruct-v0.3": {
        "endpoint": "mistral-7b",
        "context_window": 32768,
        "cost": {"prompt": 0.07, "completion": 0.07},
    },
}
