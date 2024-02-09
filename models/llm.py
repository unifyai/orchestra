import os
from typing import Any, Dict, List, Union, cast

from litellm import ModelResponse


class CompletionsModel:
    """Sets up a general CompletionsModel service."""

    def __init__(self, provider: str, model: str) -> None:
        from providers.completion import PROVIDER_CLASSES  # noqa: WPS433

        if provider.lower() not in PROVIDER_CLASSES:
            raise Exception("Provider not supported by Unify")  # noqa: WPS454

        if model.lower() not in PROVIDER_CLASSES[provider].supported_models:
            raise Exception(  # noqa: WPS454
                f"Model {model} not supported by {provider}",
            )

        self.provider_obj = PROVIDER_CLASSES[provider]()
        self.model = model.lower()

        if provider == "vertex-ai":
            from providers.completion.vertexai import VertexAI  # noqa: WPS433

            self.provider_obj = cast(VertexAI, self.provider_obj)
            self.provider_obj.set_service_account_credentials(
                str(os.getenv("ORCHESTRA_VERTEX_AI_SERVICE_ACC_JSON")),
                str(os.getenv("ORCHESTRA_VERTEX_AI_GCLOUD_PATH")),
            )
            self.provider_obj.set_project(str(os.getenv("ORCHESTRA_VERTEX_AI_PROJECT")))
            self.provider_obj.set_location(
                str(os.getenv("ORCHESTRA_VERTEX_AI_LOCATION")),
            )
        else:
            self.provider_obj.set_api_key(
                api_key=str(
                    os.getenv(
                        f"ORCHESTRA_{provider.replace('-', '_').upper()}_API_KEY",  # noqa: WPS237, E501
                    ),
                ),
            )

    def get_cost_max(self) -> float:  # noqa: D102
        return self.provider_obj.get_cost_max(self.model)

    def get_completion(  # noqa: D102
        self,
        messages: List[Dict[str, str]],
        **kwargs: Any,
    ) -> Union[ModelResponse, Any]:

        return self.provider_obj.complete(
            self.model,
            messages,
            **kwargs,
        )
