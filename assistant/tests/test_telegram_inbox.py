from datetime import date, datetime, time, timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from assistant.management.commands.send_telegram_briefings import send_due_briefings
from assistant.services.google_calendar import reschedule_calendar_event
from assistant.services.telegram_handlers import process_telegram_update
from core.models import (
    CalendarEventRecord,
    IncomingEmail,
    Note,
    ParsedAction,
    Reminder,
    Task,
    TelegramBriefingDelivery,
    TelegramChat,
    TelegramPreference,
    TelegramUser,
)


SETTINGS = {
    "TELEGRAM_WEBHOOK_SECRET": "test-webhook-secret",
    "TELEGRAM_BOT_TOKEN": "test-token",
    "TELEGRAM_IKE_USER_ID": 446464092,
    "TELEGRAM_WIFE_USER_ID": 1115153420,
    "TELEGRAM_ALLOWED_GROUP_CHAT_IDS": [],
    "AUTHORISED_EMAIL_IKE": "ike@example.com",
    "AUTHORISED_EMAIL_WIFE": "wife@example.com",
    "OPENAI_API_KEY": "test-openai-key",
    "OPENAI_MODEL": "test-model",
    "APP_TIMEZONE": "Europe/London",
}


class FakeTelegramBot:
    def __init__(self):
        self.messages = []

    def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text, kwargs))
        return {"message_id": len(self.messages) + 100}

    def answer_callback(self, callback_query_id, text=""):
        return True


def message_update(update_id, text, user_id=446464092):
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id + 10,
            "from": {"id": user_id, "is_bot": False, "first_name": "Ike"},
            "chat": {"id": user_id, "type": "private"},
            "text": text,
        },
    }


def callback_update(update_id, data, user_id=446464092):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "from": {"id": user_id, "is_bot": False, "first_name": "Ike"},
            "data": data,
            "message": {"message_id": update_id + 100, "chat": {"id": user_id, "type": "private"}},
        },
    }


@override_settings(**SETTINGS)
class TelegramInboxTests(TestCase):
    def setUp(self):
        self.bot = FakeTelegramBot()

    def test_today_shows_due_items_and_inbox_lists_notes_and_reminders(self):
        today = timezone.localdate()
        Task.objects.create(title="Renew insurance", due_date=today)
        Reminder.objects.create(title="Pay council tax", recipient="ike", reminder_date=today, reminder_time=time(10, 0))
        Note.objects.create(title="Kitchen colour", body="Eggshell white")

        process_telegram_update(message_update(100, "/today"), bot=self.bot)
        self.assertIn("Renew insurance", self.bot.messages[-1][1])
        self.assertIn("Pay council tax", self.bot.messages[-1][1])

        process_telegram_update(message_update(101, "/notes"), bot=self.bot)
        self.assertIn("Kitchen colour", self.bot.messages[-1][1])
        process_telegram_update(message_update(102, "/reminders"), bot=self.bot)
        self.assertIn("Pay council tax", self.bot.messages[-1][1])

    def test_note_can_be_edited_after_explicit_confirmation(self):
        note = Note.objects.create(title="Kitchen colour", body="Blue")
        process_telegram_update(message_update(110, "/start"), bot=self.bot)
        process_telegram_update(callback_update(111, f"tg:note:edit:{note.id}"), bot=self.bot)
        process_telegram_update(message_update(112, "Eggshell white"), bot=self.bot)
        confirmation = self.bot.messages[-1][2]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        process_telegram_update(callback_update(113, confirmation), bot=self.bot)
        note.refresh_from_db()
        self.assertEqual(note.body, "Eggshell white")

    def test_reminder_can_be_snoozed_from_inbox(self):
        reminder = Reminder.objects.create(
            title="Put bins out",
            recipient="ike",
            reminder_date=timezone.localdate(),
            reminder_time=time(9, 0),
        )
        process_telegram_update(message_update(120, "/start"), bot=self.bot)
        process_telegram_update(callback_update(121, f"tg:rem:snooze:{reminder.id}"), bot=self.bot)
        reminder.refresh_from_db()
        self.assertEqual(reminder.reminder_date, timezone.localdate() + timedelta(days=1))
        self.assertEqual(reminder.status, Reminder.Status.PENDING)

    def test_search_finds_open_tasks_and_notes(self):
        Task.objects.create(title="Book boiler service")
        Note.objects.create(title="Boiler warranty", body="Kept in the kitchen drawer")
        process_telegram_update(message_update(130, "/search boiler"), bot=self.bot)
        self.assertIn("Book boiler service", self.bot.messages[-1][1])
        self.assertIn("Boiler warranty", self.bot.messages[-1][1])


@override_settings(**SETTINGS)
class TelegramBriefingTests(TestCase):
    def test_sends_once_to_started_private_user_at_configured_time(self):
        user = TelegramUser.objects.create(user_id=446464092, role="ike", display_name="Ike")
        TelegramChat.objects.create(chat_id=446464092, chat_type="private", authorised=True)
        now = datetime(2026, 8, 8, 7, 30, tzinfo=timezone.get_current_timezone())
        TelegramPreference.objects.create(user=user, briefing_time=time(7, 30))
        bot = FakeTelegramBot()

        self.assertEqual(send_due_briefings(bot=bot, now=now), (1, 0))
        self.assertEqual(send_due_briefings(bot=bot, now=now), (0, 0))
        self.assertEqual(TelegramBriefingDelivery.objects.count(), 1)
        self.assertIn("LifeOS today", bot.messages[0][1])


@override_settings(AUTHORISED_EMAIL_IKE="ike@example.com", GOOGLE_CALENDAR_ID="primary")
class CalendarManagementTests(TestCase):
    def _event(self):
        email = IncomingEmail.objects.create(gmail_message_id="calendar-manage", outer_sender="ike@example.com")
        action = ParsedAction.objects.create(
            incoming_email=email,
            action_type=ParsedAction.ActionType.CREATE_CALENDAR_EVENT,
            title="Dentist",
            appointment_date=date(2026, 8, 12),
        )
        return CalendarEventRecord.objects.create(
            parsed_action=action,
            incoming_email=email,
            google_event_id="event-1",
            calendar_id="primary",
            title="Dentist",
            appointment_date=date(2026, 8, 12),
            start_time=time(9, 0),
            end_time=time(10, 0),
        )

    @patch("assistant.services.google_calendar.get_google_credentials")
    @patch("assistant.services.google_calendar.build")
    def test_reschedule_updates_google_and_local_record(self, mock_build, _mock_creds):
        record = self._event()
        service = MagicMock()
        mock_build.return_value = service

        result = reschedule_calendar_event(record, appointment_date=date(2026, 8, 13), start_time=time(14, 0))

        self.assertEqual(result.appointment_date, date(2026, 8, 13))
        self.assertEqual(result.start_time, time(14, 0))
        self.assertEqual(result.end_time, time(15, 0))
        patch_body = service.events.return_value.patch.call_args.kwargs["body"]
        self.assertIn("2026-08-13T14:00", patch_body["start"]["dateTime"])
