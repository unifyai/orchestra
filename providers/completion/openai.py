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

    def __init__(self, hub_model, custom_endpoint=None, custom_api_key=None):
        super().__init__(
            hub_model,
            "openai",
            custom_endpoint=custom_endpoint,
            custom_api_key=custom_api_key,
        )
        self.supported_models = supported_models

    @property
    def api_key_var(self) -> str:
        return "ORCHESTRA_OPENAI_API_KEY"

    @property
    def litellm_api_key_var(self) -> str:
        return "OPENAI_API_KEY"


supported_models = {
    "gpt-3.5-turbo": {
        "endpoint": "gpt-3.5-turbo",
        "context_window": 16385,
        "cost": {"prompt": 0.5, "completion": 1.5},
    },
    "gpt-4": {
        "endpoint": "gpt-4",
        "context_window": 8192,
        "cost": {"prompt": 30, "completion": 60},
    },
    "gpt-4-turbo": {
        "endpoint": "gpt-4-turbo",
        "context_window": 128000,
        "cost": {"prompt": 10, "completion": 30},
    },
    "gpt-4o": {
        "endpoint": "gpt-4o",
        "context_window": 128000,
        "cost": {"prompt": 2.5, "completion": 10},
    },
    "gpt-4o-2024-05-13": {
        "endpoint": "gpt-4o-2024-05-13",
        "context_window": 128000,
        "cost": {"prompt": 5, "completion": 15},
    },
    "gpt-4o-mini": {
        "endpoint": "gpt-4o-mini",
        "context_window": 128000,
        "cost": {"prompt": 0.15, "completion": 0.6},
    },
    "chatgpt-4o-latest": {
        "endpoint": "chatgpt-4o-latest",
        "context_window": 128000,
        "cost": {"prompt": 5, "completion": 15},
    },
    "o1": {
        "endpoint": "o1",
        "context_window": 200000,
        "cost": {"prompt": 15, "completion": 60},
    },
    "o3-mini": {
        "endpoint": "o3-mini",
        "context_window": 200000,
        "cost": {"prompt": 1.1, "completion": 4.4},
    },
    "gpt-4o-search-preview": {
        "endpoint": "gpt-4o-search-preview",
        "context_window": 128000,
        "cost": {"prompt": 2.5, "completion": 10},
    },
    "gpt-4o-mini-search-preview": {
        "endpoint": "gpt-4o-mini-search-preview",
        "context_window": 128000,
        "cost": {"prompt": 0.15, "completion": 0.6},
    },
    "gpt-4.1": {
        "endpoint": "gpt-4.1",
        "context_window": 1047576,
        "cost": {"prompt": 2, "completion": 8},
    },
    "gpt-4.1-mini": {
        "endpoint": "gpt-4.1-mini",
        "context_window": 1047576,
        "cost": {"prompt": 0.4, "completion": 1.6},
    },
    "gpt-4.1-nano": {
        "endpoint": "gpt-4.1-nano",
        "context_window": 1047576,
        "cost": {"prompt": 0.1, "completion": 0.4},
    },
    "o3": {
        "endpoint": "o3",
        "context_window": 200000,
        "cost": {"prompt": 2, "completion": 8},
    },
    "o4-mini": {
        "endpoint": "o4-mini",
        "context_window": 200000,
        "cost": {"prompt": 1.1, "completion": 4.4},
    },
    "gpt-5": {
        "endpoint": "gpt-5",
        "context_window": 400000,
        "cost": {"prompt": 1.25, "completion": 10},
    },
    "gpt-5-mini": {
        "endpoint": "gpt-5-mini",
        "context_window": 400000,
        "cost": {"prompt": 0.25, "completion": 2},
    },
    "gpt-5-nano": {
        "endpoint": "gpt-5-nano",
        "context_window": 400000,
        "cost": {"prompt": 0.05, "completion": 0.4},
    },
    "gpt-5-chat-latest": {
        "endpoint": "gpt-5-chat-latest",
        "context_window": 400000,
        "cost": {"prompt": 1.25, "completion": 10},
    },
    "gpt-5.1": {
        "endpoint": "gpt-5.1",
        "context_window": 400000,
        "cost": {"prompt": 1.25, "completion": 10},
    },
    "gpt-5.1-chat-latest": {
        "endpoint": "gpt-5.1-chat-latest",
        "context_window": 400000,
        "cost": {"prompt": 1.25, "completion": 10},
    },
    "gpt-5.2": {
        "endpoint": "gpt-5.2",
        "context_window": 400000,
        "cost": {"prompt": 1.75, "completion": 14},
    },
    "gpt-5.2-chat-latest": {
        "endpoint": "gpt-5.2-chat-latest",
        "context_window": 400000,
        "cost": {"prompt": 1.75, "completion": 14},
    },
}
