from providers.completion.base_completion_provider import BaseCompletionProvider


class CustomProvider(BaseCompletionProvider):
    """
    A generic completion provider that uses the Mistral service.

    """

    def __init__(
        self,
        hub_model,
        custom_endpoint=None,
        custom_api_key=None,
    ):  # this is the alias
        super().__init__(
            hub_model,
            "",
            custom_endpoint=custom_endpoint,
            custom_api_key=custom_api_key,
        )
        model_name = self.custom_endpoint.mdl_name  # get the actual name
        # if null, default to the alias
        self.hub_model = model_name if model_name else hub_model

    @property
    def api_key(self) -> str:
        return self.custom_api_key.value

    @property
    def litellm_api_key_var(self) -> str:
        return self.custom_api_key.key

    @property
    def base_url(self):
        return self.custom_endpoint.url

    @property
    def provider_endpoint(self):
        return self.hub_model
