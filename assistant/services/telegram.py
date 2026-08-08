"""Small, dependency-free Telegram Bot API client.

The bot token is kept in Django settings and is never logged. Errors raised by
this module deliberately omit the request URL because Telegram embeds the
token in that URL.
"""

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings


class TelegramAPIError(RuntimeError):
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
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise TelegramAPIError(f"Telegram API request failed for {method}.") from exc
        if not result.get("ok"):
            description = str(result.get("description", "unknown Telegram API error"))[:500]
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
