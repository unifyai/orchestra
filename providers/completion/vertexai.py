from providers.completion.base_completion_provider import BaseCompletionProvider


class VertexAI(BaseCompletionProvider):
    """
    A completion provider that uses the VertexAI service.

    Supported models: https://cloud.google.com/vertex-ai/generative-ai/docs/model-garden/explore-models
    Pricing is per million tokens: https://ai.google.dev/pricing
    """

    def __init__(self, hub_model, custom_api_key=None):
        super().__init__(hub_model, custom_api_key=custom_api_key)
        self.supported_models = supported_models

    @property
    def api_key_var(self) -> str:
        return "GOOGLE_APPLICATION_CREDENTIALS"

    @property
    def litellm_api_key_var(self) -> str:
        return "GOOGLE_APPLICATION_CREDENTIALS"


supported_models = {
    "gemini-1.5-pro": {
        "endpoint": "vertex_ai/gemini-1.5-pro",
        "context_window": 128000,
        "cost": {"prompt": 3.5, "completion": 10.5},
    },
    "gemini-1.5-flash": {
        "endpoint": "vertex_ai/gemini-1.5-flash",
        "context_window": 128000,
        "cost": {"prompt": 0.075, "completion": 0.3},
    },
}
