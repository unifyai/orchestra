from providers.completion.base_completion_provider import BaseCompletionProvider


class DeepSeek(BaseCompletionProvider):
    """
    A completion provider that uses the DeepSeek service.

    Pricing: https://api-docs.deepseek.com/quick_start/pricing
    """

    def __init__(self, hub_model, custom_endpoint=None, custom_api_key=None):
        super().__init__(
            hub_model,
            "deepseek",
            custom_endpoint=custom_endpoint,
            custom_api_key=custom_api_key,
        )
        self.supported_models = supported_models

    @property
    def api_key_var(self) -> str:
        return "ORCHESTRA_DEEPSEEK_API_KEY"

    @property
    def litellm_api_key_var(self) -> str:
        return "DEEPSEEK_API_KEY"


supported_models = {
    "deepseek-v3": {
        "endpoint": "deepseek/deepseek-chat",
        "context_window": 64000,
        "cost": {"prompt": 0.27, "completion": 1.1},
    },
}
