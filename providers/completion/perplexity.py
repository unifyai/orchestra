from providers.completion.base_completion_provider import BaseCompletionProvider


class Perplexity(BaseCompletionProvider):
    """
    A completion provider that uses the Perplexity service.

    Supported models: https://docs.perplexity.ai/docs/model-cards
    Pricing is per million tokens: https://docs.perplexity.ai/docs/pricing
    """

    def __init__(self, hub_model, custom_api_key=None):
        super().__init__(hub_model, custom_api_key=custom_api_key)
        self.supported_models = supported_models

    @property
    def api_key_var(self) -> str:
        return "ORCHESTRA_PERPLEXITY_AI_API_KEY"

    @property
    def litellm_api_key_var(self) -> str:
        return "PERPLEXITY_API_KEY"


supported_models = {
    "llama-3.1-8b-chat": {
        "endpoint": "perplexity/llama-3.1-8b-instruct",
        "context_window": 131072,
        "cost": {"prompt": 0.2, "completion": 0.2},
    },
    "llama-3.1-70b-chat": {
        "endpoint": "perplexity/llama-3.1-70b-instruct",
        "context_window": 131072,
        "cost": {"prompt": 1, "completion": 1},
    },
}
