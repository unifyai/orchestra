import os
from typing import Any, List

from providers.completion.base_completion_provider import BaseCompletionProvider


class AzureAI(BaseCompletionProvider):
    """
    A completion provider that uses the Azure AI service.

    Supported models: https://ai.azure.com/explore/models?wsid=/subscriptions/f4cde163-8224-40b0-a18b-6340fe52f8d3/resourceGroups/sso/providers/Microsoft.MachineLearningServices/workspaces/sso-1&tid=714aedce-c300-4844-8404-0dd5b92edb51
    Pricing is per million tokens: https://ai.azure.com/explore/models?wsid=/subscriptions/f4cde163-8224-40b0-a18b-6340fe52f8d3/resourceGroups/sso/providers/Microsoft.MachineLearningServices/workspaces/sso-1&tid=714aedce-c300-4844-8404-0dd5b92edb51
    """

    def __init__(self, hub_model, custom_api_key=None):
        super().__init__(hub_model, custom_api_key=custom_api_key)
        self.supported_models = supported_models
        self.project = os.environ.get("AZURE_PROJECT")
        self.region = os.environ.get("AZURE_REGION")
        self.model_api_key = (
            os.environ.get(
                f'AZURE_AI_{hub_model.replace(".", "_").replace("-", "_").upper()}_API_KEY',
            )
            if "gpt" not in hub_model
            else None
        )

    @property
    def api_key_var(self) -> str:
        return (
            "AZURE_OPENAI_API_KEY"
            if "gpt" in self.provider_endpoint
            else "AZURE_AI_API_KEY"
        )

    @property
    def litellm_api_key_var(self) -> str:
        return (
            "AZURE_API_KEY" if "gpt" in self.provider_endpoint else "AZURE_AI_API_KEY"
        )

    def get_azure_ai_url(self):
        return (
            f'https://{self.provider_endpoint.lstrip("azure_ai/")}'
            f".{self.region}.models.ai.azure.com/"
        )

    def get_azure_openai_details(self):
        version = self.supported_models[self.hub_model].get("version")
        endpoint = f"https://{self.project}.openai.azure.com"
        return version, endpoint

    def set_env_variables(self):
        if "gpt" in self.provider_endpoint:
            version, endpoint = self.get_azure_openai_details()
            os.environ["AZURE_API_BASE"] = endpoint
            os.environ["AZURE_API_VERSION"] = version
        else:
            os.environ["AZURE_AI_API_BASE"] = self.get_azure_ai_url()
            os.environ["AZURE_AI_API_KEY"] = self.model_api_key

    def __call__(
        self,
        messages: List,  # type: ignore
        stream: bool = False,
        **kwargs: Any,
    ):
        self.set_env_variables()
        return super().__call__(messages, stream=stream, **kwargs)

    def __call_async__(
        self,
        messages: List,  # type: ignore
        stream: bool = False,
        **kwargs: Any,
    ) -> Any:
        self.set_env_variables()
        return super().__call_async__(messages, stream, **kwargs)


supported_models = {
    "gpt-4o": {
        "endpoint": "azure/gpt-4o_deployment_1723186259915",
        "version": "2023-03-15-preview",
        "context_window": 128000,
        "cost": {"prompt": 5, "completion": 15},
    },
    "gpt-4o-mini": {
        "endpoint": "azure/gpt-4o-mini_deployment_1723185451601",
        "version": "2023-03-15-preview",
        "context_window": 128000,
        "cost": {"prompt": 0.15, "completion": 0.6},
    },
    "llama-3.1-405b-chat": {
        "endpoint": "azure_ai/Meta-Llama-3-1-405B-Instruct-aog",
        "context_window": 128000,
        "cost": {"prompt": 5.33, "completion": 1.6},
    },
    "llama-3.1-70b-chat": {
        "endpoint": "azure_ai/Meta-Llama-3-1-70B-Instruct-srdw",
        "context_window": 128000,
        "cost": {"prompt": 2.68, "completion": 3.54},
    },
    "llama-3.1-8b-chat": {
        "endpoint": "azure_ai/Meta-Llama-3-1-8B-Instruct-vjwtr",
        "context_window": 128000,
        "cost": {"prompt": 0.61, "completion": 0.61},
    },
}
