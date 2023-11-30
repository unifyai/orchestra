from providers.completion.base_completion_provider import BaseCompletionProvider


class Perplexity(BaseCompletionProvider):
    """
    A completion provider that uses the Perplexity service.

    Supported models: https://docs.perplexity.ai/docs/model-cards
    """

    def __init__(self) -> None:
        self.supported_models = [
            "perplexity/codellama-34b-instruct",
            "perplexity/llama-2-70b-chat",
            "perplexity/mistral-7b-instruct",
            "perplexity/pplx-7b-chat",
            "perplexity/pplx-70b-chat",
            "perplexity/pplx-7b-online",
            "perplexity/pplx-70b-online",
        ]
