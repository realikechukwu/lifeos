from datetime import date, datetime, time, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings
from django.utils import timezone

from assistant.management.commands.send_telegram_briefings import send_due_briefings
from assistant.services.google_calendar import reschedule_calendar_event
from assistant.services.telegram_handlers import build_planning_message, process_telegram_update
from assistant.services.telegram_inbox import one_month_after
from core.models import (
    CalendarEventRecord,
    IncomingEmail,
    Note,
    PatchworkShift,
    ParsedAction,
    Reminder,
    Task,
    TelegramBriefingDelivery,
    TelegramChat,
    TelegramInboxAction,
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

    def _calendar_event(self, title: str, *, day_offset: int = 1):
        email = IncomingEmail.objects.create(
            gmail_message_id=f"telegram-calendar-{title}", outer_sender="ike@example.com"
        )
        action = ParsedAction.objects.create(
            incoming_email=email,
            action_type=ParsedAction.ActionType.CREATE_CALENDAR_EVENT,
            title=title,
            appointment_date=timezone.localdate() + timedelta(days=day_offset),
        )
        return CalendarEventRecord.objects.create(
            parsed_action=action,
            incoming_email=email,
            google_event_id=f"google-{title}",
            calendar_id="primary",
            title=title,
            appointment_date=timezone.localdate() + timedelta(days=day_offset),
            start_time=time(9 + day_offset, 0),
            end_time=time(10 + day_offset, 0),
        )

    def _button(self, text: str):
        return next(
            button
            for row in self.bot.messages[-1][2]["reply_markup"]["inline_keyboard"]
            for button in row
            if button["text"] == text
        )

    def _patchwork_shift(
        self,
        label: str,
        *,
        day_offset: int = 1,
        start: time = time(9, 0),
        end: time = time(17, 0),
        active: bool = True,
        suppressed: bool = False,
    ):
        shift_date = timezone.localdate() + timedelta(days=day_offset)
        source_zone = timezone.get_current_timezone()
        return PatchworkShift.objects.create(
            source_uid=f"telegram-shift-{label}-{day_offset}-{start}",
            source_label=label,
            google_event_id=f"google-shift-{label}",
            calendar_id="primary",
            starts_at=timezone.make_aware(datetime.combine(shift_date, start), source_zone),
            ends_at=timezone.make_aware(datetime.combine(shift_date, end), source_zone),
            timezone="Europe/London",
            active=active,
            suppressed=suppressed,
        )

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

    def test_week_and_month_commands_show_rolling_plans_without_delivery_records(self):
        today = timezone.localdate()
        self._calendar_event("Today event", day_offset=0)
        self._calendar_event("Sixth day event", day_offset=6)
        self._calendar_event("Weekly boundary event", day_offset=7)
        month_end = one_month_after(today)
        Task.objects.create(title="Inside monthly window", due_date=month_end - timedelta(days=1))
        Task.objects.create(title="Monthly boundary", due_date=month_end)

        process_telegram_update(message_update(103, "/week"), bot=self.bot)
        weekly_message = self.bot.messages[-1][1]
        self.assertIn("LifeOS week ahead", weekly_message)
        self.assertIn("Today event", weekly_message)
        self.assertIn("Sixth day event", weekly_message)
        self.assertNotIn("Weekly boundary event", weekly_message)

        process_telegram_update(message_update(104, "/month"), bot=self.bot)
        monthly_message = self.bot.messages[-1][1]
        self.assertIn("LifeOS month ahead", monthly_message)
        self.assertIn("Weekly boundary event", monthly_message)
        self.assertIn("Inside monthly window", monthly_message)
        self.assertNotIn("Monthly boundary", monthly_message)
        self.assertEqual(TelegramBriefingDelivery.objects.count(), 0)

    def test_main_menu_clearly_separates_manage_and_create_actions(self):
        process_telegram_update(message_update(105, "/start"), bot=self.bot)
        self.assertEqual(
            self.bot.messages[-1][1],
            "Hi, I’m LifeOS. Send a voice note or text. I’ll clarify anything unclear and ask for "
            "confirmation before making changes.",
        )
        buttons = [
            button["text"]
            for row in self.bot.messages[-1][2]["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertIn("📋 Tasks", buttons)
        self.assertIn("📆 Week", buttons)
        self.assertIn("🗓 Month", buttons)
        self.assertIn("📚 Notes", buttons)
        self.assertIn("🔔 Reminders", buttons)
        self.assertEqual([label for label in buttons if label in {"➕ Task", "➕ Event", "➕ Note", "➕ Reminder"}], [
            "➕ Task", "➕ Event", "➕ Note", "➕ Reminder"
        ])

    def test_calendar_list_uses_named_event_buttons_then_shows_actions_for_one_event(self):
        dentist = self._calendar_event("Dentist", day_offset=1)
        school = self._calendar_event("School meeting", day_offset=2)

        process_telegram_update(message_update(106, "/calendar"), bot=self.bot)

        buttons = [
            button
            for row in self.bot.messages[-1][2]["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        labels = [button["text"] for button in buttons]
        self.assertIn("1 · Dentist", labels)
        self.assertIn("2 · School meeting", labels)
        self.assertNotIn("Reschedule", labels)
        self.assertNotIn("Cancel event", labels)
        self.assertIn("⬅️ Back", labels)
        self.assertIn("🏠 Home", labels)

        school_callback = next(button["callback_data"] for button in buttons if button["text"] == "2 · School meeting")
        process_telegram_update(callback_update(107, school_callback), bot=self.bot)

        self.assertIn("School meeting", self.bot.messages[-1][1])
        self.assertNotIn("Dentist", self.bot.messages[-1][1])
        detail_buttons = [
            button
            for row in self.bot.messages[-1][2]["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertIn("Reschedule", [button["text"] for button in detail_buttons])
        self.assertIn("Cancel event", [button["text"] for button in detail_buttons])
        self.assertIn("⬅️ Back to calendar", [button["text"] for button in detail_buttons])
        self.assertTrue(all(str(school.id) in button["callback_data"] for button in detail_buttons[:2]))
        self.assertNotEqual(dentist.id, school.id)

    def test_calendar_includes_patchwork_shift_with_read_only_detail(self):
        shift = self._patchwork_shift("Standard Day — General Medicine")
        self._calendar_event("Dentist", day_offset=2)

        process_telegram_update(message_update(222, "/calendar"), bot=self.bot)

        self.assertIn("Ike — Standard Day — General Medicine", self.bot.messages[-1][1])
        shift_button = next(
            button
            for row in self.bot.messages[-1][2]["reply_markup"]["inline_keyboard"]
            for button in row
            if button["callback_data"].startswith("tg:shift:view:")
        )
        self.assertTrue(shift_button["text"].startswith("1 · Ike — Standard Day"))
        self.assertEqual(shift_button["callback_data"], f"tg:shift:view:{shift.id}:0:home")

        process_telegram_update(
            callback_update(223, shift_button["callback_data"]), bot=self.bot
        )

        self.assertIn("Managed by Patchwork", self.bot.messages[-1][1])
        self.assertIn("09:00–17:00", self.bot.messages[-1][1])
        labels = [
            button["text"]
            for row in self.bot.messages[-1][2]["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertIn("Why read-only?", labels)
        self.assertNotIn("Reschedule", labels)
        self.assertNotIn("Cancel event", labels)
        self.assertIn("⬅️ Back to calendar", labels)
        self.assertIn("🏠 Home", labels)

    def test_calendar_hides_inactive_and_suppressed_patchwork_shifts(self):
        self._patchwork_shift("Inactive", active=False)
        self._patchwork_shift("Suppressed", suppressed=True)
        visible = self._patchwork_shift("Twilight", start=time(17, 0), end=time(21, 30))

        process_telegram_update(message_update(224, "/calendar"), bot=self.bot)

        self.assertIn(visible.display_title, self.bot.messages[-1][1])
        self.assertNotIn("Inactive", self.bot.messages[-1][1])
        self.assertNotIn("Suppressed", self.bot.messages[-1][1])

    def test_back_from_event_detail_returns_to_calendar_page(self):
        self._calendar_event("Dentist")
        process_telegram_update(message_update(108, "/calendar"), bot=self.bot)
        process_telegram_update(callback_update(109, self._button("1 · Dentist")["callback_data"]), bot=self.bot)

        process_telegram_update(
            callback_update(110, self._button("⬅️ Back to calendar")["callback_data"]),
            bot=self.bot,
        )

        self.assertIn("Upcoming calendar", self.bot.messages[-1][1])
        self.assertIn("Dentist", self.bot.messages[-1][1])

    def test_home_cancels_pending_reschedule_before_showing_main_menu(self):
        event = self._calendar_event("Dentist")
        process_telegram_update(message_update(114, "/calendar"), bot=self.bot)
        process_telegram_update(callback_update(115, self._button("1 · Dentist")["callback_data"]), bot=self.bot)
        process_telegram_update(callback_update(116, self._button("Reschedule")["callback_data"]), bot=self.bot)
        action = TelegramInboxAction.objects.get(object_id=event.id)
        self.assertEqual(action.status, TelegramInboxAction.Status.AWAITING_INPUT)

        process_telegram_update(callback_update(117, self._button("🏠 Home")["callback_data"]), bot=self.bot)

        action.refresh_from_db()
        self.assertEqual(action.status, TelegramInboxAction.Status.CANCELLED)
        self.assertIn("What would you like to do?", self.bot.messages[-1][1])

    def test_back_from_reschedule_cancels_action_and_returns_to_same_event(self):
        event = self._calendar_event("Dentist")
        process_telegram_update(message_update(118, "/calendar"), bot=self.bot)
        process_telegram_update(
            callback_update(119, self._button("1 · Dentist")["callback_data"]),
            bot=self.bot,
        )
        process_telegram_update(
            callback_update(120, self._button("Reschedule")["callback_data"]),
            bot=self.bot,
        )
        action = TelegramInboxAction.objects.get(object_id=event.id)

        process_telegram_update(
            callback_update(121, self._button("⬅️ Back to event")["callback_data"]),
            bot=self.bot,
        )

        action.refresh_from_db()
        self.assertEqual(action.status, TelegramInboxAction.Status.CANCELLED)
        self.assertIn("Dentist", self.bot.messages[-1][1])
        self.assertEqual(self._button("Reschedule")["callback_data"].split(":")[3], str(event.id))

    def test_primary_non_home_screens_have_back_and_home_navigation(self):
        for update_id, command in enumerate(
            [
                "/today",
                "/week",
                "/month",
                "/tasks",
                "/calendar",
                "/notes",
                "/reminders",
                "/settings",
                "/upcoming",
            ],
            start=200,
        ):
            process_telegram_update(message_update(update_id, command), bot=self.bot)
            labels = [
                button["text"]
                for row in self.bot.messages[-1][2]["reply_markup"]["inline_keyboard"]
                for button in row
            ]
            self.assertIn("⬅️ Back", labels, command)
            self.assertIn("🏠 Home", labels, command)

    def test_recurring_event_detail_has_read_only_action_and_navigation(self):
        event = self._calendar_event("School term")
        event.recurrence_rule = "RRULE:FREQ=WEEKLY"
        event.recurrence_description = "Weekly"
        event.save(update_fields=["recurrence_rule", "recurrence_description", "updated_at"])
        process_telegram_update(message_update(220, "/calendar"), bot=self.bot)

        process_telegram_update(
            callback_update(221, self._button("1 · School term")["callback_data"]),
            bot=self.bot,
        )

        labels = [
            button["text"]
            for row in self.bot.messages[-1][2]["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertIn("Why read-only?", labels)
        self.assertNotIn("Reschedule", labels)
        self.assertNotIn("Cancel event", labels)
        self.assertIn("⬅️ Back to calendar", labels)
        self.assertIn("🏠 Home", labels)

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
    def _started_user(self, *, briefing_time=time(7, 30)):
        user = TelegramUser.objects.create(user_id=446464092, role="ike", display_name="Ike")
        TelegramChat.objects.create(chat_id=446464092, chat_type="private", authorised=True)
        TelegramPreference.objects.create(user=user, briefing_time=briefing_time)
        return user

    def _calendar_event(self, title: str, appointment_date: date):
        email = IncomingEmail.objects.create(
            gmail_message_id=f"briefing-{title}", outer_sender="ike@example.com"
        )
        action = ParsedAction.objects.create(
            incoming_email=email,
            action_type=ParsedAction.ActionType.CREATE_CALENDAR_EVENT,
            title=title,
            appointment_date=appointment_date,
        )
        return CalendarEventRecord.objects.create(
            parsed_action=action,
            incoming_email=email,
            google_event_id=f"google-{title}",
            calendar_id="primary",
            title=title,
            appointment_date=appointment_date,
            start_time=time(9, 0),
            end_time=time(10, 0),
        )

    def test_sends_once_to_started_private_user_at_configured_time(self):
        user = self._started_user()
        now = datetime(2026, 8, 8, 7, 30, tzinfo=timezone.get_current_timezone())
        bot = FakeTelegramBot()

        self.assertEqual(send_due_briefings(bot=bot, now=now), (1, 0))
        self.assertEqual(send_due_briefings(bot=bot, now=now), (0, 0))
        self.assertEqual(TelegramBriefingDelivery.objects.count(), 1)
        self.assertEqual(
            TelegramBriefingDelivery.objects.get().briefing_type,
            TelegramBriefingDelivery.BriefingType.DAILY,
        )
        self.assertIn("LifeOS today", bot.messages[0][1])

    def test_sunday_evening_sends_next_monday_to_sunday_weekly_digest(self):
        user = self._started_user()
        now = datetime(2026, 8, 9, 18, 0, tzinfo=timezone.get_current_timezone())
        self._calendar_event("Monday clinic", date(2026, 8, 10))
        Task.objects.create(title="Friday deadline", due_date=date(2026, 8, 14))
        Reminder.objects.create(
            title="Sunday reminder",
            recipient="ike",
            reminder_date=date(2026, 8, 16),
            reminder_time=time(17, 0),
        )
        Task.objects.create(title="Outside window", due_date=date(2026, 8, 17))
        bot = FakeTelegramBot()

        self.assertEqual(send_due_briefings(bot=bot, now=now), (1, 0))
        self.assertEqual(send_due_briefings(bot=bot, now=now), (0, 0))

        message = bot.messages[0][1]
        self.assertIn("LifeOS week ahead", message)
        self.assertIn("Mon 10 Aug 2026 – Sun 16 Aug 2026", message)
        self.assertIn("Monday clinic", message)
        self.assertIn("Friday deadline", message)
        self.assertIn("Sunday reminder", message)
        self.assertNotIn("Outside window", message)
        self.assertEqual(
            TelegramBriefingDelivery.objects.get(user=user).briefing_type,
            TelegramBriefingDelivery.BriefingType.WEEKLY,
        )

    def test_first_sunday_separates_daily_and_monthly_delivery_times(self):
        user = self._started_user()
        # London is one hour ahead of UTC while daylight-saving time is active.
        daily_time = datetime(2026, 9, 6, 6, 30, tzinfo=ZoneInfo("UTC"))
        monthly_time = datetime(2026, 9, 6, 8, 30, tzinfo=ZoneInfo("UTC"))
        Task.objects.create(title="Inside month", due_date=date(2026, 10, 6))
        Task.objects.create(title="End boundary", due_date=date(2026, 10, 7))
        bot = FakeTelegramBot()

        self.assertEqual(send_due_briefings(bot=bot, now=daily_time), (1, 0))
        self.assertIn("LifeOS today", bot.messages[0][1])
        self.assertEqual(send_due_briefings(bot=bot, now=monthly_time), (1, 0))
        self.assertEqual(send_due_briefings(bot=bot, now=monthly_time), (0, 0))

        monthly_message = bot.messages[1][1]
        self.assertIn("LifeOS month ahead", monthly_message)
        self.assertIn("Mon 7 Sep 2026 – Tue 6 Oct 2026", monthly_message)
        self.assertIn("Inside month", monthly_message)
        self.assertNotIn("End boundary", monthly_message)
        self.assertEqual(
            set(TelegramBriefingDelivery.objects.filter(user=user).values_list("briefing_type", flat=True)),
            {
                TelegramBriefingDelivery.BriefingType.DAILY,
                TelegramBriefingDelivery.BriefingType.MONTHLY,
            },
        )

    def test_later_sunday_morning_does_not_send_monthly_digest(self):
        self._started_user()
        now = datetime(2026, 9, 13, 9, 30, tzinfo=timezone.get_current_timezone())
        bot = FakeTelegramBot()

        self.assertEqual(send_due_briefings(bot=bot, now=now), (0, 0))
        self.assertEqual(bot.messages, [])
        self.assertFalse(
            TelegramBriefingDelivery.objects.filter(
                briefing_type=TelegramBriefingDelivery.BriefingType.MONTHLY
            ).exists()
        )

    def test_planning_message_is_bounded_and_html_safe(self):
        for index in range(11):
            self._calendar_event(
                f"Unsafe <event> & planning title {index} " + "&" * 100,
                date(2026, 9, 7),
            )

        message = build_planning_message(
            period="weekly",
            start_date=date(2026, 9, 7),
            end_date=date(2026, 9, 14),
        )

        self.assertLess(len(message), 4096)
        self.assertNotIn("<event>", message)
        self.assertIn("&lt;event&gt;", message)
        self.assertIn("…and 1 more", message)


@override_settings(AUTHORISED_EMAIL_IKE="ike@example.com", GOOGLE_CALENDAR_ID="primary")
class CalendarManagementTests(TestCase):
    def _event(self):
        event_date = timezone.localdate() + timedelta(days=1)
        email = IncomingEmail.objects.create(gmail_message_id="calendar-manage", outer_sender="ike@example.com")
        action = ParsedAction.objects.create(
            incoming_email=email,
            action_type=ParsedAction.ActionType.CREATE_CALENDAR_EVENT,
            title="Dentist",
            appointment_date=event_date,
        )
        return CalendarEventRecord.objects.create(
            parsed_action=action,
            incoming_email=email,
            google_event_id="event-1",
            calendar_id="primary",
            title="Dentist",
            appointment_date=event_date,
            start_time=time(9, 0),
            end_time=time(10, 0),
        )

    @patch("assistant.services.google_calendar.get_google_credentials")
    @patch("assistant.services.google_calendar.build")
    def test_reschedule_updates_google_and_local_record(self, mock_build, _mock_creds):
        record = self._event()
        target_date = timezone.localdate() + timedelta(days=2)
        service = MagicMock()
        mock_build.return_value = service

        result = reschedule_calendar_event(
            record, appointment_date=target_date, start_time=time(14, 0)
        )

        self.assertEqual(result.appointment_date, target_date)
        self.assertEqual(result.start_time, time(14, 0))
        self.assertEqual(result.end_time, time(15, 0))
        patch_body = service.events.return_value.patch.call_args.kwargs["body"]
        self.assertIn(f"{target_date.isoformat()}T14:00", patch_body["start"]["dateTime"])
