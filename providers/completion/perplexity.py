from typing import Dict

from providers.completion.base_completion_provider import BaseCompletionProvider


class Perplexity(BaseCompletionProvider):
    """
    A completion provider that uses the Perplexity service.

    Supported models: https://docs.perplexity.ai/docs/model-cards
    Pricing is per million tokens: https://docs.perplexity.ai/docs/pricing
    """

    def __init__(self, hub_model):
        super().__init__(hub_model)
        self.supported_models = supported_models

    @property
    def api_key_var(self) -> str:
        return "ORCHESTRA_PERPLEXITY_AI_API_KEY"

    @property
    def base_url(self):
        return "https://api.perplexity.ai/"

    def _modify_output(self, out: Dict, **kwargs) -> Dict:
        stream = kwargs.get("stream", False)
        output = super()._modify_output(out, **kwargs)
        if stream:
            output["object"] = "chat.completion.chunk"
        return output


supported_models = {
    "codellama-34b-instruct": {
        "endpoint": "codellama-34b-instruct",
        "context_window": 16384,
        "cost": {"prompt": 0.8, "completion": 0.8},
    },
    "llama-2-70b-chat": {
        "endpoint": "llama-2-70b-chat",
        "context_window": 4096,
        "cost": {"prompt": 1, "completion": 1},
    },
    "llama-3-8b-chat": {
        "endpoint": "llama-3-8b-instruct",
        "context_window": 8192,
        "cost": {"prompt": 0.2, "completion": 0.2},
    },
    "llama-3-70b-chat": {
        "endpoint": "llama-3-70b-instruct",
        "context_window": 8192,
        "cost": {"prompt": 1, "completion": 1},
    },
}
