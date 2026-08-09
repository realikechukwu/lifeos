from datetime import datetime
from unittest.mock import MagicMock

from django.test import TestCase, override_settings
from django.utils import timezone

from assistant.services.patchwork_calendar import parse_patchwork_calendar, sync_patchwork_calendar
from core.models import PatchworkShift


def calendar_with_event(*, uid="shift-1", start="20260810T070000", end="20260810T190000", status="CONFIRMED"):
    return f"""BEGIN:VCALENDAR\r
VERSION:2.0\r
BEGIN:VEVENT\r
UID:{uid}\r
DTSTART;TZID=Europe/London:{start}\r
DTEND;TZID=Europe/London:{end}\r
STATUS:{status}\r
SUMMARY:This source title must never be displayed\r
END:VEVENT\r
END:VCALENDAR\r
""".encode()


@override_settings(GOOGLE_CALENDAR_ID="shared-calendar", APP_TIMEZONE="Europe/London")
class PatchworkCalendarSyncTests(TestCase):
    def setUp(self):
        self.service = MagicMock()
        self.service.events.return_value.insert.return_value.execute.return_value = {"id": "google-shift-1"}
        self.now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.get_current_timezone())

    def test_parser_supports_folded_lines_and_london_times(self):
        payload = calendar_with_event().replace(
            b"SUMMARY:This source title must never be displayed", b"SUMMARY:This source title\r\n must never be displayed"
        )
        shifts = parse_patchwork_calendar(payload)

        self.assertEqual(len(shifts), 1)
        self.assertEqual(shifts[0].uid, "shift-1")
        self.assertEqual(shifts[0].starts_at.hour, 7)
        self.assertEqual(shifts[0].timezone_name, "Europe/London")

    def test_new_shift_creates_neutral_google_event_and_local_mapping(self):
        result = sync_patchwork_calendar(
            fetcher=lambda: calendar_with_event(), calendar_service=self.service, now=self.now
        )

        self.assertEqual(result.created, 1)
        record = PatchworkShift.objects.get(source_uid="shift-1")
        self.assertTrue(record.active)
        self.assertEqual(record.google_event_id, "google-shift-1")
        body = self.service.events.return_value.insert.call_args.kwargs["body"]
        self.assertEqual(body["summary"], "Ike work shift")
        self.assertNotIn("This source title", str(body))

    def test_changed_shift_patches_existing_google_event(self):
        sync_patchwork_calendar(fetcher=lambda: calendar_with_event(), calendar_service=self.service, now=self.now)
        changed = calendar_with_event(start="20260810T090000", end="20260810T210000")

        result = sync_patchwork_calendar(fetcher=lambda: changed, calendar_service=self.service, now=self.now)

        self.assertEqual(result.updated, 1)
        self.service.events.return_value.patch.assert_called_once()
        record = PatchworkShift.objects.get(source_uid="shift-1")
        self.assertEqual(timezone.localtime(record.starts_at).hour, 9)

    def test_missing_shift_is_removed_from_google_and_lifeos(self):
        sync_patchwork_calendar(fetcher=lambda: calendar_with_event(), calendar_service=self.service, now=self.now)

        result = sync_patchwork_calendar(
            fetcher=lambda: b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n",
            calendar_service=self.service,
            now=self.now,
        )

        self.assertEqual(result.deactivated, 1)
        record = PatchworkShift.objects.get(source_uid="shift-1")
        self.assertFalse(record.active)
        self.service.events.return_value.delete.assert_called_once_with(
            calendarId="shared-calendar", eventId="google-shift-1"
        )

    def test_cancelled_shift_removes_existing_mirror(self):
        sync_patchwork_calendar(fetcher=lambda: calendar_with_event(), calendar_service=self.service, now=self.now)

        result = sync_patchwork_calendar(
            fetcher=lambda: calendar_with_event(status="CANCELLED"), calendar_service=self.service, now=self.now
        )

        self.assertEqual(result.deactivated, 1)
        self.assertFalse(PatchworkShift.objects.get(source_uid="shift-1").active)
