import google.auth
from providers.completion.base_completion_provider import BaseCompletionProvider

from orchestra.settings import settings


class VertexAI(BaseCompletionProvider):
    """
    A completion provider that uses the VertexAI service.

    Supported models: https://cloud.google.com/vertex-ai/generative-ai/docs/model-garden/explore-models
    Pricing is per million tokens: https://ai.google.dev/pricing
    """

    def __init__(self, hub_model):
        super().__init__(hub_model)
        self.supported_models = supported_models

    @property
    def api_key(self) -> str:
        # TODO: check if this doesn't add TTFT, prob does
        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        auth_req = google.auth.transport.requests.Request()
        creds.refresh(auth_req)
        return creds.token

    @property
    def base_url(self):
        return (
            "https://us-central1-aiplatform.googleapis.com/v1beta1/projects/"
            f"{settings.vertexai_project}/locations/{settings.vertexai_location}/endpoints/openapi"
        )


supported_models = {
    "gemini-1.5-pro": {
        "endpoint": "google/gemini-1.5-pro",
        "context_window": 128000,
        "cost": {"prompt": 3.5, "completion": 10.5},
    },
    "gemini-1.5-flash": {
        "endpoint": "google/gemini-1.5-flash",
        "context_window": 128000,
        "cost": {"prompt": 0.35, "completion": 1.05},
    },
    # "gemma-2-9b-it": {
    #     "endpoint": "google/gemma2-9b-it",
    #     "context_window": 8192,
    #     "cost": {"prompt": 0.2, "completion": 0.2},
    # },
    # "gemma-2-27b-it": {
    #     "endpoint": "google/gemma2-27b-it",
    #     "context_window": 8192,
    #     "cost": {}
    # }
}
