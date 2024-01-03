import base64
import logging
from typing import Any, Dict, List, Optional

import openai
import requests
from providers.image_gen.base_imagegen_provider import BaseImageGenProvider

logger = logging.getLogger(__name__)


class Stability(BaseImageGenProvider):
    """
    A image generation provider provider that uses the Stability service.

    Supported models: https://platform.stability.ai/docs/features/api-parameters#engine
    """

    supported_models: List[str] = [
        "stable-diffusion-xl-1024-v0-9",
        "stable-diffusion-xl-1024-v1-0",
        "stable-diffusion-v1-6",
    ]

    def imagegen(  # noqa: C901, WPS212, WPS210, WPS231, E501
        self,
        prompt: str,
        model: str,
        kwargs: Optional[Dict],
    ) -> Optional[Any]:
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

        :raises ValueError: If the specified model is not supported.
        """
        if model not in self.supported_models:
            raise ValueError("Model not supported")
        try:
            if kwargs is None:
                kwargs = {}
            engine_id = f"https://api.stability.ai/v1/generation/{model}/"
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            }
            kwargs_keys = [
                "seed",
                "height",
                "width",
                "steps",
                "samples",
                "cfg_scale",
                "sampler",
                "mask_image",
                "style_preset",
                "clip_guidance_preset",
            ]
            payload = {
                key: kwargs.get(key)
                for key in kwargs_keys
                if kwargs.get(key) is not None
            }
            payload["text_prompts"] = prompt

            if kwargs.get("init_image", None) is not None:
                payload["init_image"] = kwargs.get("init_image", None)
                if kwargs.get("strength", None) is not None:
                    payload["image_strength"] = kwargs.get("strength", None)
                engine_id += "image-to-image"  # noqa: WPS336
                if kwargs.get("mask_source", None) is not None:
                    payload["mask_source"] = kwargs.get("mask_source", None)
                    if kwargs.get("mask_image", None) is not None:
                        payload["mask_image"] = kwargs.get(  # noqa: WPS220, E501
                            "mask_image",
                            None,  # noqa: C812
                        )
                    engine_id += "/masking"  # noqa: WPS336
            else:
                engine_id += "text-to-image"  # noqa: WPS336

            response = requests.post(
                engine_id,
                headers=headers,
                json=payload,
                timeout=30,  # noqa: C812, WPS432, E501
            )  # noqa: E501

            if response.status_code != 200:  # noqa: WPS432
                return response

            images = [
                base64.b64decode(image["base64"])
                for image in response.json()["artifacts"]
            ]

            return {"images": images}
        except openai.APITimeoutError as error:
            logger.error(f"Raised openai.APITimeoutError, Error: {error}")
        except Exception as error:
            error_type = type(error)
            logger.error(f"Raised error type: {error_type}, Error: {error}")
        return None
