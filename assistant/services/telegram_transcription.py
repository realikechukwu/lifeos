"""Ephemeral speech-to-text support for Telegram voice notes."""

import os
import tempfile
from pathlib import PurePath

from django.conf import settings
from openai import OpenAI


# Keep this fixed so voice input does not acquire a second configurable model path.
TRANSCRIPTION_MODEL = "gpt-4o-mini-transcribe"


class TelegramTranscriptionError(RuntimeError):
    """A recoverable voice transcription failure safe to show generically."""


def transcribe_telegram_voice(
    audio_bytes: bytes, *, filename: str = "telegram-voice.ogg", content_type: str = "audio/ogg"
) -> str:
    """Transcribe audio through a short-lived local file and remove it in all cases."""
    del content_type  # Kept explicit at the boundary for safe format metadata from callers.
    if not isinstance(audio_bytes, bytes) or not audio_bytes:
        raise TelegramTranscriptionError("Voice audio was empty.")

    suffix = PurePath(filename).suffix.lower()
    if not suffix or len(suffix) > 10 or not suffix[1:].isalnum():
        suffix = ".ogg"

    temporary_path = ""
    try:
        with tempfile.NamedTemporaryFile(prefix="lifeos-voice-", suffix=suffix, delete=False) as temp:
            temporary_path = temp.name
            temp.write(audio_bytes)

        client = OpenAI(api_key=settings.OPENAI_API_KEY)
        with open(temporary_path, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(
                model=TRANSCRIPTION_MODEL,
                file=audio_file,
            )
        text = " ".join(str(getattr(transcript, "text", "")).split())
        if not text or not any(character.isalnum() for character in text):
            raise TelegramTranscriptionError("Transcription did not contain usable text.")
        return text
    except TelegramTranscriptionError:
        raise
    except Exception:
        # SDK and file failures are deliberately collapsed so audio or secrets
        # cannot leak through exception text into webhook logs or database fields.
        raise TelegramTranscriptionError("Voice transcription failed.") from None
    finally:
        if temporary_path:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
