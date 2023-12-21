import os

from providers.image_gen import PROVIDER_CLASSES


class ImagegenModel:
    """Sets up a general Image Generation Model service."""

    def __init__(self, provider: str, model: str) -> None:
        if provider.lower() not in PROVIDER_CLASSES:
            raise Exception("Provider not supported by Unify")  # noqa: WPS454

        if model.lower() not in PROVIDER_CLASSES[provider].supported_models:
            raise Exception(f"Model not supported by {provider}")  # noqa: WPS454

        self.provider_obj = PROVIDER_CLASSES[provider]()
        self.model = model.lower()
        api_key = str(os.getenv(f"ORCHESTRA_{provider.upper()}_API_KEY"))
        if api_key is not None:
            self.set_api_key(api_key, model)

    def set_api_key(self, api_key: str, model: str = "") -> None:  # noqa: D102
        self.provider_obj.set_api_key(api_key, model)

    def get_image(  # noqa: D102
        self,
        prompt: str,
        kwargs,
    ):
        return self.provider_obj.imagegen(
            prompt,
            self.model,
            kwargs,
        )
