import logging
from typing import Any, Dict, Optional

import replicate
from fastapi import HTTPException, status
from replicate.model import Prediction

from orchestra.settings import settings


class ReplicateAPIError(HTTPException):
    def __init__(self, status_code: int, detail: str):
        super().__init__(status_code=status_code, detail=detail)


class ReplicateService:
    """
    Service for interacting with the Replicate API.
    """

    def __init__(self):
        api_token = settings.replicate_api_key
        if not api_token:
            raise ValueError("REPLICATE_API_TOKEN environment variable is not set.")
        self.client = replicate.Client(api_token=api_token)

    def generate_photo(
        self,
        prompt: str,
        aspect_ratio: str,
        output_format: str,
        output_quality: int,
        safety_tolerance: float,
        prompt_upsampling: bool,
    ) -> str:
        """
        Generates a new image from a text prompt using FLUX 1.1 Pro.
        """
        try:
            model_identifier = "black-forest-labs/flux-1.1-pro"
            output = self.client.run(
                model_identifier,
                input={
                    "prompt": prompt,
                    "aspect_ratio": aspect_ratio,
                    "output_format": output_format,
                    "output_quality": output_quality,
                    "safety_tolerance": safety_tolerance,
                    "prompt_upsampling": prompt_upsampling,
                },
            )
            if not output or not isinstance(output, str):
                raise ReplicateAPIError(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Unexpected output type from Replicate: {repr(output)}",
                )
            return output

        except Exception as e:
            logging.error(f"Replicate generate_photo failed: {e}", exc_info=True)
            raise ReplicateAPIError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Request to Replicate API failed: {e}",
            )

    def edit_photo(
        self,
        prompt: str,
        input_image: str,
        aspect_ratio: str,
        output_format: str,
        safety_tolerance: float,
    ) -> str:
        """
        Edits an image with a text prompt using FLUX Kontext Pro.
        """
        try:
            model_identifier = "black-forest-labs/flux-kontext-pro"
            output = self.client.run(
                model_identifier,
                input={
                    "prompt": prompt,
                    "input_image": input_image,
                    "aspect_ratio": aspect_ratio,
                    "output_format": output_format,
                    "safety_tolerance": safety_tolerance,
                },
            )
            if not output or not isinstance(output, str):
                raise ReplicateAPIError(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Unexpected output from Replicate: {repr(output)}",
                )
            return output

        except Exception as e:
            logging.error(f"Replicate edit_photo failed: {e}", exc_info=True)
            raise ReplicateAPIError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Request to Replicate API failed: {e}",
            )

    # Default prompt for OmniHuman 1.5 optimized for natural talking-head animation.
    # Structure follows the recommended format for the model's documentation.
    _OMNI_HUMAN_DEFAULT_PROMPT = (
        "A steady medium close-up shot. The person looks directly at the camera with a calm, "
        "friendly expression and talks naturally. Subtle head movements and gentle facial "
        "expressions accompany the speech, with natural eye blinks and slight eyebrow raises "
        "for emphasis."
    )

    def create_video_animation(
        self,
        image_url: str,
        audio_url: str,
        seed: Optional[int] = None,
    ) -> Prediction:
        """
        Creates a video animation prediction using bytedance/omni-human-1.5 (OmniHuman 1.5).
        Video duration is derived from audio length by the model.
        """
        try:
            model_identifier = "bytedance/omni-human-1.5"
            model_input: Dict[str, Any] = {
                "image": image_url,
                "audio": audio_url,
                "prompt": self._OMNI_HUMAN_DEFAULT_PROMPT,
            }
            if seed is not None:
                model_input["seed"] = seed

            prediction = self.client.predictions.create(
                version=model_identifier,
                input=model_input,
            )
            return prediction
        except Exception as e:
            logging.error(
                f"Replicate create_video_animation failed: {e}",
                exc_info=True,
            )
            raise ReplicateAPIError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Request to Replicate API for video animation failed: {e}",
            )

    def get_prediction(self, prediction_id: str) -> Prediction:
        """
        Gets a prediction from Replicate.
        """
        try:
            prediction = self.client.predictions.get(prediction_id)
            return prediction
        except Exception as e:
            logging.error(
                f"Replicate get_prediction failed for id {prediction_id}: {e}",
                exc_info=True,
            )
            raise ReplicateAPIError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Request to Replicate API to get prediction failed: {e}",
            )

    def cancel_prediction(self, prediction_id: str) -> Prediction:
        """
        Cancels a prediction on Replicate.
        """
        try:
            prediction = self.client.predictions.cancel(prediction_id)
            return prediction
        except Exception as e:
            logging.error(
                f"Replicate cancel_prediction failed for id {prediction_id}: {e}",
                exc_info=True,
            )
            raise ReplicateAPIError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Request to Replicate API to cancel prediction failed: {e}",
            )
