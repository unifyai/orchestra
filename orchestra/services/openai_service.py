import json
import logging
from typing import Optional

from fastapi import HTTPException, status
from openai import OpenAI
from pydantic import BaseModel

from orchestra.settings import settings


class OpenAIAPIError(HTTPException):
    def __init__(self, status_code: int, detail: str):
        super().__init__(status_code=status_code, detail=detail)


class LanguageDetectionResponse(BaseModel):
    """Pydantic model for the expected structured output from OpenAI."""

    language_code: str


class VoiceDescriptionResponse(BaseModel):
    """Pydantic model for the voice description output from OpenAI."""

    voice_description: str


class OpenAIService:
    """
    Service for interacting with the OpenAI API.
    """

    def __init__(self):
        if not settings.openai_api_key:
            raise ValueError("openai_api_key is not set in settings.")
        self.client = OpenAI(api_key=settings.openai_api_key)

    def detect_language_from_text(self, text: str) -> Optional[str]:
        """
        Detects language from a string of text using OpenAI's structured output.
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
            response = self.client.beta.chat.completions.parse(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text},
                ],
                response_format=LanguageDetectionResponse,
            )
            response_content = response.choices[0].message.content
            if not response_content:
                logging.error(
                    "OpenAI returned an empty response for language detection.",
                )
                raise OpenAIAPIError(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="OpenAI returned an empty response for language detection.",
                )

            # Parse the JSON string and validate with Pydantic
            parsed_json = json.loads(response_content)
            validated_response = LanguageDetectionResponse(**parsed_json)
            language_code = validated_response.language_code

            if language_code in supported_languages:
                return language_code
            else:
                logging.warning(
                    f"Detected language '{language_code}' is not supported. Falling back to 'en'.",
                )
                return "en"

        except json.JSONDecodeError as e:
            logging.error(
                f"Failed to parse JSON from OpenAI response: {response_content}",
                exc_info=True,
            )
            raise OpenAIAPIError(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to parse language detection response from OpenAI.",
            ) from e
        except Exception as e:
            logging.error(
                f"An error occurred with OpenAI API request: {e}",
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
            response = self.client.beta.chat.completions.parse(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                response_format=VoiceDescriptionResponse,
            )
            response_content = response.choices[0].message.content
            if not response_content:
                logging.error(
                    "OpenAI returned an empty response for voice description generation.",
                )
                raise OpenAIAPIError(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="OpenAI returned an empty response for voice description generation.",
                )

            # Parse the JSON string and validate with Pydantic
            parsed_json = json.loads(response_content)
            validated_response = VoiceDescriptionResponse(**parsed_json)
            return validated_response.voice_description

        except json.JSONDecodeError as e:
            logging.error(
                f"Failed to parse JSON from OpenAI response: {response_content}",
                exc_info=True,
            )
            raise OpenAIAPIError(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to parse voice description response from OpenAI.",
            ) from e
        except Exception as e:
            logging.error(
                f"An error occurred with OpenAI API request for voice description: {e}",
                exc_info=True,
            )
            raise OpenAIAPIError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"An error occurred with the voice description generation service: {str(e)}",
            ) from e
