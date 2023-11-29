from providers.completion.base_completion_provider import BaseCompletionProvider


class Anthropic(BaseCompletionProvider):
    def __init__(self):
        self.supported_models = [
            "claude-2.1",
            "claude-2",
            "claude-instant-1",
            "claude-instant-1.2"
        ]
