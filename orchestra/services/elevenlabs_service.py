import io
from typing import Any, Dict, Optional, Tuple

import httpx
from fastapi import HTTPException, status

from orchestra.settings import settings


class ElevenLabsAPIError(HTTPException):
    def __init__(self, status_code: int, detail: str):
        super().__init__(status_code=status_code, detail=detail)


LONG_OPERATION_TIMEOUT = httpx.Timeout(60.0)  # 60 seconds
TTS_TIMEOUT = httpx.Timeout(30.0)  # Timeout for TTS requests


class ElevenLabsService:
    """
    Service for interacting with the ElevenLabs API.
    """

    def __init__(self):
        self.v1_base_url = "https://api.elevenlabs.io/v1"
        self.v2_base_url = "https://api.elevenlabs.io/v2"
        if not settings.elevenlabs_api_key:
            raise ValueError("elevenlabs_api_key is not set in settings.")
        self.headers = {
            "xi-api-key": settings.elevenlabs_api_key,
            "Content-Type": "application/json",
        }

    def _handle_audio_response(self, response: httpx.Response) -> bytes:
        if not (200 <= response.status_code < 300):
            try:
                # Attempt to parse error detail if JSON
                error_data = response.json()
                error_detail = error_data.get("detail", {}).get(
                    "message",
                    response.text,
                )
            except httpx.JSONDecodeError:
                error_detail = response.text  # Raw error text
            raise ElevenLabsAPIError(
                status_code=response.status_code,
                detail=f"ElevenLabs API audio generation failed: {error_detail}",
            )
        return response.content

    def _handle_response(
        self,
        response: httpx.Response,
    ) -> Dict[str, Any]:  # For JSON responses
        # For DELETE, ElevenLabs returns 200 OK with JSON {"status": "ok"}
        # or error JSON. No 204.
        try:
            response_data = response.json()
        except httpx.JSONDecodeError:
            raise ElevenLabsAPIError(
                status_code=response.status_code,
                detail=f"ElevenLabs API returned non-JSON response: {response.text}",
            )

        if not (200 <= response.status_code < 300):
            error_detail = response_data.get(
                "detail",  # For some errors
                response_data.get(
                    "message",
                    "Unknown ElevenLabs API error",
                ),  # For others
            )
            if (
                isinstance(error_detail, dict) and "message" in error_detail
            ):  # Nested detail
                error_detail = error_detail["message"]
            raise ElevenLabsAPIError(
                status_code=response.status_code,
                detail=str(error_detail),
            )
        return response_data

    async def clone_voice(
        self,
        file_content: bytes,
        file_name: str,
        name: str,
        description: Optional[str] = None,
        remove_background_noise: bool = False,
    ) -> Dict[str, Any]:
        """
        Clones a voice using ElevenLabs API.
        Reference: https://elevenlabs.io/docs/api-reference/voices/ivc/create
        """
        url = f"{self.v1_base_url}/voices/add"
        files = {
            "files": (file_name, io.BytesIO(file_content), "audio/mpeg"),
        }
        data = {
            "name": name,
            "remove_background_noise": str(remove_background_noise).lower(),
        }
        if description:
            data["description"] = description

        # For multipart/form-data, httpx sets Content-Type. Do not set it in v2_headers.
        request_headers = self.headers.copy()
        request_headers.pop("Content-Type", None)

        try:
            async with httpx.AsyncClient(timeout=LONG_OPERATION_TIMEOUT) as client:
                response = await client.post(
                    url,
                    data=data,
                    files=files,
                    headers=request_headers,
                )
            return self._handle_response(response)
        except httpx.RequestError as e:
            raise ElevenLabsAPIError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Request to ElevenLabs failed: {e}",
            )

    async def delete_voice(self, voice_id: str) -> Dict[str, Any]:
        """
        Deletes a voice from ElevenLabs.
        Reference: https://elevenlabs.io/docs/api-reference/voices/delete
        """
        url = f"{self.v1_base_url}/voices/{voice_id}"
        try:
            async with httpx.AsyncClient() as client:
                response = await client.delete(url, headers=self.headers)
            return self._handle_response(response)
        except httpx.RequestError as e:
            raise ElevenLabsAPIError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Request to ElevenLabs failed: {e}",
            )

    async def list_voices(self) -> Dict[str, Any]:
        """
        List all available voices from ElevenLabs.
        Reference: https://elevenlabs.io/docs/api-reference/voices/search
        """
        url = f"{self.v2_base_url}/voices"
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, headers=self.headers)
            return self._handle_response(response)
        except httpx.RequestError as e:
            raise ElevenLabsAPIError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Request to ElevenLabs failed: {e}",
            )

    async def get_voice(self, voice_id: str) -> Dict[str, Any]:
        """
        Get details of a specific voice from ElevenLabs.
        Reference: https://elevenlabs.io/docs/api-reference/voices/get
        """
        url = f"{self.v1_base_url}/voices/{voice_id}"
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, headers=self.headers)
            return self._handle_response(response)
        except httpx.RequestError as e:
            raise ElevenLabsAPIError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Request to ElevenLabs failed: {e}",
            )

    def _map_common_format_to_elevenlabs_output(
        self,
        common_format: str,
        # Potentially add sample_rate, bitrate preferences here if needed for more granular mapping
    ) -> str:
        """Helper to map common format names to ElevenLabs output_format strings."""
        # This is a simplified mapping. For full control,
        # allow user to pass the `elevenlabs_explicit_output_format` string.
        mapping = {
            "mp3": "mp3_44100_128",  # Default MP3
            "wav": "pcm_44100",  # High-quality PCM as WAV
            "flac": "flac_22050_opus",  # EL supports flac via this opus encoded flac. Client must decode.
            # Or map to a PCM if direct FLAC is an issue.
            # For simplicity, might be better to map FLAC to high quality PCM for now
            # or state FLAC not directly supported via this mapping.
            # Let's map to pcm_44100 as well for broader compatibility.
            "pcm_s16le": "pcm_24000",  # Example, can be pcm_16000, pcm_22050, pcm_44100
            "pcm_mulaw": "ulaw_8000",
        }
        return mapping.get(common_format, "mp3_44100_128")  # Default to MP3

    def _get_content_type_for_elevenlabs_format(self, el_format_str: str) -> str:
        if el_format_str.startswith("mp3_"):
            return "audio/mpeg"
        if el_format_str.startswith("pcm_"):
            # e.g. pcm_16000 -> audio/L16; rate=16000; channels=1
            # This is a simplification; actual content type for PCM can be more specific.
            # ElevenLabs API docs suggest they return 'audio/mpeg' for mp3, 'audio/wav' for wav-like pcm.
            # For simplicity, if they give pcm, we might return audio/wav or audio/L16
            # based on how clients are expected to handle it.
            # Let's assume audio/wav for PCM from EL for now if not ulaw.
            return "audio/wav"
        if el_format_str.startswith("ulaw_"):
            return "audio/mulaw"  # Potentially with ;rate=8000
        # Add other mappings if EL supports more raw types directly (e.g. "audio/flac")
        return "application/octet-stream"  # Fallback

    async def generate_speech(
        self,
        text: str,
        voice_id: str,  # This is ElevenLabs' voice_id
        model_id: Optional[str] = "eleven_multilingual_v2",
        output_format: str = "mp3",  # mp3, wav, pcm_s16le, pcm_mulaw, flac
        optimize_streaming_latency: Optional[int] = None,
        stability: Optional[float] = None,
        similarity_boost: Optional[float] = None,
    ) -> Tuple[bytes, str]:
        """
        Generates speech from text using ElevenLabs API and returns raw audio bytes and content type.
        Reference: https://elevenlabs.io/docs/api-reference/text-to-speech/convert
        """
        elevenlabs_output_identifier = self._map_common_format_to_elevenlabs_output(
            output_format,
        )

        url = f"{self.v1_base_url}/text-to-speech/{voice_id}"

        params: Dict[str, Any] = {"output_format": elevenlabs_output_identifier}
        if optimize_streaming_latency is not None:
            params["optimize_streaming_latency"] = optimize_streaming_latency

        payload: Dict[str, Any] = {"text": text}
        if model_id:
            payload["model_id"] = model_id

        voice_settings: Dict[str, float] = {}
        if stability is not None:
            voice_settings["stability"] = stability
        if similarity_boost is not None:
            voice_settings["similarity_boost"] = similarity_boost
        if voice_settings:
            payload["voice_settings"] = voice_settings

        determined_content_type = self._get_content_type_for_elevenlabs_format(
            elevenlabs_output_identifier,
        )

        try:
            async with httpx.AsyncClient(timeout=TTS_TIMEOUT) as client:
                response = await client.post(
                    url,
                    json=payload,
                    headers=self.headers,
                    params=params,
                )
            audio_bytes = self._handle_audio_response(response)
            return audio_bytes, determined_content_type
        except httpx.RequestError as e:
            raise ElevenLabsAPIError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Request to ElevenLabs TTS failed: {e}",
            )

    async def design_voice_generate_previews(
        self,
        voice_description: str,  # Main description
        text_for_preview: Optional[str] = None,
        auto_generate_text_flag: Optional[bool] = None,
        model_id_for_design: Optional[str] = None,
        # Add other optional parameters here if you added them to schema
    ) -> Dict[str, Any]:
        """
        Generates voice design previews from a text description using ElevenLabs.
        Uses POST /v1/text-to-voice/design
        """
        url = f"{self.v1_base_url}/text-to-voice/design"
        payload: Dict[str, Any] = {"voice_description": voice_description}

        if text_for_preview is not None:
            payload["text"] = text_for_preview
        if auto_generate_text_flag is not None:
            payload["auto_generate_text"] = auto_generate_text_flag
        if model_id_for_design is not None:
            payload["model_id"] = model_id_for_design
        # Add other optional params to payload if defined

        # Headers (assuming self.headers includes xi-api-key but not Content-Type)
        request_headers = self.headers.copy()
        request_headers["Content-Type"] = "application/json"

        try:
            async with httpx.AsyncClient(timeout=LONG_OPERATION_TIMEOUT) as client:
                response = await client.post(url, json=payload, headers=request_headers)
            return self._handle_response(response)
        except httpx.RequestError as e:
            raise ElevenLabsAPIError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Request to ElevenLabs /text-to-voice/design failed: {e}",
            )

    async def create_voice_from_generated_id(
        self,
        voice_name: str,
        generated_voice_id: str,
        description: str,
        labels: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        Creates a full voice from a generated_voice_id obtained from the design preview step.
        Reference: https://elevenlabs.io/docs/api-reference/text-to-voice/create
        """
        url = f"{self.v1_base_url}/text-to-voice"  # This is what user example implies for creating from generated_id
        payload: Dict[str, Any] = {
            "voice_name": voice_name,
            "generated_voice_id": generated_voice_id,
            "voice_description": description,
        }
        if labels:
            payload["labels"] = labels

        try:
            async with httpx.AsyncClient(timeout=LONG_OPERATION_TIMEOUT) as client:
                response = await client.post(url, json=payload, headers=self.headers)
            return self._handle_response(response)
        except httpx.RequestError as e:
            raise ElevenLabsAPIError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Request to ElevenLabs /text-to-voice (for creation) failed: {e}",
            )
