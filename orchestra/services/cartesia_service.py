import io
from typing import Any, Dict, Optional, Union

import httpx
from fastapi import HTTPException, status

from orchestra.settings import settings


class CartesiaAPIError(HTTPException):
    def __init__(self, status_code: int, detail: str):
        super().__init__(status_code=status_code, detail=detail)


LONG_OPERATION_TIMEOUT = httpx.Timeout(60.0)  # 60 seconds


class CartesiaService:
    """
    Service for interacting with the Cartesia API.
    """

    def __init__(self):
        self.base_url = "https://api.cartesia.ai"
        if not settings.cartesia_api_key:
            raise ValueError("cartesia_api_key is not set in settings.")
        self.headers = {
            "Cartesia-Version": settings.cartesia_api_version or "2025-04-16",
            "Authorization": f"Bearer {settings.cartesia_api_key}",
        }

    def _handle_response(self, response: httpx.Response) -> Dict[str, Any]:
        if response.status_code == 204:  # No content for successful DELETE
            return {"status": "success", "detail": "Operation successful, no content."}
        try:
            response_data = response.json()
        except httpx.JSONDecodeError:
            raise CartesiaAPIError(
                status_code=response.status_code,
                detail=f"Cartesia API returned non-JSON response: {response.text}",
            )

        if not (200 <= response.status_code < 300):
            error_detail = response_data.get(
                "detail",
                response_data.get("message", "Unknown Cartesia API error"),
            )
            raise CartesiaAPIError(
                status_code=response.status_code,
                detail=str(error_detail),
            )
        return response_data

    def clone_voice(
        self,
        file_content: bytes,
        file_name: str,
        name: str,
        language: str,
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Clones a voice using Cartesia API.
        """
        url = f"{self.base_url}/voices/clone"
        files = {
            "clip": (file_name, io.BytesIO(file_content), "application/octet-stream"),
        }
        payload: Dict[str, Union[str, None]] = {
            "name": name,
            "language": language,
            "description": description,
            "mode": "similarity",
        }
        # Filter out None values from payload
        payload = {k: v for k, v in payload.items() if v is not None}

        try:
            with httpx.Client(timeout=LONG_OPERATION_TIMEOUT) as client:
                response = client.post(
                    url,
                    data=payload,
                    files=files,
                    headers=self.headers,
                )
            return self._handle_response(response)
        except httpx.RequestError as e:
            raise CartesiaAPIError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Request to Cartesia failed: {e}",
            )

    def localize_voice(
        self,
        base_voice_id: str,
        name: str,
        target_language: str,
        original_speaker_gender: str,
        description: Optional[str] = None,
        dialect: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Localizes a voice using Cartesia API.
        """
        url = f"{self.base_url}/voices/localize"
        payload: Dict[str, Any] = {
            "voice_id": base_voice_id,
            "name": name,
            "language": target_language,
            "original_speaker_gender": original_speaker_gender,
        }
        if description:
            payload["description"] = description
        if dialect:
            payload["dialect"] = dialect

        headers_with_content_type = {**self.headers, "Content-Type": "application/json"}

        try:
            with httpx.Client(timeout=LONG_OPERATION_TIMEOUT) as client:
                response = client.post(
                    url,
                    json=payload,
                    headers=headers_with_content_type,
                )
            return self._handle_response(response)
        except httpx.RequestError as e:
            raise CartesiaAPIError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Request to Cartesia failed: {e}",
            )

    def delete_voice(self, voice_id: str) -> Dict[str, Any]:
        """
        Deletes a voice from Cartesia.
        """
        url = f"{self.base_url}/voices/{voice_id}"
        try:
            with httpx.Client() as client:
                response = client.delete(url, headers=self.headers)
            return self._handle_response(response)
        except httpx.RequestError as e:
            raise CartesiaAPIError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Request to Cartesia failed: {e}",
            )

    def list_voices(self) -> Dict[str, Any]:
        """
        List all available voices from Cartesia.
        """
        url = f"{self.base_url}/voices"
        try:
            with httpx.Client() as client:
                response = client.get(url, headers=self.headers)
            return self._handle_response(response)
        except httpx.RequestError as e:
            raise CartesiaAPIError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Request to Cartesia failed: {e}",
            )
