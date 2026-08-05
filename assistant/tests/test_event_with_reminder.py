from datetime import date, time, timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import CalendarEventRecord, IncomingEmail, ParsedAction, Reminder
from assistant.services.router import route_and_execute


@override_settings(
    AUTHORISED_EMAIL_IKE="ike@example.com",
    AUTHORISED_EMAIL_WIFE="wife@example.com",
    GOOGLE_CALENDAR_ID="primary",
)
class EventPlusLinkedReminderTests(TestCase):
    def _make_email(self):
        return IncomingEmail.objects.create(
            gmail_message_id="msg-event-reminder-1",
            outer_sender="ike@example.com",
            subject="Boiler service",
            body_text="Add the boiler service next Tuesday at 10am and remind both of us the day before.",
            status=IncomingEmail.Status.RECEIVED,
        )

    @patch("assistant.services.google_calendar.get_google_credentials")
    @patch("assistant.services.google_calendar.build")
    def test_one_instruction_creates_event_and_one_linked_reminder(self, mock_build, mock_creds):
        mock_service = MagicMock()
        mock_service.events.return_value.insert.return_value.execute.return_value = {"id": "evt-123"}
        mock_build.return_value = mock_service

        event_date = timezone.localdate() + timedelta(days=7)
        email = self._make_email()
        parsed_action = ParsedAction.objects.create(
            incoming_email=email,
            action_type=ParsedAction.ActionType.CREATE_CALENDAR_EVENT,
            title="Boiler service",
            appointment_date=event_date,
            start_time=time(10, 0),
            reminder_recipient="both",
            reminder_lead_days=1,  # "the day before" - Python computes the date
            confidence=0.95,
        )

        outcome, extra = route_and_execute(parsed_action)

        self.assertEqual(outcome, "created_with_reminder")
        self.assertEqual(CalendarEventRecord.objects.count(), 1)
        self.assertEqual(Reminder.objects.count(), 1)

        reminder = extra["reminder"]
        self.assertEqual(reminder.reminder_date, event_date - timedelta(days=1))
        self.assertEqual(reminder.reminder_time, time(10, 0))  # defaults to the event's own start time
        self.assertEqual(reminder.recipient, "both")
        self.assertEqual(reminder.related_calendar_event_id, extra["calendar_event"].id)

    @patch("assistant.services.google_calendar.get_google_credentials")
    @patch("assistant.services.google_calendar.build")
    def test_event_without_a_reminder_request_creates_no_reminder(self, mock_build, mock_creds):
        mock_service = MagicMock()
        mock_service.events.return_value.insert.return_value.execute.return_value = {"id": "evt-456"}
        mock_build.return_value = mock_service

        event_date = timezone.localdate() + timedelta(days=7)
        email = self._make_email()
        parsed_action = ParsedAction.objects.create(
            incoming_email=email,
            action_type=ParsedAction.ActionType.CREATE_CALENDAR_EVENT,
            title="Boiler service",
            appointment_date=event_date,
            start_time=time(10, 0),
            confidence=0.95,
        )

        outcome, extra = route_and_execute(parsed_action)

        self.assertEqual(outcome, "created")
        self.assertEqual(Reminder.objects.count(), 0)
