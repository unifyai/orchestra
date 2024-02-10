import os
import subprocess  # noqa: S404
from typing import List

import litellm
import requests
from litellm.utils import ModelResponse
from providers.completion.base_completion_provider import BaseCompletionProvider

# Pricing info of providers with pay-per-character model (only Vertex AI currently)
# is standardized to per thousand tokens.
PRICING_PER_CHARACTERS = 1000


class VertexAI(BaseCompletionProvider):
    """
    A completion provider that uses the VertexAI service.

    Supported models:
    https://cloud.google.com/vertex-ai/docs/generative-ai/learn/models
    Pricing is per thousand character: https://cloud.google.com/vertex-ai/pricing
    Models in Preview stage (100% discounted):
    https://cloud.google.com/vertex-ai/docs/generative-ai/get-token-count
    Deprecation:
    https://cloud.google.com/vertex-ai/docs/generative-ai/learn/model-versioning
    """

    supported_models = {
        "text-bison": {
            "endpoint": "text-bison",  # redirects to latest
            "context_window": 8192,
            "cost": {"prompt": 0.00025, "completion": 0.0005, "per_character": True},
        },
        "text-bison-002": {
            "endpoint": "text-bison@002",
            "context_window": 8192,
            "cost": {"prompt": 0.00025, "completion": 0.0005, "per_character": True},
        },
        "text-bison-001": {
            "endpoint": "text-bison@001",
            "context_window": 8192,
            "cost": {"prompt": 0.00025, "completion": 0.0005, "per_character": True},
        },  # shutdown on 2024-07-06
        "chat-bison": {
            "endpoint": "chat-bison",  # redirects to latest
            "context_window": 8192,
            "cost": {"prompt": 0.00025, "completion": 0.0005, "per_character": True},
        },
        "chat-bison-002": {
            "endpoint": "chat-bison@002",
            "context_window": 8192,
            "cost": {"prompt": 0.00025, "completion": 0.0005, "per_character": True},
        },
        "chat-bison-001": {
            "endpoint": "chat-bison@001",
            "context_window": 8192,
            "cost": {"prompt": 0.00025, "completion": 0.0005, "per_character": True},
        },  # shutdown on 2024-07-06
        "code-bison": {  # Preview, 100% discount
            "endpoint": "code-bison",  # redirects to latest
            "context_window": 6144,
            "cost": {"prompt": 0, "completion": 0, "per_character": True},
        },
        "code-bison-002": {  # Preview, 100% discount
            "endpoint": "code-bison@002",
            "context_window": 6144,
            "cost": {"prompt": 0, "completion": 0, "per_character": True},
        },
        "code-bison-001": {  # Preview, 100% discount
            "endpoint": "code-bison@001",
            "context_window": 6144,
            "cost": {"prompt": 0, "completion": 0, "per_character": True},
        },  # shutdown on 2024-07-06
        "codechat-bison": {  # Preview, 100% discount
            "endpoint": "codechat-bison",  # redirects to latest
            "context_window": 6144,
            "cost": {"prompt": 0, "completion": 0, "per_character": True},
        },
        "codechat-bison-002": {  # Preview, 100% discount
            "endpoint": "codechat-bison@002",
            "context_window": 6144,
            "cost": {"prompt": 0, "completion": 0, "per_character": True},
        },
        "codechat-bison-001": {  # Preview, 100% discount
            "endpoint": "codechat-bison@001",
            "context_window": 6144,
            "cost": {"prompt": 0, "completion": 0, "per_character": True},
        },  # shutdown on 2024-07-06
        "code-gecko": {  # Preview, 100% discount
            "endpoint": "code-gecko",  # redirects to latest
            "context_window": 2048,
            "cost": {"prompt": 0, "completion": 0, "per_character": True},
        },
        "code-gecko-002": {  # Preview, 100% discount
            "endpoint": "code-gecko@002",
            "context_window": 2048,
            "cost": {"prompt": 0, "completion": 0, "per_character": True},
        },
        "code-gecko-001": {  # Preview, 100% discount
            "endpoint": "code-gecko@001",
            "context_window": 2048,
            "cost": {"prompt": 0, "completion": 0, "per_character": True},
        },  # shutdown on 2024-07-06
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
        gcloud_install_path: str,
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

    # TODO: convert to a property max_cost
    def get_cost_max(self, model_name: str) -> float:  # noqa: D102
        if model_name not in self.supported_models:
            raise ValueError("Model not supported")
        return (
            self.supported_models[model_name]["cost"]["completion"]
            * self.supported_models[model_name]["context_window"]
            / PRICING_PER_CHARACTERS
        )

    def compute_cost(
        self,
        model_name: str,
        prompts: List[str],
        response: ModelResponse,
    ) -> float:
        """
        Compute the cost of a completion.

        :param model_name: The model to use for completion.
        :param prompts: List of the prompt texts.
        :param response: Model response from LiteLLM completion.

        :return: The cost of the completion.
        """
        cost_data = self.supported_models[model_name]["cost"]  # type: ignore
        prompt_cost = sum(
            self.get_billable_characters(prompt, model_name)  # type: ignore
            * cost_data["prompt"]
            / PRICING_PER_CHARACTERS
            for prompt in prompts
        )
        completion_cost = (
            self.get_billable_characters(  # type: ignore
                response.choices[0]["message"]["content"],
                model_name,
            )
            * cost_data["completion"]
            / PRICING_PER_CHARACTERS
        )
        return prompt_cost + completion_cost
