import logging
from typing import Dict, List, Optional

import openai
from stability_sdk import client
from stability_sdk.interfaces.gooseai.generation import generation_pb2 as generation

logger = logging.getLogger(__name__)


class Stability:
    """
    A image generation provider provider that uses the Stability service.

    Supported models: https://platform.stability.ai/docs/features/api-parameters#engine
    """

    supported_models: List[str] = [
        "stable-diffusion-xl-1024-v0-9",
        "stable-diffusion-xl-1024-v1-0",
    ]

    def __init__(self) -> None:
        self.model: str = ""
        self._stability_client: client = None  # Rename instance attribute

    def set_api_key(self, api_key: str, engine: str = "") -> None:
        """
        Call the config setter for Stability.

        :param api_key: The API key to set.
        :type api_key: str
        :param engine: The engine (model) to use.
        :type engine: str
        """
        self.set_stability_config(api_key, engine)

    def set_stability_config(self, api_key: str, engine: str):
        """
        Set the config parameters for Stability.

        :param api_key: The API key to set.
        :type api_key: str
        :param engine: The engine (model) to use.
        :type engine: str
        :raises ValueError: If the provided engine is not supported.
        """
        if engine not in self.supported_models:
            raise ValueError("Model not supported")
        self._stability_client = client.StabilityInference(
            key=api_key,
            engine=engine,
        )

    def imagegen(
        self, prompt: str, model: str, kwargs: Optional[Dict],
    ) -> Optional[Dict]:
        """
        Generates images using the Stability API.

        :param prompt: The text prompt to use for image generation.
        :type prompt: str
        :param model: The model to use for image generation.
        :type model: str
        :param kwargs: Additional keyword arguments to pass to the API.
        :type kwargs: Optional[Any]
        :return: A dictionary containing the generated images,
            or None if an error occurs.
        :rtype: Optional[Dict]
        """
        try:
            if kwargs is None:
                kwargs = {}
            response = self._stability_client.generate(  # Use renamed attribute
                prompt=prompt,
                seed=kwargs.get("seed", None),
                init_image=kwargs.get("init_image", None),
                height=kwargs.get("height", None),
                width=kwargs.get("width", None),
                steps=kwargs.get("steps", None),
                samples=kwargs.get("samples", None),
                cfg_scale=kwargs.get("cfg_scale", None),
                sampler=kwargs.get("sampler", None),
                mask_image=kwargs.get("mask_image", None),
                start_schedule=kwargs.get("start_schedule", None),
                end_schedule=kwargs.get("end_schedule", None),
            )
            images = [
                artifact.binary
                for resp in response
                for artifact in resp.artifacts
                if artifact.type == generation.ARTIFACT_IMAGE
            ]

            return {"images": images}
        except openai.APITimeoutError as error:
            logger.error(f"Raised openai.APITimeoutError, Error: {error}")
        except Exception as error:
            error_type = type(error)
            logger.error(f"Raised error type: {error_type}, Error: {error}")
        return None
