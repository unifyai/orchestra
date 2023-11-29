import litellm
from providers.completion.base_completion_provider import BaseCompletionProvider


class OpenAI(BaseCompletionProvider):
    def __init__(self):
        self.supported_models = [
            "gpt-4-1106-preview",
            "gpt-3.5-turbo",
            "gpt-3.5-turbo-0301",
            "gpt-3.5-turbo-0613",
            "gpt-3.5-turbo-16k",
            "gpt-3.5-turbo-16k-0613",
            "gpt-4",
            "gpt-4-0314",
            "gpt-4-0613",
            "gpt-4-32k",
            "gpt-4-32k-0314",
            "gpt-4-32k-0613",
        ]

    def set_organization(self, organization):
        litellm.organization = organization

    def set_api_version(self, api_version):
        litellm.api_version = api_version
