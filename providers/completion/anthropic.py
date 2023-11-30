from providers.completion.base_completion_provider import BaseCompletionProvider


class Anthropic(BaseCompletionProvider):  # noqa: D101
    def __init__(self):
        # https://docs.anthropic.com/claude/reference/selecting-a-model
        self.supported_models = [
            "claude-2.1",
            "claude-2",
            "claude-instant-1",
            "claude-instant-1.2",
        ]
