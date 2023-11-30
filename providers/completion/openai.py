import litellm
from providers.completion.base_completion_provider import BaseCompletionProvider


class OpenAI(BaseCompletionProvider):  # noqa: D101
    def __init__(self) -> None:
        """
        Initializes with list of supported models.

        Source: https://openai.com/pricing
        Deprecation: https://platform.openai.com/docs/deprecations/deprecation-history
        """
        self.supported_models = [
            "gpt-4-1106-preview",
            "gpt-3.5-turbo",
            "gpt-3.5-turbo-0301",  # shutdown on 2024-06-13
            "gpt-3.5-turbo-0613",  # shutdown on 2024-06-13
            "gpt-3.5-turbo-16k-0613",  # shutdown on 2024-06-13
            "gpt-3.5-turbo-1106",  # recommended replacement for above three
            "gpt-3.5-turbo-16k",
            "gpt-4",
            "gpt-4-0314",  # shutdown on 2024-06-13
            "gpt-4-0613",  # recommended replacement for above
            "gpt-4-32k",
            "gpt-4-32k-0314",  # shutdown on 2024-06-13
            "gpt-4-32k-0613",  # recommended replacement for above
            # Base GPT
            "ada",  # shutdown on 2024-01-04
            "babbage",  # shutdown on 2024-01-04
            "babbage-002",  # recommended replacement for above two
            "curie",  # shutdown on 2024-01-04
            "davinci",  # shutdown on 2024-01-04
            "davinci-002",  # recommended replacement for above two
            "code-davinci-002",  # shutdown on 2024-01-04
            # InstructGPT
            "text-ada-001",  # shutdown on 2024-01-04
            "text-babbage-001",  # shutdown on 2024-01-04
            "text-curie-001",  # shutdown on 2024-01-04
            "text-davinci-001",  # shutdown on 2024-01-04
            "text-davinci-002",  # shutdown on 2024-01-04
            "text-davinci-003",  # shutdown on 2024-01-04
            "gpt-3.5-turbo-instruct",  # recommended replacement for above seven
        ]

    def set_organization(self, organization) -> None:  # noqa: D102
        litellm.organization = organization

    def set_api_version(self, api_version) -> None:  # noqa: D102
        litellm.api_version = api_version
