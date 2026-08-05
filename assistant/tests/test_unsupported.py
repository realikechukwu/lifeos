from django.test import TestCase, override_settings

from core.models import CalendarEventRecord, IncomingEmail, Note, ParsedAction, Reminder, Task
from assistant.services.router import route_and_execute


@override_settings(AUTHORISED_EMAIL_IKE="ike@example.com", AUTHORISED_EMAIL_WIFE="wife@example.com")
class UnsupportedActionTests(TestCase):
    def test_unsupported_action_executes_nothing(self):
        email = IncomingEmail.objects.create(
            gmail_message_id="msg-unsupported-1",
            outer_sender="ike@example.com",
            subject="Order something",
            body_text="Please order a new phone case for me.",
            status=IncomingEmail.Status.RECEIVED,
        )
        parsed_action = ParsedAction.objects.create(
            incoming_email=email,
            action_type=ParsedAction.ActionType.UNSUPPORTED,
            confidence=0.9,
        )

        outcome, extra = route_and_execute(parsed_action)

        self.assertEqual(outcome, "unsupported")
        self.assertEqual(extra, {})
        self.assertEqual(Task.objects.count(), 0)
        self.assertEqual(Note.objects.count(), 0)
        self.assertEqual(Reminder.objects.count(), 0)
        self.assertEqual(CalendarEventRecord.objects.count(), 0)
        parsed_action.refresh_from_db()
        self.assertEqual(parsed_action.status, ParsedAction.Status.PENDING_REVIEW)

    def test_requires_review_action_executes_nothing(self):
        email = IncomingEmail.objects.create(
            gmail_message_id="msg-unsupported-2",
            outer_sender="ike@example.com",
            subject="Ambiguous",
            body_text="Not sure what to do with this.",
            status=IncomingEmail.Status.RECEIVED,
        )
        parsed_action = ParsedAction.objects.create(
            incoming_email=email,
            action_type=ParsedAction.ActionType.REQUIRES_REVIEW,
            confidence=0.5,
        )

        outcome, extra = route_and_execute(parsed_action)

        self.assertEqual(outcome, "pending_review")
        self.assertEqual(extra, {})
        self.assertEqual(Task.objects.count(), 0)
