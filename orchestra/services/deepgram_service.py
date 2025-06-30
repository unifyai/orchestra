import logging
from typing import Any, Dict, Optional

import httpx
from fastapi import HTTPException, status

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
            "Authorization": f"Bearer {settings.deepgram_api_key}",
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

    def detect_language_from_text(self, text: str) -> Optional[str]:
        """
        Detects language from a string of text.
        Reference: https://developers.deepgram.com/reference/text-intelligence-api/text-read
        """
        url = f"{self.base_url}/read?detect_language=true"
        payload = {"text": text}
        try:
            with httpx.Client() as client:
                response = client.post(url, json=payload, headers=self.headers)

            response_data = self._handle_response(response)

            language = response_data.get("results", {}).get("language")
            if language:
                return language
            return None
        except httpx.RequestError as e:
            logging.error(f"Request to Deepgram /read failed: {e}")
            raise DeepgramAPIError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Request to Deepgram for text analysis failed: {e}",
            )
        except DeepgramAPIError as e:
            logging.error(f"Deepgram API error during text analysis: {e.detail}")
            raise e

    def detect_language_from_audio(
        self,
        audio_content: bytes,
        content_type: str,
    ) -> Optional[str]:
        """
        Detects language from audio bytes.
        Reference: https://developers.deepgram.com/reference/speech-to-text-api/listen
        """
        url = (
            f"{self.base_url}/listen?detect_language=true&model=nova-2&utterances=false"
        )

        request_headers = self.headers.copy()
        request_headers["Content-Type"] = content_type

        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    url,
                    content=audio_content,
                    headers=request_headers,
                )

            response_data = self._handle_response(response)

            channels = response_data.get("results", {}).get("channels", [])
            if channels and isinstance(channels, list) and len(channels) > 0:
                alternatives = channels[0].get("alternatives", [])
                if (
                    alternatives
                    and isinstance(alternatives, list)
                    and len(alternatives) > 0
                ):
                    detected_language = alternatives[0].get("language")
                    if detected_language:
                        return detected_language
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
