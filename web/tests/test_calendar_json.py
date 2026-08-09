"""The calendar JSON endpoint combines calendar events, task due dates, and
reminders into FullCalendar-compatible events, distinguished by type."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import (
    AssignedTo, CalendarEventRecord, IncomingEmail, ParsedAction, PatchworkShift, RecipientTarget, Reminder, Task,
)

User = get_user_model()


class CalendarJsonTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="member", password="pw123456!")
        self.client.force_login(self.user)

        today = timezone.localdate()
        email = IncomingEmail.objects.create(gmail_message_id="msg-cal-1", outer_sender="ike@example.com")
        action = ParsedAction.objects.create(
            incoming_email=email, action_type=ParsedAction.ActionType.CREATE_CALENDAR_EVENT, title="Dentist",
        )
        CalendarEventRecord.objects.create(
            parsed_action=action, incoming_email=email, google_event_id="evt-1", calendar_id="cal-1",
            title="Dentist", appointment_date=today + timedelta(days=2), all_day=True,
        )
        Task.objects.create(
            title="Pay rent", assigned_to=AssignedTo.IKE, due_date=today + timedelta(days=1),
        )
        Reminder.objects.create(
            title="Water plants", recipient=RecipientTarget.WIFE,
            reminder_date=today + timedelta(days=1), reminder_time=timezone.now().time(),
        )
        PatchworkShift.objects.create(
            source_uid="patchwork-shift-1",
            source_label="Long Day — General Medicine",
            google_event_id="patchwork-google-1",
            calendar_id="cal-1",
            starts_at=timezone.now() + timedelta(days=3),
            ends_at=timezone.now() + timedelta(days=3, hours=12),
        )

    def test_calendar_json_includes_all_three_types(self):
        response = self.client.get(reverse("web:calendar_events_json"))
        self.assertEqual(response.status_code, 200)
        events = response.json()
        types = {event["extendedProps"]["type"] for event in events}
        self.assertEqual(types, {"Calendar event", "Ike work shift", "Task due date", "Reminder"})
        work_shift = next(event for event in events if event["extendedProps"]["type"] == "Ike work shift")
        self.assertEqual(work_shift["title"], "Ike — Long Day — General Medicine")

    def test_calendar_json_respects_start_end_range(self):
        far_future = (timezone.localdate() + timedelta(days=400)).isoformat()
        response = self.client.get(reverse("web:calendar_events_json"), {"start": far_future})
        self.assertEqual(response.json(), [])
