import io
import json
import logging
import mimetypes
from typing import Optional, Tuple

import httpx
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


class ImageAnalysisResponse(BaseModel):
    """Pydantic model for image analysis output from OpenAI."""

    has_human_face: bool
    is_nsfw: bool
    reason: str


class TextModerationResponse(BaseModel):
    """Pydantic model for text moderation output from OpenAI."""

    contains_speech: bool
    is_nsfw: bool
    reason: str


class TextModerationResult(BaseModel):
    """Pydantic model for simple text moderation output."""

    is_nsfw: bool
    reason: str


class OpenAIService:
    """
    Service for interacting with the OpenAI API.
    """

    def __init__(self):
        if not settings.openai_api_key:
            raise ValueError("openai_api_key is not set in settings.")
        self.client = OpenAI(api_key=settings.openai_api_key)

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
            response = self.client.beta.chat.completions.parse(
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
                response_format=ImageAnalysisResponse,
            )
            response_content = response.choices[0].message.content
            if not response_content:
                logging.error("OpenAI returned an empty response for image analysis.")
                # Return a safe default to block if unsure
                return ImageAnalysisResponse(
                    has_human_face=False,
                    is_nsfw=True,
                    reason="OpenAI returned an empty response.",
                )

            parsed_json = json.loads(response_content)
            return ImageAnalysisResponse(**parsed_json)
        except Exception as e:
            logging.error(
                f"An error occurred with OpenAI image analysis: {e}",
                exc_info=True,
            )
            raise OpenAIAPIError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"An error occurred with the image analysis service: {str(e)}",
            ) from e

    def analyze_audio(self, audio_url: str) -> TextModerationResponse:
        """
        Analyzes audio for speech and NSFW content by transcribing it and then moderating the text.
        """
        # 1. Download audio
        try:
            with httpx.Client() as client:
                response = client.get(audio_url)
                response.raise_for_status()
                audio_bytes = response.content
                content_type = response.headers.get(
                    "content-type",
                    "application/octet-stream",
                )

                # Guess extension from MIME type to satisfy OpenAI API's format check.
                extension = mimetypes.guess_extension(content_type)
                if not extension:
                    # Fallback for common audio types if guess fails
                    if "wav" in content_type:
                        extension = ".wav"
                    elif "mpeg" in content_type:
                        extension = ".mp3"
                    elif "mp4" in content_type:
                        extension = ".m4a"
                    else:
                        extension = ".tmp"  # Fallback that may fail

                filename = f"audio{extension}"
                audio_file = (filename, io.BytesIO(audio_bytes))

        except httpx.RequestError as e:
            logging.error(f"Failed to download audio from {audio_url}: {e}")
            raise OpenAIAPIError(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to download audio for moderation: {str(e)}",
            )

        # 2. Transcribe audio
        try:
            transcription = self.client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
            )
            transcript_text = transcription.text
        except Exception as e:
            # If transcription itself fails, it's a service error, not a "no speech" case.
            logging.error(
                f"OpenAI Whisper failed to transcribe audio: {e}",
                exc_info=True,
            )
            raise OpenAIAPIError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Failed to transcribe audio for moderation: {str(e)}",
            )

        # 3. Moderate the transcript for speech presence and NSFW content
        system_prompt = """
        You are a content moderation expert. Analyze the provided text, which is a transcription of an audio file.
        Your task is to determine two things:
        1. Does the text contain intelligible human speech, or is it just background noise, music, or nonsensical sounds transcribed into text?
        2. Is the content of the speech Not Safe For Work (NSFW), including explicit language, hate speech, or threats?

        Respond with a JSON object with three keys:
        - 'contains_speech': boolean (true if intelligible speech is present, false otherwise).
        - 'is_nsfw': boolean (true if the speech is NSFW, false if it's clean or if there is no speech).
        - 'reason': string (a brief explanation, e.g., "The audio contains clear speech about a neutral topic.", "No intelligible speech was detected, only music.", or "The speech contains explicit language.").
        """

        try:
            response = self.client.beta.chat.completions.parse(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": transcript_text or "(empty transcript)",
                    },
                ],
                response_format=TextModerationResponse,
            )
            response_content = response.choices[0].message.content
            if not response_content:
                logging.error("OpenAI returned an empty response for audio analysis.")
                # Safe default: block if unsure
                return TextModerationResponse(
                    contains_speech=False,
                    is_nsfw=True,
                    reason="Moderation service returned an empty response.",
                )

            parsed_json = json.loads(response_content)
            return TextModerationResponse(**parsed_json)
        except Exception as e:
            logging.error(
                f"An error occurred with OpenAI audio moderation: {e}",
                exc_info=True,
            )
            raise OpenAIAPIError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"An error occurred with the audio moderation service: {str(e)}",
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
            response = self.client.beta.chat.completions.parse(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text},
                ],
                response_format=TextModerationResult,
            )
            response_content = response.choices[0].message.content
            if not response_content:
                logging.error("OpenAI returned an empty response for text moderation.")
                # Safe default: block if unsure
                return TextModerationResult(
                    is_nsfw=True,
                    reason="OpenAI returned an empty response during moderation.",
                )

            parsed_json = json.loads(response_content)
            return TextModerationResult(**parsed_json)
        except Exception as e:
            logging.error(
                f"An error occurred with OpenAI text moderation: {e}",
                exc_info=True,
            )
            raise OpenAIAPIError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"An error occurred with the text moderation service: {str(e)}",
            ) from e

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

    def generate_speech(
        self,
        text: str,
        voice_id: str,
        model_id: Optional[str] = "gpt-4o-mini-tts",
        output_format: str = "mp3",
    ) -> Tuple[bytes, str]:
        """
        Generates speech from text using OpenAI API and returns raw audio bytes and content type.
        """

        allowed_voices = ["marin", "cedar", "alloy", "ash", "shimmer", "coral"]
        if voice_id not in allowed_voices:
            raise OpenAIAPIError(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported OpenAI voice '{voice_id}'. Supported voices are: {allowed_voices}",
            )

        # Map our common format to OpenAI's `response_format`
        supported_formats = {
            "mp3": ("mp3", "audio/mpeg"),
            "flac": ("flac", "audio/flac"),
        }

        if output_format not in supported_formats:
            raise OpenAIAPIError(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported output format '{output_format}' for OpenAI. Supported formats are: {list(supported_formats.keys())}.",
            )

        openai_format, content_type = supported_formats[output_format]

        try:
            response = self.client.audio.speech.create(
                model=model_id or "gpt-4o-mini-tts",
                voice=voice_id,
                input=text,
                response_format=openai_format,
            )
            audio_bytes = response.read()
            return audio_bytes, content_type
        except Exception as e:
            logging.error(
                f"An error occurred with OpenAI speech generation: {e}",
                exc_info=True,
            )
            # This will catch API errors from OpenAI client as well.
            raise OpenAIAPIError(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"An error occurred with the speech generation service: {str(e)}",
            ) from e
