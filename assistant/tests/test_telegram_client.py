from io import BytesIO
from unittest.mock import patch
from urllib.error import URLError

from django.test import SimpleTestCase

from assistant.services.telegram import TelegramAPIError, TelegramBot, TelegramFileTooLargeError


class FakeResponse(BytesIO):
    def __init__(self, value: bytes, *, content_length: int | None = None):
        super().__init__(value)
        self.read_calls = 0
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def __enter__(self):
        return self

    def read(self, size=-1):
        self.read_calls += 1
        return super().read(size)

    def __exit__(self, *args):
        self.close()


class TelegramClientDownloadTests(SimpleTestCase):
    @patch("assistant.services.telegram.urlopen")
    def test_download_is_bounded(self, urlopen):
        urlopen.return_value = FakeResponse(b"abcdef")

        with self.assertRaises(TelegramFileTooLargeError):
            TelegramBot("secret-token").download_file("voice/file.ogg", max_bytes=5)

    @patch("assistant.services.telegram.urlopen")
    def test_content_length_is_rejected_before_read(self, urlopen):
        response = FakeResponse(b"", content_length=6)
        urlopen.return_value = response

        with self.assertRaises(TelegramFileTooLargeError):
            TelegramBot("secret-token").download_file("voice/file.ogg", max_bytes=5)

        self.assertEqual(response.read_calls, 0)

    def test_audio_filename_preserves_safe_extension(self):
        self.assertEqual(TelegramBot.safe_audio_filename("voice/file_1.oga"), "telegram-voice.oga")
        self.assertEqual(TelegramBot.safe_audio_filename("voice/file"), "telegram-voice.ogg")

    @patch("assistant.services.telegram.urlopen")
    def test_download_error_does_not_expose_or_chain_token_url(self, urlopen):
        urlopen.side_effect = URLError(
            "https://api.telegram.org/file/botsecret-token/voice/file.ogg"
        )

        with self.assertRaises(TelegramAPIError) as raised:
            TelegramBot("secret-token").download_file("voice/file.ogg", max_bytes=5)

        self.assertNotIn("secret-token", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
