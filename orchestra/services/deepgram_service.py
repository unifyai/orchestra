import logging
from typing import Any, Dict, Optional

import httpx
from fastapi import HTTPException, status

from orchestra.services.bucket_service import BucketService
from orchestra.settings import settings


class DeepgramAPIError(HTTPException):
    def __init__(self, status_code: int, detail: str):
        super().__init__(status_code=status_code, detail=detail)


class DeepgramService:
    """
    Service for interacting with the Deepgram API for language detection.
    """

    def __init__(self):
        self.base_url = "https://api.deepgram.com/v1"
        if not settings.deepgram_api_key:
            raise ValueError("deepgram_api_key is not set in settings.")
        self.headers = {
            "Authorization": f"Token {settings.deepgram_api_key}",
            "Content-Type": "application/json",
        }

    def _handle_response(self, response: httpx.Response) -> Dict[str, Any]:
        if not (200 <= response.status_code < 300):
            try:
                error_data = response.json()
                error_detail = error_data.get("err_msg", response.text)
            except httpx.JSONDecodeError:
                error_detail = response.text
            raise DeepgramAPIError(
                status_code=response.status_code,
                detail=f"Deepgram API request failed: {error_detail}",
            )
        return response.json()

    def detect_language_from_audio(
        self,
        audio_content: bytes,
        user_id: str,
        content_type: str,
    ) -> Optional[str]:
        """
        Detects language from audio by uploading it to a temporary URL.
        Reference: https://developers.deepgram.com/reference/speech-to-text-api/listen
        """
        bucket_service = BucketService()
        temp_gcs_uri = None

        if settings.selected_voice_provider == "cartesia":
            # Reference: https://docs.pipecat.ai/server/services/tts/cartesia
            supported_languages = [
                "de",
                "en",
                "es",
                "fr",
                "hi",
                "it",
                "ja",
                "ko",
                "nl",
                "pl",
                "pt",
                "ru",
                "sv",
                "tr",
                "zh",
            ]
        else:  # Elevenlabs
            # Reference: https://elevenlabs.io/docs/models#multilingual-v2
            supported_languages = [
                "en",
                "ja",
                "zh",
                "de",
                "hi",
                "fr",
                "ko",
                "pt",
                "it",
                "es",
                "id",
                "nl",
                "tr",
                "fil",
                "pl",
                "sv",
                "bg",
                "ro",
                "ar",
                "cs",
                "el",
                "fi",
                "hr",
                "ms",
                "sk",
                "da",
                "ta",
                "uk",
                "ru",
            ]

        try:
            # Step 1: Upload audio to a temporary GCS location to get a public URL
            signed_url, temp_gcs_uri = bucket_service.upload_temp_assistant_file(
                file_content=audio_content,
                user_id=user_id,
                content_type=content_type,
            )

            # Step 2: Call Deepgram with the URL
            url = f"{self.base_url}/listen?detect_language=true"
            payload = {"url": signed_url}

            # Increased timeout for remote file processing
            with httpx.Client(timeout=60.0) as client:
                response = client.post(url, json=payload, headers=self.headers)

            response_data = self._handle_response(response)

            channels = response_data.get("results", {}).get("channels", [])
            if channels and isinstance(channels, list):
                detected_language = channels[0].get("detected_language")
                if detected_language:
                    if detected_language in supported_languages:
                        return detected_language
                    else:
                        logging.warning(
                            "Detected language in audio file not supported by current provider, falling back to english.",
                        )
                        return "en"

            return None
        except httpx.RequestError as e:
            logging.error(f"Request to Deepgram /listen failed: {e}")
            raise DeepgramAPIError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Request to Deepgram for audio analysis failed: {e}",
            )
        except DeepgramAPIError as e:
            logging.error(f"Deepgram API error during audio analysis: {e.detail}")
            raise e
        except Exception as e:
            # Catch other exceptions, like from BucketService
            logging.error(f"An unexpected error occurred during audio analysis: {e}")
            raise DeepgramAPIError(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"An unexpected error occurred during audio analysis: {str(e)}",
            )
        finally:
            # Step 3: Clean up the temporary file from GCS
            if temp_gcs_uri:
                try:
                    bucket_service.delete_assistant_file(temp_gcs_uri)
                except Exception as e_cleanup:
                    logging.error(
                        f"Failed to clean up temporary audio file {temp_gcs_uri}: {e_cleanup}",
                    )
