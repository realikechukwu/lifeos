"""The Upcoming feed combines calendar events, task due dates (including
overdue), pending reminders, and pending-review actions."""

from datetime import datetime, time, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import (
    AssignedTo,
    CalendarEventRecord,
    IncomingEmail,
    ParsedAction,
    RecipientTarget,
    Reminder,
    Task,
    PatchworkShift,
)

User = get_user_model()


class UpcomingFeedTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="member", password="pw123456!")
        self.client.force_login(self.user)

        today = timezone.localdate()

        self.email = IncomingEmail.objects.create(
            gmail_message_id="msg-1", outer_sender="ike@example.com", subject="Test",
        )
        self.parsed_action = ParsedAction.objects.create(
            incoming_email=self.email,
            action_type=ParsedAction.ActionType.CREATE_TASK,
            title="Needs a human",
            status=ParsedAction.Status.PENDING_REVIEW,
        )

        self.calendar_event = CalendarEventRecord.objects.create(
            parsed_action=self.parsed_action,
            incoming_email=self.email,
            google_event_id="evt-1",
            calendar_id="cal-1",
            title="Dentist",
            appointment_date=today + timedelta(days=3),
        )

        self.overdue_task = Task.objects.create(
            title="Overdue chore", assigned_to=AssignedTo.IKE, status=Task.Status.OPEN,
            due_date=today - timedelta(days=2),
        )
        self.upcoming_task = Task.objects.create(
            title="Future chore", assigned_to=AssignedTo.WIFE, status=Task.Status.OPEN,
            due_date=today + timedelta(days=5),
        )
        self.far_task = Task.objects.create(
            title="Far future chore", assigned_to=AssignedTo.WIFE, status=Task.Status.OPEN,
            due_date=today + timedelta(days=60),
        )

        self.reminder = Reminder.objects.create(
            title="Pay the bill", recipient=RecipientTarget.IKE,
            reminder_date=today + timedelta(days=1), reminder_time=timezone.now().time(),
        )

    def test_upcoming_combines_expected_item_kinds(self):
        response = self.client.get(reverse("web:upcoming"))
        self.assertEqual(response.status_code, 200)

        kinds = {item.kind for item in response.context["items"]}
        self.assertEqual(
            kinds,
            {"calendar_event", "task_due", "task_overdue", "reminder", "review"},
        )

    def test_far_future_task_is_excluded_from_the_30_day_window(self):
        response = self.client.get(reverse("web:upcoming"))
        titles = [item.title for item in response.context["items"]]
        self.assertNotIn("Far future chore", titles)

    def test_overdue_tasks_are_sorted_to_the_top(self):
        response = self.client.get(reverse("web:upcoming"))
        items = response.context["items"]
        self.assertTrue(items[0].is_overdue)
        self.assertEqual(items[0].title, "Overdue chore")

    def test_today_section_is_always_present_even_when_empty(self):
        response = self.client.get(reverse("web:upcoming"))

        sections = response.context["upcoming_sections"]
        self.assertTrue(sections[0].is_today)
        self.assertEqual(sections[0].label, "Today")
        self.assertEqual(sections[0].items, [])
        self.assertContains(response, "Nothing scheduled for today.")

    def test_attention_items_are_separate_from_dated_sections(self):
        response = self.client.get(reverse("web:upcoming"))

        attention_titles = {item.title for item in response.context["attention_items"]}
        dated_titles = {
            item.title
            for section in response.context["upcoming_sections"]
            for item in section.items
        }
        self.assertEqual(attention_titles, {"Overdue chore", "Needs a human"})
        self.assertNotIn("Overdue chore", dated_titles)
        self.assertNotIn("Needs a human", dated_titles)
        self.assertIn("Pay the bill", dated_titles)

    def test_tomorrow_section_and_relative_time_are_labelled(self):
        response = self.client.get(reverse("web:upcoming"))

        tomorrow = next(section for section in response.context["upcoming_sections"] if section.label == "Tomorrow")
        reminder = next(item for item in tomorrow.items if item.title == "Pay the bill")
        self.assertTrue(reminder.display_when.startswith("Tomorrow · "))

    def test_future_pending_reminders_are_limited_to_horizon(self):
        Reminder.objects.create(
            title="Much later reminder",
            recipient=RecipientTarget.WIFE,
            reminder_date=timezone.localdate() + timedelta(days=31),
            reminder_time=timezone.now().time(),
        )

        response = self.client.get(reverse("web:upcoming"))

        self.assertNotIn("Much later reminder", [item.title for item in response.context["items"]])

    def test_past_pending_reminder_is_an_overdue_attention_item(self):
        Reminder.objects.create(
            title="Missed reminder",
            recipient=RecipientTarget.WIFE,
            reminder_date=timezone.localdate() - timedelta(days=1),
            reminder_time=timezone.now().time(),
        )

        response = self.client.get(reverse("web:upcoming"))

        reminder = next(item for item in response.context["items"] if item.title == "Missed reminder")
        self.assertTrue(reminder.is_attention)
        self.assertTrue(reminder.is_overdue)
        self.assertEqual(reminder.status, "Overdue")
        self.assertEqual(reminder.display_when.split(" · ")[0], "1 day overdue")

    def test_reminder_due_earlier_today_needs_attention(self):
        today = timezone.localdate()
        Reminder.objects.create(title="Morning reminder", recipient=RecipientTarget.IKE,
                                reminder_date=today, reminder_time=time(8))
        now = timezone.make_aware(datetime.combine(today, time(12)))
        with patch("web.services.timezone.now", return_value=now):
            response = self.client.get(reverse("web:upcoming"))
        reminder = next(item for item in response.context["attention_items"] if item.title == "Morning reminder")
        self.assertEqual(reminder.display_when, "Due earlier today · 08:00")

    def test_horizon_includes_day_30(self):
        Reminder.objects.create(title="At horizon", recipient=RecipientTarget.IKE,
                                reminder_date=timezone.localdate() + timedelta(days=30), reminder_time=time(8))
        response = self.client.get(reverse("web:upcoming"))
        self.assertIn("At horizon", [item.title for item in response.context["items"]])

    def test_shift_label_and_section_use_same_display_timezone(self):
        today = timezone.localdate()
        # Source date is tomorrow, but London date is today.
        start = datetime.combine(today + timedelta(days=1), time(0, 30), ZoneInfo("Asia/Tokyo"))
        PatchworkShift.objects.create(source_uid="tokyo-shift", source_label="Shift",
                                     timezone="Asia/Tokyo", starts_at=start, ends_at=start + timedelta(hours=8))
        response = self.client.get(reverse("web:upcoming"))
        item = next(item for item in response.context["upcoming_sections"][0].items if item.kind == "work_shift")
        self.assertTrue(item.display_when.startswith("Today · "))

    def test_event_details_available_without_google_link(self):
        self.calendar_event.calendar_id = ""
        self.calendar_event.start_time = time(11)
        self.calendar_event.end_time = time(12)
        self.calendar_event.save()
        response = self.client.get(reverse("web:upcoming"))
        self.assertContains(response, "Details <span class=\"visually-hidden\">for Dentist")
        item = next(item for item in response.context["items"] if item.title == "Dentist")
        self.assertIn("11:00 – 12:00", item.details_when)
