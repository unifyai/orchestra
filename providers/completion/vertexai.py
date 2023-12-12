import os
import subprocess

import litellm
import requests
from providers.completion.base_completion_provider import BaseCompletionProvider


class VertexAI(BaseCompletionProvider):
    """
    A completion provider that uses the VertexAI service.

    Supported models: https://cloud.google.com/vertex-ai/docs/generative-ai/learn/models
    Pricing is per character: https://docs.perplexity.ai/docs/pricing
    Models in Preview stage (100% discounted): https://cloud.google.com/vertex-ai/docs/generative-ai/get-token-count#get_the_token_count_for_a_prompt
    """

    supported_models = {
        "text-bison": {
            "endpoint": "text-bison",
            "context_window": 8192,
            "cost": {"prompt": 0.00025, "completion": 0.0005, "per_character": True},
        },
        "chat-bison": {
            "endpoint": "chat-bison",
            "context_window": 8192,
            "cost": {"prompt": 0.00025, "completion": 0.0005},
        },
        "code-bison": {  # Preview, 100% discount
            "endpoint": "code-bison",
            "context_window": 6144,
            "cost": {"prompt": 0, "completion": 0},
        },
        "codechat-bison": {  # Preview, 100% discount
            "endpoint": "codechat-bison",
            "context_window": 6144,
            "cost": {"prompt": 0, "completion": 0},
        },
        "code-gecko": {  # Preview, 100% discount
            "endpoint": "code-gecko",
            "context_window": 2048,
            "cost": {"prompt": 0, "completion": 0},
        },
        "text-bison-32k": {
            "endpoint": "text-bison-32k",
            "context_window": 32000,
            "cost": {"prompt": 0.00025, "completion": 0.0005},
        },
        "chat-bison-32k": {
            "endpoint": "chat-bison-32k",
            "context_window": 32000,
            "cost": {"prompt": 0.00025, "completion": 0.0005},
        },
        "code-bison-32k": {  # Preview, 100% discount
            "endpoint": "code-bison-32k",
            "context_window": 32000,
            "cost": {"prompt": 0, "completion": 0},
        },
        "codechat-bison-32k": {  # Preview, 100% discount
            "endpoint": "codechat-bison-32k",
            "context_window": 32000,
            "cost": {"prompt": 0, "completion": 0},
        },
    }

    def set_service_account_credentials(self, json_credentials_path: str) -> None:
        """
        Sets the service account credentials for GCP.

        :param json_credentials_path: Credentials json file.
        """
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = json_credentials_path
        self.access_token = subprocess.getoutput(
            "/workspaces/orchestra/google-cloud-sdk/bin/gcloud auth application-default print-access-token"
        )

    def set_project(self, vertex_project: str) -> None:  # noqa: D102
        litellm.vertex_project = vertex_project

    def set_location(self, vertex_location: str) -> None:  # noqa: D102
        litellm.vertex_location = vertex_location

    def get_billable_characters(self, prompt, model_id):
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        url = f"https://us-central1-aiplatform.googleapis.com/v1/projects/{litellm.vertex_project}/locations/us-central1/publishers/google/models/{model_id}:countTokens"
        payload = {
            "instances": [
                {"prompt": prompt},
            ],
        }
        response = requests.post(url, headers=headers, json=payload)
        return response.json()["totalBillableCharacters"]
