from providers.completion.base_completion_provider import BaseCompletionProvider


class CustomProvider(BaseCompletionProvider):
    """
    A generic completion provider that uses the Mistral service.

    """

    def __init__(
        self,
        hub_model,
        custom_endpoint_dao,
        custom_api_key_dao,
        user_id,
        name,
        custom_api_key=None,
    ):
        super().__init__(hub_model, custom_api_key=custom_api_key)
        self.custom_api_key_dao = custom_api_key_dao
        self.custom_endpoint_dao = custom_endpoint_dao
        self.custom_endpoint = custom_endpoint_dao.filter(user_id, name)[0]
        self.custom_api_key = self.custom_api_key_dao.filter(
            id=self.custom_endpoint.key_id,
        )[0]

    @property
    def api_key(self) -> str:
        return self.custom_api_key.value

    @property
    def base_url(self):
        return self.custom_endpoint.url

    @property
    def provider_endpoint(self):
        return self.hub_model
