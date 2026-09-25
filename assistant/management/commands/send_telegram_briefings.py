"""Send due daily, weekly, and monthly private LifeOS Telegram briefings."""

from dataclasses import dataclass
from datetime import date, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import IntegrityError, transaction
from django.utils import timezone

from assistant.services.telegram import TelegramBot
from assistant.services.telegram_handlers import (
    build_planning_message,
    build_today_message,
    today_keyboard,
)
from assistant.services.telegram_inbox import one_month_after
from core.models import TelegramBriefingDelivery, TelegramChat, TelegramPreference


WEEKLY_BRIEFING_TIME = time(18, 0)
MONTHLY_BRIEFING_TIME = time(9, 30)


@dataclass(frozen=True)
class DueBriefing:
    briefing_type: str
    delivery_date: date
    start_date: date | None = None
    end_date: date | None = None


def _same_minute(left: time, right: time) -> bool:
    return left.hour == right.hour and left.minute == right.minute


def due_briefings(preference: TelegramPreference, local_now) -> list[DueBriefing]:
    """Return every briefing type due for a preference in this local minute."""
    local_date = local_now.date()
    local_time = local_now.time()
    due = []
    if _same_minute(preference.briefing_time, local_time):
        due.append(DueBriefing(TelegramBriefingDelivery.BriefingType.DAILY, local_date))
    if local_date.weekday() == 6 and _same_minute(WEEKLY_BRIEFING_TIME, local_time):
        start_date = local_date + timedelta(days=1)
        due.append(
            DueBriefing(
                TelegramBriefingDelivery.BriefingType.WEEKLY,
                local_date,
                start_date,
                start_date + timedelta(days=7),
            )
        )
    if (
        local_date.weekday() == 6
        and local_date.day <= 7
        and _same_minute(MONTHLY_BRIEFING_TIME, local_time)
    ):
        start_date = local_date + timedelta(days=1)
        due.append(
            DueBriefing(
                TelegramBriefingDelivery.BriefingType.MONTHLY,
                local_date,
                start_date,
                one_month_after(start_date),
            )
        )
    return due


def send_due_briefings(*, bot=None, now=None) -> tuple[int, int]:
    """Send each due briefing type at most once per user/date."""
    bot = bot or TelegramBot()
    now = now or timezone.now()
    local_now = now.astimezone(ZoneInfo(settings.APP_TIMEZONE))
    preferences = TelegramPreference.objects.select_related("user").filter(briefing_enabled=True)
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
        for briefing in due_briefings(preference, local_now):
            try:
                with transaction.atomic():
                    delivery = TelegramBriefingDelivery.objects.create(
                        user=user,
                        briefing_type=briefing.briefing_type,
                        briefing_date=briefing.delivery_date,
                    )
            except IntegrityError:
                continue
            try:
                if briefing.briefing_type == TelegramBriefingDelivery.BriefingType.DAILY:
                    message = build_today_message()
                else:
                    message = build_planning_message(
                        period=briefing.briefing_type,
                        start_date=briefing.start_date,
                        end_date=briefing.end_date,
                    )
                bot.send_message(chat.chat_id, message, reply_markup=today_keyboard())
            except Exception:  # noqa: BLE001 - retry is safe because claim is removed
                delivery.delete()
                failed += 1
            else:
                sent += 1
    return sent, failed


class Command(BaseCommand):
    help = "Send due daily, weekly, and monthly private Telegram LifeOS briefings."

    def handle(self, *args, **options):
        sent, failed = send_due_briefings()
        self.stdout.write(f"Sent {sent} Telegram briefing(s). {failed} failed.")
