import os
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from assistant.services.telegram_transcription import (
    TelegramTranscriptionError,
    transcribe_telegram_voice,
)


@override_settings(OPENAI_API_KEY="test-openai-key")
class TelegramTranscriptionTests(SimpleTestCase):
    @patch("assistant.services.telegram_transcription.OpenAI")
    def test_uses_fixed_transcription_model_and_removes_temporary_audio(self, openai):
        client = openai.return_value
        client.audio.transcriptions.create.return_value = SimpleNamespace(text="  hello   world  ")

        result = transcribe_telegram_voice(b"audio", filename="voice.ogg", content_type="audio/ogg")

        self.assertEqual(result, "hello world")
        kwargs = client.audio.transcriptions.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "gpt-4o-mini-transcribe")
        temporary_path = kwargs["file"].name
        self.assertTrue(temporary_path.endswith(".ogg"))
        self.assertFalse(os.path.exists(temporary_path))

    @patch("assistant.services.telegram_transcription.OpenAI")
    def test_rejects_empty_transcript_and_removes_temporary_audio(self, openai):
        client = openai.return_value
        client.audio.transcriptions.create.return_value = SimpleNamespace(text=" … ")

        with self.assertRaises(TelegramTranscriptionError):
            transcribe_telegram_voice(b"audio", filename="voice.ogg")

        temporary_path = client.audio.transcriptions.create.call_args.kwargs["file"].name
        self.assertFalse(os.path.exists(temporary_path))
