from typing import Any, List

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
        return "ORCHESTRA_VERTEXAI_SERVICE_ACC_JSON"

    @property
    def litellm_api_key_var(self) -> str:
        return "GOOGLE_APPLICATION_CREDENTIALS"

    def __call__(
        self,
        messages: List,
        stream: bool = False,
        **kwargs: Any,
    ) -> Any:  # noqa: WPS210
        kwargs_region = kwargs.pop("region", None)
        region = (
            kwargs_region
            if kwargs_region
            else self.supported_models[self.hub_model]["region"]
        )
        kwargs["vertex_location"] = region
        return super().__call__(messages, stream, **kwargs)

    def __call_async__(
        self,
        messages: List,
        stream: bool = False,
        **kwargs: Any,
    ) -> Any:
        kwargs_region = kwargs.pop("region", None)
        region = (
            kwargs_region
            if kwargs_region
            else self.supported_models[self.hub_model]["region"]
        )
        kwargs["vertex_location"] = region
        return super().__call__(messages, stream, **kwargs)


supported_models = {
    "gemini-1.5-pro": {
        "endpoint": "vertex_ai/gemini-1.5-pro",
        "region": "us-west1",
        "context_window": 128000,
        "cost": {"prompt": 3.5, "completion": 10.5},
    },
    "gemini-1.5-flash": {
        "endpoint": "vertex_ai/gemini-1.5-flash",
        "region": "us-west1",
        "context_window": 128000,
        "cost": {"prompt": 0.075, "completion": 0.3},
    },
    "claude-3-haiku": {
        "endpoint": "vertex_ai/claude-3-haiku@20240307",
        "region": "us-east5",
        "context_window": 200000,
        "cost": {"prompt": 0.25, "completion": 1.25},
    },
    "claude-3-sonnet": {
        "endpoint": "vertex_ai/claude-3-sonnet@20240229",
        "region": "us-east5",
        "context_window": 200000,
        "cost": {"prompt": 3, "completion": 15},
    },
    "claude-3-opus": {
        "endpoint": "vertex_ai/claude-3-opus@20240229",
        "region": "us-east5",
        "context_window": 200000,
        "cost": {"prompt": 15, "completion": 75},
    },
    "claude-3.5-sonnet": {
        "endpoint": "vertex_ai/claude-3-5-sonnet@20240620",
        "region": "us-east5",
        "context_window": 200000,
        "cost": {"prompt": 3, "completion": 15},
    },
    "llama-3.1-405b-chat": {
        "endpoint": "vertex_ai/meta/llama3-405b-instruct-maas",
        "region": "us-central1",
        "context_window": 128000,
        "cost": {"prompt": 5.32, "completion": 16},
    },
    "mistral-large": {
        "endpoint": "vertex_ai/mistral-large",
        "region": "europe-west4",
        "context_window": 32768,
        "cost": {"prompt": 3, "completion": 9},
    },
    "mistral-nemo": {
        "endpoint": "vertex_ai/mistral-nemo",
        "region": "europe-west4",
        "context_window": 128000,
        "cost": {"prompt": 0.3, "completion": 0.3},
    },
}
