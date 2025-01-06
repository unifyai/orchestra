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
    # "grok-2-vision-latest": {
    #     "endpoint": "xai/grok-2-vision-latest",
    #     "context_window": 8192,
    #     "cost": {"prompt": 2, "completion": 10},
    # },
    # "grok-2-latest": {
    #     "endpoint": "xai/grok-2-latest",
    #     "context_window": 131072,
    #     "cost": {"prompt": 2, "completion": 10},
    # }
}
