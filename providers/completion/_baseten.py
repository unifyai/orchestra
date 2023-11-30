from providers.completion.base_completion_provider import BaseCompletionProvider


class Baseten(BaseCompletionProvider):
    """
    A completion provider that uses the Baseten service.

    Initializes with list of few OSS models as example since hourly payment.
    """

    def __init__(self) -> None:
        self.supported_models = [
            "baseten/qvv0xeq",  # Falcon 7B
            "baseten/q841o8w",  # Wizard LM
            "baseten/31dxrj3",  # MPT 7B Base
        ]
