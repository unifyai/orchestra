from providers.completion.base_completion_provider import BaseCompletionProvider


class XAI(BaseCompletionProvider):
    """
    A completion provider that uses the XAI service.

    Pricing: https://docs.x.ai/docs/models
    """

    def __init__(self, hub_model, custom_endpoint=None, custom_api_key=None):
        super().__init__(
            hub_model,
            "xai",
            custom_endpoint=custom_endpoint,
            custom_api_key=custom_api_key,
        )
        self.supported_models = supported_models

    @property
    def api_key_var(self) -> str:
        return "ORCHESTRA_XAI_API_KEY"

    @property
    def litellm_api_key_var(self) -> str:
        return "XAI_API_KEY"


supported_models = {
    "grok-4": {
        "endpoint": "xai/grok-4",
        "context_window": 256000,
        "cost": {"prompt": 3, "completion": 15},
    },
    "grok-3": {
        "endpoint": "xai/grok-3",
        "context_window": 131072,
        "cost": {"prompt": 3, "completion": 15},
    },
    "grok-3-mini": {
        "endpoint": "xai/grok-3-mini",
        "context_window": 131072,
        "cost": {"prompt": 0.3, "completion": 0.5},
    },
}
