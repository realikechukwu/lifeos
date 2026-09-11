"""Small, dependency-free Telegram Bot API client.

The bot token is kept in Django settings and is never logged. Errors raised by
this module deliberately omit the request URL because Telegram embeds the
token in that URL.
"""

import json
from pathlib import PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings


class TelegramAPIError(RuntimeError):
    pass


class TelegramFileTooLargeError(TelegramAPIError):
    pass


class TelegramBot:
    def __init__(self, token: str | None = None):
        self.token = token or settings.TELEGRAM_BOT_TOKEN
        if not self.token:
            raise TelegramAPIError("Telegram bot token is not configured.")

    def call(self, method: str, payload: dict | None = None) -> dict:
        body = json.dumps(payload or {}).encode("utf-8")
        request = Request(
            f"https://api.telegram.org/bot{self.token}/{method}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=20) as response:  # noqa: S310 - fixed Telegram API host
                result = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
            # Do not chain urllib errors: their representation can contain the
            # token-bearing request URL.
            raise TelegramAPIError(f"Telegram API request failed for {method}.") from None
        if not result.get("ok"):
            description = str(result.get("description", "unknown Telegram API error"))
            description = description.replace(self.token, "[redacted]")[:500]
            raise TelegramAPIError(f"Telegram API rejected {method}: {description}")
        return result.get("result", {})

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict | None = None,
        reply_to_message_id: int | None = None,
    ) -> dict:
        payload = {
            "chat_id": chat_id,
            "text": text[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        if reply_to_message_id:
            payload["reply_parameters"] = {"message_id": reply_to_message_id}
        return self.call("sendMessage", payload)

    def edit_message(
        self, chat_id: int, message_id: int, text: str, *, reply_markup: dict | None = None
    ) -> dict:
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": reply_markup or {"inline_keyboard": []},
        }
        return self.call("editMessageText", payload)

    def answer_callback(self, callback_query_id: str, text: str = "") -> dict:
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text[:200]
        return self.call("answerCallbackQuery", payload)

    def set_webhook(self, url: str, secret_token: str) -> dict:
        return self.call(
            "setWebhook",
            {
                "url": url,
                "secret_token": secret_token,
                "allowed_updates": ["message", "callback_query"],
                "drop_pending_updates": False,
            },
        )

    def set_commands(self, commands: list[dict]) -> dict:
        return self.call("setMyCommands", {"commands": commands})

    def get_webhook_info(self) -> dict:
        return self.call("getWebhookInfo")

    def get_file(self, file_id: str) -> dict:
        """Resolve a Telegram file id without exposing its download path."""
        result = self.call("getFile", {"file_id": file_id})
        if not isinstance(result, dict) or not isinstance(result.get("file_path"), str):
            raise TelegramAPIError("Telegram returned an unreadable file reference.")
        return result

    def download_file(self, file_path: str, *, max_bytes: int) -> bytes:
        """Download a Telegram file with a timeout and a strict byte limit."""
        if not file_path or max_bytes < 1:
            raise TelegramAPIError("Telegram file download parameters are invalid.")
        request = Request(
            f"https://api.telegram.org/file/bot{self.token}/{file_path}",
            method="GET",
        )
        try:
            with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed Telegram API host
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > max_bytes:
                    raise TelegramFileTooLargeError("Telegram file exceeds the download limit.")
                chunks = []
                downloaded = 0
                while True:
                    chunk = response.read(min(64 * 1024, max_bytes - downloaded + 1))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    downloaded += len(chunk)
                    if downloaded > max_bytes:
                        raise TelegramFileTooLargeError("Telegram file exceeds the download limit.")
                return b"".join(chunks)
        except TelegramFileTooLargeError:
            raise
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            # Never expose or chain the download URL because it embeds the bot token.
            raise TelegramAPIError("Telegram file download failed.") from None

    @staticmethod
    def safe_audio_filename(file_path: str) -> str:
        """Retain a harmless audio suffix for OpenAI's format detection."""
        suffix = PurePosixPath(file_path).suffix.lower()
        # Telegram commonly names OGG/Opus voice notes with .oga, while the
        # transcription API documents the equivalent container as .ogg.
        if suffix == ".oga":
            suffix = ".ogg"
        if not suffix or len(suffix) > 10 or not suffix[1:].isalnum():
            suffix = ".ogg"
        return f"telegram-voice{suffix}"
