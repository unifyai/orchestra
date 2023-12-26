import os
import subprocess  # noqa: S404

import litellm
import requests
from providers.completion.base_completion_provider import BaseCompletionProvider


class VertexAI(BaseCompletionProvider):
    """
    A completion provider that uses the VertexAI service.

    Supported models:
    https://cloud.google.com/vertex-ai/docs/generative-ai/learn/models
    Pricing is per thousand character: https://cloud.google.com/vertex-ai/pricing
    Models in Preview stage (100% discounted):
    https://cloud.google.com/vertex-ai/docs/generative-ai/get-token-count
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
            "cost": {"prompt": 0.00025, "completion": 0.0005, "per_character": True},
        },
        "code-bison": {  # Preview, 100% discount
            "endpoint": "code-bison",
            "context_window": 6144,
            "cost": {"prompt": 0, "completion": 0, "per_character": True},
        },
        "codechat-bison": {  # Preview, 100% discount
            "endpoint": "codechat-bison",
            "context_window": 6144,
            "cost": {"prompt": 0, "completion": 0, "per_character": True},
        },
        "code-gecko": {  # Preview, 100% discount
            "endpoint": "code-gecko",
            "context_window": 2048,
            "cost": {"prompt": 0, "completion": 0, "per_character": True},
        },
        "text-bison-32k": {
            "endpoint": "text-bison-32k",
            "context_window": 32000,
            "cost": {"prompt": 0.00025, "completion": 0.0005, "per_character": True},
        },
        "chat-bison-32k": {
            "endpoint": "chat-bison-32k",
            "context_window": 32000,
            "cost": {"prompt": 0.00025, "completion": 0.0005, "per_character": True},
        },
        "code-bison-32k": {  # Preview, 100% discount
            "endpoint": "code-bison-32k",
            "context_window": 32000,
            "cost": {"prompt": 0, "completion": 0, "per_character": True},
        },
        "codechat-bison-32k": {  # Preview, 100% discount
            "endpoint": "codechat-bison-32k",
            "context_window": 32000,
            "cost": {"prompt": 0, "completion": 0, "per_character": True},
        },
        "gemini-pro": {  # Preview, 100% discount
            "endpoint": "gemini-pro",
            "context_window": 32760,
            "cost": {"prompt": 0, "completion": 0, "per_character": True},
        },
    }

    def set_service_account_credentials(
        self,
        json_credentials_path: str,
        gcloud_install_path: str = "/workspaces/orchestra/google-cloud-sdk/bin/gcloud",
    ) -> None:
        """
        Sets the service account credentials for GCP.

        :param json_credentials_path: Credentials json file.
        :param gcloud_install_path: Path to the gcloud command.
        """
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = json_credentials_path
        self.access_token = subprocess.getoutput(
            f"{gcloud_install_path} auth application-default print-access-token",
        )

    def set_project(self, vertex_project: str) -> None:  # noqa: D102
        litellm.vertex_project = vertex_project

    def set_location(self, vertex_location: str) -> None:  # noqa: D102
        litellm.vertex_location = vertex_location

    def get_billable_characters(self, prompt, model_id) -> int:
        """
        Gets the number of billable characters for a prompt.

        :param prompt: The prompt to get the billable characters for.
        :raises Exception: If the request times out.
        :param model_id: The model ID to get the billable characters for.
        :return: The number of billable characters.
        """
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        url = (
            f"https://us-central1-aiplatform.googleapis.com/v1/projects/"
            f"{litellm.vertex_project}/locations/us-central1/publishers/"
            f"google/models/{model_id}:countTokens"
        )
        payload = {
            "instances": [
                {"prompt": prompt},
            ],
        }
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=5)
        except requests.exceptions.Timeout:
            raise Exception(  # noqa: WPS454
                (
                    "Timeout while getting billable characters, "
                    "ensure properly configured service account credentials",
                ),
            )
        return response.json().get("totalBillableCharacters", 0)
