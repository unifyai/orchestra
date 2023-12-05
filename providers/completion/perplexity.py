from providers.completion.base_completion_provider import BaseCompletionProvider


class Perplexity(BaseCompletionProvider):
    """
    A completion provider that uses the Perplexity service.

    Supported models: https://docs.perplexity.ai/docs/model-cards
    """

    supported_models = {
        "codellama-34b-instruct": "perplexity/codellama-34b-instruct",
        "llama-2-70b-chat": "perplexity/llama-2-70b-chat",
        "mistral-7b-instruct": "perplexity/mistral-7b-instruct",
        "pplx-7b-chat": "perplexity/pplx-7b-chat",
        "pplx-70b-chat": "perplexity/pplx-70b-chat",
        "pplx-7b-online": "perplexity/pplx-7b-online",
        "pplx-70b-online": "perplexity/pplx-70b-online",
    }
