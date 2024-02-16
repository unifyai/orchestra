from providers.completion.base_completion_provider import BaseCompletionProvider


class OpenAI(BaseCompletionProvider):
    """
    A completion provider that uses the OpenAI service.

    Source: https://openai.com/pricing
    Deprecation: https://platform.openai.com/docs/deprecations/deprecation-history
    Pricing is per million tokens.

    Note: OpenAI's model versioning ends with an -MMDD suffix; e.g., gpt-4-0613.
    The undated model name, e.g., gpt-4, will typically point to the latest
    version (e.g. gpt-4 points to gpt-4-0613).
    """

    def __init__(self, hub_model):
        super().__init__(hub_model)
        self.supported_models = supported_models

    @property
    def api_key_var(self) -> str:
        return "ORCHESTRA_OPENAI_API_KEY"

    @property
    def base_url(self):
        return "https://api.openai.com/v1"


supported_models = {
    "gpt-4-1106-preview": {
        "endpoint": "gpt-4-1106-preview",
        "context_window": 128000,
        "cost": {"prompt": 10, "completion": 30},
    },
    "gpt-3.5-turbo": {
        "endpoint": "gpt-3.5-turbo",  # redirects to latest: gpt-3.5-turbo-1106
        "context_window": 16385,
        "cost": {"prompt": 1, "completion": 2},
    },
    "gpt-3.5-turbo-0301": {
        "endpoint": "gpt-3.5-turbo-0301",
        "context_window": 4096,
        "cost": {"prompt": 1.5, "completion": 2},
    },  # shutdown on 2024-06-13
    "gpt-3.5-turbo-0613": {
        "endpoint": "gpt-3.5-turbo-0613",
        "context_window": 4096,
        "cost": {"prompt": 1.5, "completion": 2},
    },  # shutdown on 2024-06-13
    "gpt-3.5-turbo-16k-0613": {
        "endpoint": "gpt-3.5-turbo-16k-0613",
        "context_window": 16385,
        "cost": {"prompt": 3, "completion": 4},
    },  # shutdown on 2024-06-13
    "gpt-3.5-turbo-1106": {
        "endpoint": "gpt-3.5-turbo-1106",
        "context_window": 16385,
        "cost": {"prompt": 1, "completion": 2},
    },  # recommended replacement for above three
    "gpt-3.5-turbo-16k": {
        "endpoint": "gpt-3.5-turbo-16k",  # redirects to gpt-3.5-turbo-16k-0613
        "context_window": 16385,
        "cost": {"prompt": 3, "completion": 4},
    },
    "gpt-4": {
        "endpoint": "gpt-4",  # redirects to gpt-4-0613
        "context_window": 8192,
        "cost": {"prompt": 30, "completion": 60},
    },
    "gpt-4-0314": {
        "endpoint": "gpt-4-0314",
        "context_window": 8192,
        "cost": {"prompt": 30, "completion": 60},
    },  # shutdown on 2024-06-13
    "gpt-4-0613": {
        "endpoint": "gpt-4-0613",
        "context_window": 8192,
        "cost": {"prompt": 30, "completion": 60},
    },  # recommended replacement for above
    "gpt-4-32k": {  # redirects to gpt-4-32k-0613
        "endpoint": "gpt-4-32k",
        "context_window": 32768,
        "cost": {"prompt": 60, "completion": 120},
    },
    "gpt-4-32k-0314": {
        "endpoint": "gpt-4-32k-0314",
        "context_window": 32768,
        "cost": {"prompt": 60, "completion": 120},
    },  # shutdown on 2024-06-13
    "gpt-4-32k-0613": {
        "endpoint": "gpt-4-32k-0613",
        "context_window": 32768,
        "cost": {"prompt": 60, "completion": 120},
    },  # recommended replacement for above
    # Base GPT
    "babbage-002": {
        "endpoint": "babbage-002",
        "context_window": 16384,
        "cost": {"prompt": 16, "completion": 16},
    },
    "davinci-002": {
        "endpoint": "davinci-002",
        "context_window": 16384,
        "cost": {"prompt": 12, "completion": 12},
    },
    # InstructGPT
    "gpt-3.5-turbo-instruct": {
        "endpoint": "gpt-3.5-turbo-instruct",
        "context_window": 4096,
        "cost": {"prompt": 1.5, "completion": 2},
    },
}
