from datetime import date, time

from django.test import TestCase

from core.models import CalendarEventRecord, IncomingEmail, ParsedAction
from assistant.services.google_calendar import check_for_duplicate, compute_duplicate_key


class DuplicateKeyTests(TestCase):
    def test_same_inputs_produce_the_same_key(self):
        key1 = compute_duplicate_key("Dental appointment", date(2026, 8, 18), "10:30", "primary")
        key2 = compute_duplicate_key("Dental appointment", date(2026, 8, 18), "10:30", "primary")
        self.assertEqual(key1, key2)

    def test_key_is_case_and_whitespace_insensitive_on_title(self):
        key1 = compute_duplicate_key("Dental Appointment", date(2026, 8, 18), "10:30", "primary")
        key2 = compute_duplicate_key("  dental   appointment  ", date(2026, 8, 18), "10:30", "primary")
        self.assertEqual(key1, key2)

    def test_different_date_produces_a_different_key(self):
        key1 = compute_duplicate_key("Dental appointment", date(2026, 8, 18), "10:30", "primary")
        key2 = compute_duplicate_key("Dental appointment", date(2026, 8, 19), "10:30", "primary")
        self.assertNotEqual(key1, key2)

    def test_all_day_uses_the_all_day_sentinel(self):
        key1 = compute_duplicate_key("School trip", date(2026, 8, 18), "ALL_DAY", "primary")
        key2 = compute_duplicate_key("School trip", date(2026, 8, 18), "09:00", "primary")
        self.assertNotEqual(key1, key2)


class RescheduleDetectionTests(TestCase):
    def _make_incoming_email(self, gmail_message_id="msg-1"):
        return IncomingEmail.objects.create(
            gmail_message_id=gmail_message_id,
            outer_sender="ike@example.com",
            subject="Appointment",
            status=IncomingEmail.Status.PROCESSED,
        )

    def _make_existing_event(self, booking_reference, appointment_date, start_time):
        email = self._make_incoming_email(gmail_message_id="original-msg")
        action = ParsedAction.objects.create(
            incoming_email=email,
            action_type=ParsedAction.ActionType.CREATE_CALENDAR_EVENT,
            title="Dental appointment",
            appointment_date=appointment_date,
            start_time=start_time,
            confidence=0.95,
            status=ParsedAction.Status.EXECUTED,
        )
        return CalendarEventRecord.objects.create(
            parsed_action=action,
            incoming_email=email,
            google_event_id="evt-1",
            calendar_id="primary",
            title="Dental appointment",
            appointment_date=appointment_date,
            start_time=start_time,
            booking_reference=booking_reference,
            duplicate_key=compute_duplicate_key("Dental appointment", appointment_date, start_time.strftime("%H:%M"), "primary"),
        )

    def test_same_booking_reference_same_date_time_is_a_duplicate(self):
        self._make_existing_event("BSD-88213", date(2026, 8, 18), time(10, 30))
        new_email = self._make_incoming_email(gmail_message_id="new-msg")
        new_action = ParsedAction.objects.create(
            incoming_email=new_email,
            action_type=ParsedAction.ActionType.CREATE_CALENDAR_EVENT,
            title="Dental appointment",
            appointment_date=date(2026, 8, 18),
            start_time=time(10, 30),
            booking_reference="BSD-88213",
            confidence=0.95,
        )
        kind, existing = check_for_duplicate(new_action, "primary")
        self.assertEqual(kind, "duplicate")
        self.assertIsNotNone(existing)

    def test_same_booking_reference_changed_date_is_a_possible_reschedule(self):
        self._make_existing_event("BSD-88213", date(2026, 8, 18), time(10, 30))
        new_email = self._make_incoming_email(gmail_message_id="new-msg-2")
        new_action = ParsedAction.objects.create(
            incoming_email=new_email,
            action_type=ParsedAction.ActionType.CREATE_CALENDAR_EVENT,
            title="Dental appointment",
            appointment_date=date(2026, 8, 25),  # moved to a new date
            start_time=time(10, 30),
            booking_reference="BSD-88213",
            confidence=0.95,
        )
        kind, existing = check_for_duplicate(new_action, "primary")
        self.assertEqual(kind, "reschedule")
        self.assertIsNotNone(existing)
