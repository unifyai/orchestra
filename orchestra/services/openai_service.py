import logging
from typing import Optional, Type, TypeVar

import httpx
from fastapi import HTTPException, status
from pydantic import BaseModel

from orchestra.settings import settings

_ResponseModelT = TypeVar("_ResponseModelT", bound=BaseModel)


def _strict_json_schema(schema: dict) -> dict:
    if schema.get("type") == "object":
        schema["additionalProperties"] = False
        for subschema in schema.get("properties", {}).values():
            if isinstance(subschema, dict):
                _strict_json_schema(subschema)
    for item in schema.get("anyOf", []):
        if isinstance(item, dict):
            _strict_json_schema(item)
    for item in schema.get("oneOf", []):
        if isinstance(item, dict):
            _strict_json_schema(item)
    items = schema.get("items")
    if isinstance(items, dict):
        _strict_json_schema(items)
    return schema


class OpenAIAPIError(HTTPException):
    def __init__(self, status_code: int, detail: str):
        super().__init__(status_code=status_code, detail=detail)


class LanguageDetectionResponse(BaseModel):
    """Pydantic model for the expected structured output from OpenAI."""

    language_code: str


class VoiceDescriptionResponse(BaseModel):
    """Pydantic model for the voice description output from OpenAI."""

    voice_description: str


class ImageAnalysisResponse(BaseModel):
    """Pydantic model for image analysis output from OpenAI."""

    has_human_face: bool
    is_nsfw: bool
    reason: str


class TextModerationResult(BaseModel):
    """Pydantic model for simple text moderation output."""

    is_nsfw: bool
    reason: str


class OpenAIService:
    """
    Service for OpenAI-compatible model tasks routed through OpenRouter.
    """

    def __init__(self):
        if not settings.openrouter_api_key:
            raise ValueError(
                "openrouter_api_key is not set in settings.",
            )
        transport = httpx.HTTPTransport(retries=3)
        self._http_client = httpx.Client(
            base_url=settings.openrouter_api_base,
            headers={
                "Authorization": f"Bearer {settings.openrouter_api_key}",
                "Content-Type": "application/json",
            },
            transport=transport,
            timeout=httpx.Timeout(30.0),
        )

    @staticmethod
    def _chat_model(model: str) -> str:
        if not model.startswith("openai/"):
            return f"openai/{model}"
        return model

    def _parse_chat_completion(
        self,
        *,
        model: str,
        messages: list[dict],
        response_model: Type[_ResponseModelT],
    ) -> _ResponseModelT:
        schema = _strict_json_schema(response_model.model_json_schema())
        payload = {
            "model": self._chat_model(model),
            "messages": messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": response_model.__name__,
                    "strict": True,
                    "schema": schema,
                },
            },
            "provider": {"require_parameters": True},
        }
        response = self._http_client.post("/chat/completions", json=payload)
        response.raise_for_status()
        body = response.json()
        response_content = body["choices"][0]["message"].get("content")
        if not response_content:
            raise OpenAIAPIError(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Model provider returned an empty response.",
            )
        return response_model.model_validate_json(response_content)

    def analyze_image(self, image_url: str) -> ImageAnalysisResponse:
        """
        Analyzes an image to check for a human face and NSFW content.
        """
        system_prompt = """
        You are an image analysis expert for a content moderation pipeline.
        Analyze the image provided by the user and determine two things:
        1. Does the image contain a person with a visible human face?
        2. Is the image Not Safe For Work (NSFW)? This includes explicit nudity, violence, hate symbols, or other offensive content.
        Respond with a JSON object containing three keys:
        - 'has_human_face': boolean (true if a human face is clearly visible, false otherwise).
        - 'is_nsfw': boolean (true if the content is NSFW, false otherwise).
        - 'reason': string (a brief explanation for your decision, e.g., "Image is a landscape with no people," or "Image contains explicit content.").
        """
        try:
            return self._parse_chat_completion(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Please analyze this image."},
                            {
                                "type": "image_url",
                                "image_url": {"url": image_url},
                            },
                        ],
                    },
                ],
                response_model=ImageAnalysisResponse,
            )
        except Exception as e:
            logging.error(
                f"An error occurred with image analysis: {e}",
                exc_info=True,
            )
            raise OpenAIAPIError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"An error occurred with the image analysis service: {str(e)}",
            ) from e

    def moderate_text(self, text: str) -> TextModerationResult:
        """
        Analyzes a string of text for NSFW content.
        """
        system_prompt = """
        You are a text analysis expert for a content moderation pipeline.
        Analyze the text provided by the user and determine if it is Not Safe For Work (NSFW).
        NSFW content includes explicit language, hate speech, threats, or other highly offensive material.
        Respond with a JSON object containing two keys:
        - 'is_nsfw': boolean (true if the content is NSFW, false otherwise).
        - 'reason': string (a brief explanation for your decision, e.g., "Text is clean," or "Text contains explicit language.").
        """
        try:
            return self._parse_chat_completion(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text},
                ],
                response_model=TextModerationResult,
            )
        except Exception as e:
            logging.error(
                f"An error occurred with text moderation: {e}",
                exc_info=True,
            )
            raise OpenAIAPIError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"An error occurred with the text moderation service: {str(e)}",
            ) from e

    def detect_language_from_text(self, text: str) -> Optional[str]:
        """
        Detects language from a string of text using structured output.
        - If detection is successful and the language is supported, returns the language code.
        - If detection is successful but the language is NOT supported, returns 'en' as a fallback.
        - If the API call or JSON parsing fails, it raises an OpenAIAPIError.
        """

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

        system_prompt = """
        You are a language detection expert.
        Analyze the text provided by the user and identify its primary language.
        Respond with a JSON object containing a single key, 'language_code',
        which holds the two-letter ISO 639-1 code for the detected language.
        For example, if the text is in English, respond with: {"language_code": "en"}
        """
        try:
            validated_response = self._parse_chat_completion(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text},
                ],
                response_model=LanguageDetectionResponse,
            )
            language_code = validated_response.language_code

            if language_code in supported_languages:
                return language_code
            else:
                logging.warning(
                    f"Detected language '{language_code}' is not supported. Falling back to 'en'.",
                )
                return "en"

        except Exception as e:
            logging.error(
                f"An error occurred with language detection request: {e}",
                exc_info=True,
            )
            raise OpenAIAPIError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"An error occurred with the language detection service: {str(e)}",
            ) from e

    def generate_voice_description_from_bio(
        self,
        bio: str,
        description_hint: Optional[str] = None,
    ) -> str:
        """
        Generates a detailed voice description for a TTS model based on a character bio and an optional hint.
        """
        system_prompt = """
        You are an expert in creating voice prompts for Text-to-Speech (TTS) models like ElevenLabs.
        Your task is to generate a concise, descriptive voice prompt based on the provided biography and an optional description hint.
        The voice prompt should describe the voice's characteristics, such as accent, tone, age, and style.
        The final description MUST be between 20 and 1000 characters long.
        Focus on creating a description that a TTS model can interpret to generate a specific voice.
        Respond with a JSON object containing a single key, 'voice_description'.
        """

        user_content = f"Character Biography:\n---\n{bio}\n---\n"
        if description_hint:
            user_content += (
                f"\nAdditional Voice Description Hint:\n---\n{description_hint}\n---"
            )

        try:
            validated_response = self._parse_chat_completion(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                response_model=VoiceDescriptionResponse,
            )
            return validated_response.voice_description

        except Exception as e:
            logging.error(
                f"An error occurred with voice description request: {e}",
                exc_info=True,
            )
            raise OpenAIAPIError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"An error occurred with the voice description generation service: {str(e)}",
            ) from e
