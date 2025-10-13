from typing import Any, List

from providers.completion.base_completion_provider import BaseCompletionProvider


class VertexAI(BaseCompletionProvider):
    """
    A completion provider that uses the VertexAI service.

    Supported models: https://cloud.google.com/vertex-ai/generative-ai/docs/model-garden/explore-models
    Pricing is per million tokens: https://ai.google.dev/pricing
    """

    def __init__(self, hub_model, custom_endpoint=None, custom_api_key=None):
        super().__init__(
            hub_model,
            "vertex_ai",
            custom_endpoint=custom_endpoint,
            custom_api_key=custom_api_key,
        )
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
        region = kwargs_region
        if self.hub_model in self.supported_models:
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
        region = kwargs_region
        if self.hub_model in self.supported_models:
            region = (
                kwargs_region
                if kwargs_region
                else self.supported_models[self.hub_model]["region"]
            )
        kwargs["vertex_location"] = region
        return super().__call__(messages, stream, **kwargs)


supported_models = {
    "gemini-2.5-flash-lite": {
        "endpoint": "vertex_ai/gemini-2.5-flash-lite",
        "region": "us-central1",
        "context_window": 200000,
        "cost": {"prompt": 0.1, "completion": 0.4},
    },
    "gemini-2.5-flash": {
        "endpoint": "vertex_ai/gemini-2.5-flash",
        "region": "us-central1",
        "context_window": 200000,
        "cost": {"prompt": 0.3, "completion": 2.5},
    },
    "gemini-2.5-pro": {
        "endpoint": "vertex_ai/gemini-2.5-pro",
        "region": "us-central1",
        "context_window": 200000,
        "cost": {"prompt": 1.25, "completion": 10},
    },
    "gemini-2.0-flash-lite": {
        "endpoint": "vertex_ai/gemini-2.0-flash-lite-001",
        "region": "us-central1",
        "context_window": 1048576,
        "cost": {"prompt": 0.075, "completion": 0.3},
    },
    "gemini-2.0-flash": {
        "endpoint": "vertex_ai/gemini-2.0-flash-001",
        "region": "us-central1",
        "context_window": 1048576,
        "cost": {"prompt": 0.15, "completion": 0.6},
    },
    "claude-3-haiku": {
        "endpoint": "vertex_ai/claude-3-haiku@20240307",
        "region": "us-east5",
        "context_window": 200000,
        "cost": {"prompt": 0.25, "completion": 1.25},
    },
    "claude-3.5-haiku": {
        "endpoint": "vertex_ai/claude-3-5-haiku@20241022",
        "region": "us-east5",
        "context_window": 200000,
        "cost": {"prompt": 0.8, "completion": 4},
    },
    "claude-3.7-sonnet": {
        "endpoint": "vertex_ai/claude-3-7-sonnet@20250219",
        "region": "us-east5",
        "context_window": 200000,
        "cost": {"prompt": 3, "completion": 15},
    },
    "claude-4-sonnet": {
        "endpoint": "vertex_ai/claude-sonnet-4@20250514",
        "region": "us-east5",
        "context_window": 200000,
        "cost": {"prompt": 3, "completion": 15},
    },
    "claude-4-opus": {
        "endpoint": "vertex_ai/claude-opus-4@20250514",
        "region": "us-east5",
        "context_window": 200000,
        "cost": {"prompt": 15, "completion": 75},
    },
    "claude-4.1-opus": {
        "endpoint": "vertex_ai/claude-opus-4-1@20250805",
        "region": "us-east5",
        "context_window": 200000,
        "cost": {"prompt": 15, "completion": 75},
    },
    "claude-4.5-sonnet": {
        "endpoint": "vertex_ai/claude-sonnet-4-5@20250929",
        "region": "us-east5",
        "context_window": 200000,
        "cost": {"prompt": 3, "completion": 15},
    },
    "llama-3.1-405b-chat": {
        "endpoint": "vertex_ai/meta/llama3-405b-instruct-maas",
        "region": "us-central1",
        "context_window": 128000,
        "cost": {"prompt": 5, "completion": 16},
    },
    "llama-3.3-70b-chat": {
        "endpoint": "vertex_ai/meta/llama-3.3-70b-instruct-maas",
        "region": "us-central1",
        "context_window": 128000,
        "cost": {"prompt": 0.72, "completion": 0.72},
    },
    "llama-4-maverick-instruct": {
        "endpoint": "vertex_ai/meta/llama-4-maverick-17b-128e-instruct-maas",
        "region": "us-east5",
        "context_window": 1048576,
        "cost": {"prompt": 0.35, "completion": 1.15},
    },
    "llama-4-scout-instruct": {
        "endpoint": "vertex_ai/meta/llama-4-scout-17b-16e-instruct-maas",
        "region": "us-east5",
        "context_window": 1048576,
        "cost": {"prompt": 0.25, "completion": 0.7},
    },
    "mistral-medium": {
        "endpoint": "vertex_ai/mistral-medium-3",
        "region": "europe-west4",
        "context_window": 128000,
        "cost": {"prompt": 0.4, "completion": 2},
    },
    "mistral-large": {
        "endpoint": "vertex_ai/mistral-large",
        "region": "europe-west4",
        "context_window": 128000,
        "cost": {"prompt": 2, "completion": 6},
    },
    "mistral-small": {
        "endpoint": "vertex_ai/mistral-small-2503",
        "region": "europe-west4",
        "context_window": 128000,
        "cost": {"prompt": 0.1, "completion": 0.3},
    },
    "qwen-3-235b-a22b-instruct": {
        "endpoint": "vertex_ai/qwen/qwen3-235b-a22b-instruct-2507-maas",
        "region": "us-south1",
        "context_window": 256000,
        "cost": {"prompt": 0.25, "completion": 1},
    },
    "deepseek-v3.1": {
        "endpoint": "vertex_ai/deepseek-ai/deepseek-v3.1-maas",
        "region": "us-west2",
        "context_window": 160000,
        "cost": {"prompt": 0.6, "completion": 1.7},
    },
    "deepseek-r1": {
        "endpoint": "vertex_ai/deepseek-ai/deepseek-r1-0528-maas",
        "region": "us-central1",
        "context_window": 128000,
        "cost": {"prompt": 1.35, "completion": 5.4},
    },
    "gpt-oss-20b": {
        "endpoint": "vertex_ai/openai/gpt-oss-20b-maas",
        "region": "us-central1",
        "context_window": 131072,
        "cost": {"prompt": 0.075, "completion": 0.3},
    },
    "gpt-oss-120b": {
        "endpoint": "vertex_ai/openai/gpt-oss-120b-maas",
        "region": "us-central1",
        "context_window": 131072,
        "cost": {"prompt": 0.15, "completion": 0.6},
    },
}
