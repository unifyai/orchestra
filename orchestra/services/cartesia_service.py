import io
from typing import Any, Dict, Optional, Tuple, Union

import httpx
from fastapi import HTTPException, status

from orchestra.settings import settings


class CartesiaAPIError(HTTPException):
    def __init__(self, status_code: int, detail: str):
        super().__init__(status_code=status_code, detail=detail)


LONG_OPERATION_TIMEOUT = httpx.Timeout(60.0)  # 60 seconds
TTS_TIMEOUT = httpx.Timeout(30.0)  # Timeout for TTS requests


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
            "Content-Type": "application/json",
        }

    def _handle_audio_response(self, response: httpx.Response) -> bytes:
        if not (200 <= response.status_code < 300):
            try:
                # Attempt to parse error detail if JSON
                error_data = response.json()
                error_detail = error_data.get("detail", response.text)
            except httpx.JSONDecodeError:
                error_detail = response.text
            raise CartesiaAPIError(
                status_code=response.status_code,
                detail=f"Cartesia API audio generation failed: {error_detail}",
            )
        return response.content

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
        Reference: https://docs.cartesia.ai/2025-04-16/api-reference/voices/clone
        """
        url = f"{self.base_url}/voices/clone"
        files = {
            "clip": (file_name, io.BytesIO(file_content), "application/octet-stream"),
        }
        data: Dict[str, Union[str, None]] = {
            "name": name,
            "language": language,
            "description": description,
            "mode": "similarity",
        }
        # Filter out None values from data
        data = {k: v for k, v in data.items() if v is not None}

        # For multipart/form-data, httpx sets Content-Type. Do not set it in self.headers for this one.
        request_headers = self.headers.copy()
        request_headers.pop("Content-Type", None)

        try:
            with httpx.Client(timeout=LONG_OPERATION_TIMEOUT) as client:
                response = client.post(
                    url,
                    data=data,
                    files=files,
                    headers=request_headers,
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
        Reference: https://docs.cartesia.ai/2025-04-16/api-reference/voices/delete
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
        Reference: https://docs.cartesia.ai/2025-04-16/api-reference/voices/list
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

    def get_voice(self, id: str) -> Dict[str, Any]:
        """
        Get details of a specific voice from Cartesia.
        Reference: https://docs.cartesia.ai/2025-04-16/api-reference/voices/get
        """
        url = f"{self.base_url}/voices/{id}"
        try:
            with httpx.Client() as client:
                response = client.get(url, headers=self.headers)
            return self._handle_response(response)
        except httpx.RequestError as e:
            raise CartesiaAPIError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Request to Cartesia failed: {e}",
            )

    def generate_speech(
        self,
        transcript: str,
        voice_id: str,  # This is Cartesia's internal voice ID
        model_id: Optional[str] = "sonic-2",
        output_format_container: str = "mp3",
        output_sample_rate: Optional[int] = None,  # e.g. 44100
        output_bit_rate: Optional[int] = None,  # e.g. 128000 (not for PCM)
        language: Optional[str] = "en",
    ) -> Tuple[bytes, str]:
        """
        Generates speech from text using Cartesia API and returns raw audio bytes and content type.
        Reference: https://docs.cartesia.ai/2025-04-16/api-reference/tts/bytes
        """
        url = f"{self.base_url}/tts/bytes"

        payload_output_format: Dict[str, Any] = {"container": output_format_container}
        if output_sample_rate:
            payload_output_format["sample_rate"] = output_sample_rate

        content_type = f"audio/{output_format_container}"
        if output_format_container == "mp3":
            content_type = "audio/mpeg"  # Standard MIME for MP3
            if output_bit_rate:
                payload_output_format["bit_rate"] = output_bit_rate
            elif not output_sample_rate:  # Default if nothing specified
                payload_output_format["sample_rate"] = 44100
                payload_output_format["bit_rate"] = 128000
        elif output_format_container in ["pcm_s16le", "pcm_mulaw"]:
            # Determine encoding and default sample rate for PCM
            if output_format_container == "pcm_s16le":
                payload_output_format["encoding"] = "pcm_s16le"
                payload_output_format["sample_rate"] = (
                    output_sample_rate or 24000
                )  # Cartesia default for pcm_s16le
                content_type = f"audio/L16; rate={payload_output_format['sample_rate']}; channels=1"
            else:  # pcm_mulaw
                payload_output_format["encoding"] = "pcm_mulaw"
                payload_output_format["sample_rate"] = (
                    output_sample_rate or 8000
                )  # Cartesia default for pcm_mulaw
                content_type = f"audio/mulaw; rate={payload_output_format['sample_rate']}; channels=1"
            # bit_rate is not applicable for PCM
            payload_output_format.pop("bit_rate", None)
        elif output_format_container == "wav":
            if output_bit_rate:  # Wav doesn't typically have a bitrate like mp3
                pass  # Cartesia might ignore it or use it for internal compression
            if not output_sample_rate:
                payload_output_format["sample_rate"] = 44100

        payload = {
            "model_id": model_id or "sonic-2",
            "transcript": transcript,
            "voice": {
                "mode": "id",
                "id": voice_id,
            },
            "output_format": payload_output_format,
        }
        if language:
            payload["language"] = language

        try:
            with httpx.Client(timeout=TTS_TIMEOUT) as client:
                response = client.post(url, json=payload, headers=self.headers)
            audio_bytes = self._handle_audio_response(response)
            return audio_bytes, content_type
        except httpx.RequestError as e:
            raise CartesiaAPIError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Request to Cartesia TTS failed: {e}",
            )
