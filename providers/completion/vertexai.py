import os

import google.auth
from providers.completion.base_completion_provider import BaseCompletionProvider


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
    def api_key_var(self) -> str:
        creds, _ = google.auth.load_credentials_from_file(
            os.environ["ORCHESTRA_VERTEXAI_SERVICE_ACC_JSON"],
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        auth_req = google.auth.transport.requests.Request()
        creds.refresh(auth_req)
        os.environ["ORCHESTRA_VERTEXAI_API_KEY"] = creds.token
        return "ORCHESTRA_VERTEXAI_API_KEY"

    @property
    def base_url(self):
        return (
            "https://us-central1-aiplatform.googleapis.com/v1beta1/projects/"
            f"{os.environ['ORCHESTRA_VERTEXAI_PROJECT']}/locations/{os.environ['ORCHESTRA_VERTEXAI_LOCATION']}/endpoints/openapi"
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
}
