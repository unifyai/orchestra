from providers.completion.base_completion_provider import BaseCompletionProvider


class Baseten(BaseCompletionProvider):
    """
    A completion provider that uses the Baseten service.

    Initializes with list of few OSS models as example since hourly payment.
    """

    supported_models = {
        "falcon-7b": "baseten/qvv0xeq",
        "wizardlm-70b": "baseten/q841o8w",
        "mpt-7b": "baseten/31dxrj3",
    }
