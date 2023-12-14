from providers.completion.base_completion_provider import BaseCompletionProvider


class Anthropic(BaseCompletionProvider):
    """
    A completion provider that uses the Anthropic service.

    Supported models: https://docs.anthropic.com/claude/reference/selecting-a-model
    Pricing is per million tokens.
    """

    supported_models = {
        "claude-2.1": {
            "endpoint": "claude-2.1",
            "context_window": 200000,
            "cost": {"prompt": 8, "completion": 24},
        },
        "claude-2": {
            "endpoint": "claude-2",
            "context_window": 100000,
            "cost": {"prompt": 8, "completion": 24},
        },
        "claude-instant-1": {
            "endpoint": "claude-instant-1",
            "context_window": 100000,
            "cost": {"prompt": 0.8, "completion": 2.4},
        },
        "claude-instant-1.2": {
            "endpoint": "claude-instant-1.2",
            "context_window": 100000,
            "cost": {"prompt": 0.8, "completion": 2.4},
        },
    }
