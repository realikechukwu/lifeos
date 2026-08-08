"""Send one private LifeOS briefing per opted-in Telegram user each day."""

from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import IntegrityError, transaction
from django.utils import timezone

from assistant.services.telegram import TelegramBot
from assistant.services.telegram_handlers import build_today_message, today_keyboard
from core.models import TelegramBriefingDelivery, TelegramChat, TelegramPreference, TelegramUser


def due_preferences(now=None):
    """Return enabled preferences matching the current app-local minute."""
    now = now or timezone.now()
    local_now = now.astimezone(ZoneInfo(settings.APP_TIMEZONE))
    return (
        TelegramPreference.objects.select_related("user")
        .filter(briefing_enabled=True, briefing_time__hour=local_now.hour, briefing_time__minute=local_now.minute),
        local_now.date(),
    )


def send_due_briefings(*, bot=None, now=None) -> tuple[int, int]:
    """Send due briefings at most once per user/date. Returns (sent, failed)."""
    bot = bot or TelegramBot()
    preferences, briefing_date = due_preferences(now)
    sent = failed = 0
    for preference in preferences:
        user = preference.user
        chat = TelegramChat.objects.filter(
            chat_id=user.user_id,
            chat_type=TelegramChat.ChatType.PRIVATE,
            authorised=True,
        ).first()
        if not chat:
            # A user receives private briefings only after starting the bot;
            # no delivery record is made so it can be sent once they do.
            continue
        try:
            with transaction.atomic():
                delivery = TelegramBriefingDelivery.objects.create(user=user, briefing_date=briefing_date)
        except IntegrityError:
            continue
        try:
            bot.send_message(chat.chat_id, build_today_message(), reply_markup=today_keyboard())
        except Exception:  # noqa: BLE001 - retry is safe because claim is removed
            delivery.delete()
            failed += 1
        else:
            sent += 1
    return sent, failed


class Command(BaseCommand):
    help = "Send due private Telegram LifeOS briefings (once per user/day)."

    def handle(self, *args, **options):
        sent, failed = send_due_briefings()
        self.stdout.write(f"Sent {sent} Telegram briefing(s). {failed} failed.")
