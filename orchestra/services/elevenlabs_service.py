import io
from typing import Any, Dict, Optional

import httpx
from fastapi import HTTPException, status

from orchestra.settings import settings


class ElevenLabsAPIError(HTTPException):
    def __init__(self, status_code: int, detail: str):
        super().__init__(status_code=status_code, detail=detail)


LONG_OPERATION_TIMEOUT = httpx.Timeout(60.0)  # 60 seconds


class ElevenLabsService:
    """
    Service for interacting with the ElevenLabs API.
    """

    def __init__(self):
        self.base_url = "https://api.elevenlabs.io/v2"
        if not settings.elevenlabs_api_key:
            raise ValueError("elevenlabs_api_key is not set in settings.")
        self.headers = {
            "xi-api-key": settings.elevenlabs_api_key,
        }

    def _handle_response(self, response: httpx.Response) -> Dict[str, Any]:
        try:
            response_data = response.json()
        except httpx.JSONDecodeError:
            raise ElevenLabsAPIError(
                status_code=response.status_code,
                detail=f"ElevenLabs API returned non-JSON response: {response.text}",
            )

        if not (200 <= response.status_code < 300):
            error_detail = response_data.get(
                "detail",
                response_data.get("message", "Unknown ElevenLabs API error"),
            )
            raise ElevenLabsAPIError(
                status_code=response.status_code,
                detail=str(error_detail),
            )
        return response_data

    def clone_voice(
        self,
        file_content: bytes,
        file_name: str,
        name: str,
        description: Optional[str] = None,
        remove_background_noise: bool = False,
    ) -> Dict[str, Any]:
        """
        Clones a voice using ElevenLabs API (Professional Voice Cloning).
        """
        url = f"{self.base_url}/voices/add"
        files = {
            "files": (file_name, io.BytesIO(file_content), "audio/mpeg"),
        }
        data = {
            "name": name,
            "remove_background_noise": str(remove_background_noise).lower(),
        }
        if description:
            data["description"] = description

        try:
            with httpx.Client(timeout=LONG_OPERATION_TIMEOUT) as client:
                response = client.post(
                    url,
                    data=data,
                    files=files,
                    headers=self.headers,
                )
            return self._handle_response(response)
        except httpx.RequestError as e:
            raise ElevenLabsAPIError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Request to ElevenLabs failed: {e}",
            )

    def delete_voice(self, voice_id: str) -> Dict[str, Any]:
        """
        Deletes a voice from ElevenLabs.
        """
        url = f"{self.base_url}/voices/{voice_id}"
        try:
            with httpx.Client() as client:
                response = client.delete(url, headers=self.headers)
            return self._handle_response(response)
        except httpx.RequestError as e:
            raise ElevenLabsAPIError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Request to ElevenLabs failed: {e}",
            )

    def list_voices(self) -> Dict[str, Any]:
        """
        List all available voices from ElevenLabs.
        """
        url = f"{self.base_url}/voices"
        try:
            with httpx.Client() as client:
                response = client.get(url, headers=self.headers)
            return self._handle_response(response)
        except httpx.RequestError as e:
            raise ElevenLabsAPIError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Request to ElevenLabs failed: {e}",
            )

    def get_voice(self, voice_id: str) -> Dict[str, Any]:
        """
        Get details of a specific voice from ElevenLabs.
        """
        url = f"{self.base_url}/voices/{voice_id}"
        try:
            with httpx.Client() as client:
                response = client.get(url, headers=self.headers)
            return self._handle_response(response)
        except httpx.RequestError as e:
            raise ElevenLabsAPIError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Request to ElevenLabs failed: {e}",
            )
