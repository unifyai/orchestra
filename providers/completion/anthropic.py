from providers.completion.base_completion_provider import BaseCompletionProvider


class Anthropic(BaseCompletionProvider):
    """
    A completion provider that uses the Anthropic service.

    Supported models: https://docs.anthropic.com/claude/reference/selecting-a-model
    """

    supported_models = {
        "claude-2.1",
        "claude-2",
        "claude-instant-1",
        "claude-instant-1.2",
    }
