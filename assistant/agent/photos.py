"""Photos sent to the bot on Telegram: letters, appointment cards, receipts.

The image reaches Claude as a native image block in the conversation, the same
way an uploaded PDF does; nothing else is stored.
"""
import base64

from django.db import IntegrityError, transaction

from core.models import AgentJob, AgentThread, AuditLog
from assistant.services.telegram import TelegramFileTooLargeError

# The Messages API accepts images up to 5 MB each.
MAX_PHOTO_BYTES = 5 * 1024 * 1024
IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
DEFAULT_CAPTION = "I've sent a photo."
TOO_LARGE = "That image is over 5 MB. Send it as a photo instead of a file."


def is_image_document(message: dict) -> bool:
    return (message.get("document") or {}).get("mime_type") in IMAGE_MIMES


def _photo_file(message: dict) -> tuple[str, str] | None:
    """The Telegram file id and media type to download, or None if too large."""
    if message.get("photo"):
        # Sizes come smallest first; Telegram re-encodes every photo as JPEG.
        sizes = [s for s in message["photo"] if s.get("file_size", 0) <= MAX_PHOTO_BYTES]
        return (sizes[-1]["file_id"], "image/jpeg") if sizes else None
    document = message["document"]
    if document.get("file_size", 0) > MAX_PHOTO_BYTES:
        return None
    return document["file_id"], document["mime_type"]


def handle_telegram_photo(message: dict, chat, user, update_id: int, bot, *, caption: str) -> None:
    if AgentJob.objects.filter(telegram_update_id=update_id).exists():
        return
    found = _photo_file(message)
    if found is None:
        bot.send_message(chat.chat_id, TOO_LARGE)
        return
    file_id, mime = found
    remote = bot.get_file(file_id)
    try:
        data = bot.download_file(remote["file_path"], max_bytes=MAX_PHOTO_BYTES)
    except TelegramFileTooLargeError:
        bot.send_message(chat.chat_id, TOO_LARGE)
        return
    text = (caption or DEFAULT_CAPTION)[:8000]
    try:
        with transaction.atomic():
            job = AgentJob.objects.create(
                channel=AgentThread.Channel.TELEGRAM, telegram_chat=chat, telegram_user=user,
                telegram_update_id=update_id, telegram_message_id=message.get("message_id"), text=text,
                input_blocks=[
                    {"type": "text", "text": text},
                    {"type": "text", "text": "Untrusted photo sent by the speaker:"},
                    {"type": "image", "source": {
                        "type": "base64", "media_type": mime, "data": base64.b64encode(data).decode("ascii"),
                    }},
                ],
            )
            AuditLog.objects.create(action="agent_photo_received", object_type="AgentJob",
                                    object_id=str(job.pk), details={"role": user.role})
    except IntegrityError:
        # Telegram redelivered an update that is already queued.
        pass
