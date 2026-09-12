"""The calendar JSON endpoint combines calendar events, task due dates, and
reminders into FullCalendar-compatible events, distinguished by type."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
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

    def test_overnight_event_end_is_the_following_date(self):
        event = CalendarEventRecord.objects.get(google_event_id="evt-1")
        event.all_day = False
        event.start_time = time(22)
        event.end_time = time(6)
        event.save()
        events = self.client.get(reverse("web:calendar_events_json")).json()
        result = next(item for item in events if item["id"] == f"calendar-{event.id}")
        self.assertEqual(result["end"], f"{event.appointment_date + timedelta(days=1)}T06:00:00")
        next_day = event.appointment_date + timedelta(days=1)
        overlap = self.client.get(reverse("web:calendar_events_json"), {
            "start": next_day.isoformat(), "end": (next_day + timedelta(days=1)).isoformat(),
        }).json()
        self.assertIn(f"calendar-{event.id}", [item["id"] for item in overlap])
        # An event finishing exactly at the range start does not overlap it.
        event.end_time = time.min
        event.save()
        midnight = self.client.get(reverse("web:calendar_events_json"), {
            "start": next_day.isoformat(), "end": (next_day + timedelta(days=1)).isoformat(),
        }).json()
        self.assertNotIn(f"calendar-{event.id}", [item["id"] for item in midnight])

    @override_settings(TIME_ZONE="Europe/London")
    def test_shift_overlapping_range_is_included_and_formatted_in_london(self):
        shift = PatchworkShift.objects.get(source_uid="patchwork-shift-1")
        shift.starts_at = datetime(2026, 9, 30, 22, tzinfo=ZoneInfo("UTC"))
        shift.ends_at = datetime(2026, 10, 1, 7, tzinfo=ZoneInfo("UTC"))
        shift.timezone = "UTC"
        shift.save()
        events = self.client.get(reverse("web:calendar_events_json"), {
            "start": "2026-10-01", "end": "2026-10-02",
        }).json()
        result = next(item for item in events if item["id"] == f"patchwork-{shift.id}")
        self.assertEqual(result["start"], "2026-09-30T23:00:00+01:00")
        self.assertEqual(result["end"], "2026-10-01T08:00:00+01:00")

    def test_shift_without_end_is_still_included(self):
        shift = PatchworkShift.objects.get(source_uid="patchwork-shift-1")
        shift.ends_at = None
        shift.save()
        events = self.client.get(reverse("web:calendar_events_json"), {
            "start": timezone.localdate().isoformat(),
        }).json()
        self.assertIn(f"patchwork-{shift.id}", [item["id"] for item in events])

    def test_filters_and_task_navigation_have_stable_metadata(self):
        events = self.client.get(reverse("web:calendar_events_json")).json()
        self.assertEqual({item["extendedProps"]["category"] for item in events}, {
            "event", "work", "task", "reminder",
        })
        task = next(item for item in events if item["extendedProps"]["category"] == "task")
        self.assertIn("?highlight=", task["extendedProps"]["detailUrl"])
