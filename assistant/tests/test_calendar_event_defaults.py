"""Calendar events only need a title + date to post — a missing start
time defaults to 09:00, a missing end time defaults to a 1-hour slot, and
other self-reported missing/ambiguous fields no longer block automatic
creation. The confirmation reply says what was assumed/left unclear
instead of silently guessing or parking a clear request in review."""

from datetime import date, time, timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import CalendarEventRecord, IncomingEmail, ParsedAction
from assistant.services.email_sender import build_confirmation_body
from assistant.services.google_calendar import DEFAULT_START_TIME
from assistant.services.router import route_and_execute


@override_settings(
    AUTHORISED_EMAIL_IKE="ike@example.com",
    AUTHORISED_EMAIL_WIFE="wife@example.com",
    GOOGLE_CALENDAR_ID="primary",
)
class CalendarEventDefaultingTests(TestCase):
    def _make_email(self, message_id="msg-defaults-1"):
        return IncomingEmail.objects.create(
            gmail_message_id=message_id,
            outer_sender="ike@example.com",
            subject="Library event",
            status=IncomingEmail.Status.RECEIVED,
        )

    @patch("assistant.services.google_calendar.get_google_credentials")
    @patch("assistant.services.google_calendar.build")
    def test_missing_start_time_still_creates_and_defaults_to_9am(self, mock_build, mock_creds):
        mock_service = MagicMock()
        mock_service.events.return_value.insert.return_value.execute.return_value = {"id": "evt-1"}
        mock_build.return_value = mock_service

        event_date = timezone.localdate() + timedelta(days=7)
        email = self._make_email()
        parsed_action = ParsedAction.objects.create(
            incoming_email=email,
            action_type=ParsedAction.ActionType.CREATE_CALENDAR_EVENT,
            title="Words and Pictures",
            appointment_date=event_date,
            start_time=None,  # not stated
            confidence=0.96,
            missing_fields=["start_time"],
        )

        outcome, extra = route_and_execute(parsed_action)

        self.assertEqual(outcome, "created")
        parsed_action.refresh_from_db()
        self.assertEqual(parsed_action.status, ParsedAction.Status.EXECUTED)
        self.assertEqual(parsed_action.start_time, DEFAULT_START_TIME)

        record = extra["calendar_event"]
        self.assertEqual(record.start_time, DEFAULT_START_TIME)

        body = build_confirmation_body(outcome, parsed_action, extra)
        self.assertIn("I used 9:00am", body)

    @patch("assistant.services.google_calendar.get_google_credentials")
    @patch("assistant.services.google_calendar.build")
    def test_missing_end_time_and_reminder_recipient_does_not_block_creation(self, mock_build, mock_creds):
        """Regression test for the exact real-world case that motivated
        this change: a clear title/date/start_time, but end_time and
        reminder_recipient weren't stated. Must still post the event."""
        mock_service = MagicMock()
        mock_service.events.return_value.insert.return_value.execute.return_value = {"id": "evt-2"}
        mock_build.return_value = mock_service

        event_date = timezone.localdate() + timedelta(days=7)
        email = self._make_email(message_id="msg-defaults-2")
        parsed_action = ParsedAction.objects.create(
            incoming_email=email,
            action_type=ParsedAction.ActionType.CREATE_CALENDAR_EVENT,
            title="Words and Pictures event for Marizu at Halewood library",
            appointment_date=event_date,
            start_time=time(10, 30),
            reminder_lead_days=1,
            confidence=0.96,
            missing_fields=["end_time", "reminder_recipient"],
            ambiguity_notes=["Reminder recipient is not specified."],
        )

        outcome, extra = route_and_execute(parsed_action)

        # The event is created even though reminder details are unclear —
        # only the linked reminder is skipped, not the whole action.
        self.assertEqual(outcome, "created_reminder_unclear")
        parsed_action.refresh_from_db()
        self.assertEqual(parsed_action.status, ParsedAction.Status.EXECUTED)
        self.assertEqual(CalendarEventRecord.objects.count(), 1)

        record = CalendarEventRecord.objects.get()
        self.assertEqual(record.end_time, time(11, 30))

        body = build_confirmation_body(outcome, parsed_action, extra)
        self.assertIn("scheduled it for 1 hour", body)
        self.assertIn("could not confirm the reminder details", body)
        # start_time was given, so it must not be (mis)reported as defaulted.
        self.assertNotIn("I used 9:00am", body)


class ConfirmationReplyWordingTests(TestCase):
    """Locks in the fixed reply wording so 'the time' is never used for a
    missing end time, and reminder_recipient is never silently dropped."""

    def _make_action(self, **kwargs):
        email = IncomingEmail.objects.create(
            gmail_message_id=f"msg-wording-{id(kwargs)}",
            outer_sender="ike@example.com",
            status=IncomingEmail.Status.RECEIVED,
        )
        defaults = dict(
            incoming_email=email,
            action_type=ParsedAction.ActionType.CREATE_CALENDAR_EVENT,
            title="Test event",
            appointment_date=date(2026, 8, 11),
            status=ParsedAction.Status.PENDING_REVIEW,
        )
        defaults.update(kwargs)
        return ParsedAction.objects.create(**defaults)

    def test_pending_review_distinguishes_end_time_from_start_time(self):
        action = self._make_action(missing_fields=["end_time"])
        body = build_confirmation_body("pending_review", action)
        self.assertIn("the end time", body)
        self.assertNotIn("the time.", body)

    def test_pending_review_mentions_reminder_recipient(self):
        action = self._make_action(missing_fields=["reminder_recipient"])
        body = build_confirmation_body("pending_review", action)
        self.assertIn("who the reminder should go to", body)
