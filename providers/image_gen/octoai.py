import logging
from typing import Dict, List, Optional
import requests
import base64
import openai

logger = logging.getLogger(__name__)


class OctoAI:
    """
    A image generation provider provider that uses the OctoAI service.

    Supported models: https://docs.octoai.cloud/docs/image-gen-api-docs
    """

    supported_models: List[str] = [
        "sd",
        "sdxl",
    ]

    def __init__(self) -> None:
        self.model: str = ""

    def set_api_key(self, api_key: str, engine: str = "") -> None:
        """
        Call the config setter for Stability.

        :param api_key: The API key to set.
        :type api_key: str
        """
        self.api_key = api_key

    def imagegen(
        self,
        prompt: str,
        model: str,
        kwargs: Optional[Dict],
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
            url = f"https://image.octoai.run/generate/{model}"
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "prompt": prompt,
                "prompt_2": kwargs.get("prompt_2", None),
                "negative_prompt": kwargs.get("negative_prompt", None),
                "negative_prompt_2": kwargs.get("negative_prompt_2", None),
                "sampler": kwargs.get("sampler", "DDIM"),
                "height": kwargs.get("height", None),
                "width": kwargs.get("width", None),
                "cfg_scale": kwargs.get("cfg_scale", None),
                "steps": kwargs.get("steps", None),
                "num_images": kwargs.get("samples", None),
                "seed": kwargs.get("seed", None),
                "use_refiner": kwargs.get("use_refiner", False),
                "high_noise_frac": kwargs.get("high_noise_frac", None),
                "checkpoint": kwargs.get("checkpoint", None),
                "loras": kwargs.get("loras", None),
                "textual_inversions": kwargs.get("textual_inversions", None),
                "vae": kwargs.get("vae", None),
                "init_image": kwargs.get("image", None),
                "strength": kwargs.get("strength", None),
                "mask_image": kwargs.get("mask_image", None),
                "seed": kwargs.get("seed", None),
            }

            response = requests.post(url, headers=headers, json=payload)

            if response.status_code != 200:
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


