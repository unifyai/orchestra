import base64
import logging
from typing import Any, Dict, List, Optional

import openai
import requests
from providers.image_gen.base_imagegen_provider import BaseImageGenProvider

logger = logging.getLogger(__name__)


class OctoAI(BaseImageGenProvider):
    """
    A image generation provider provider that uses the OctoAI service.

    Supported models: https://docs.octoai.cloud/docs/image-gen-api-docs
    """

    supported_models: List[str] = [
        "sd",
        "sdxl",
    ]

    def parse_prompt(self, prompt_list):  # noqa: D102
        positive_prompt_str = ""
        negative_prompt_str = ""
        for prompt in prompt_list:
            text = prompt.get("text")
            weight = prompt.get("weight", 1)
            if weight != 1:
                text = f"{text} :{abs(weight)}"  # noqa: WPS237
                text = f"({text})"
            if weight < 0:
                negative_prompt_str += text + " "  # noqa: WPS336
            else:
                positive_prompt_str += text + " "  # noqa: WPS336
        return positive_prompt_str, negative_prompt_str

    def imagegen(  # noqa: C901, WPS210, WPS231, E501
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
            url = f"https://image.octoai.run/generate/{model}"
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            prompt, negative_prompt = self.parse_prompt(prompt)
            kwargs_to_payload_keys = {
                "sampler": "sampler",
                "height": "height",
                "width": "width",
                "cfg_scale": "cfg_scale",
                "steps": "steps",
                "samples": "num_images",
                "seed": "seed",
                "use_refiner": "use_refiner",
                "high_noise_frac": "high_noise_frac",
                "checkpoint": "checkpoint",
                "loras": "loras",
                "textual_inversions": "textual_inversions",
                "vae": "vae",
                "init_image": "init_image",
                "strength": "strength",
                "mask_image": "mask_image",
            }
            payload = {
                payload_key: kwargs.get(kwargs_key, None)
                for kwargs_key, payload_key in kwargs_to_payload_keys.items()
                if kwargs.get(kwargs_key, None) is not None
            }
            payload["prompt"] = prompt
            if negative_prompt != "":
                payload["negative_prompt"] = negative_prompt

            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=30,  # noqa: WPS432, E501, C812
            )
            if response.status_code != 200:  # noqa: WPS432
                return response

            img_list = response.json()["images"]
            images = [base64.b64decode(img["image_b64"]) for img in img_list]

            return {"images": images}
        except openai.APITimeoutError as error:
            logger.error(f"Raised openai.APITimeoutError, Error: {error}")
        except Exception as error:
            error_type = type(error)
            logger.error(f"Raised error type: {error_type}, Error: {error}")
        return None
