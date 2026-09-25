from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from assistant.management.commands.poll_gmail import process_incoming_email
from assistant.services.extractor import AssistantAction
from assistant.services.router import route_and_execute as real_route_and_execute
from core.models import IncomingEmail, ParsedAction, Task


@override_settings(
    AUTHORISED_EMAIL_IKE="ike@example.com",
    AUTHORISED_EMAIL_WIFE="wife@example.com",
)
class EmailBatchProcessingTests(TestCase):
    @patch("assistant.management.commands.poll_gmail.route_and_execute")
    @patch("assistant.management.commands.poll_gmail.extract_actions")
    def test_one_item_failure_does_not_discard_successful_items(self, extract, route):
        extract.return_value = [
            AssistantAction(
                action_type="create_task",
                title="Buy milk",
                assigned_to="ike",
                confidence=0.55,
                ambiguity_notes=["Milk type was not stated."],
            ),
            AssistantAction(
                action_type="create_task",
                title="Broken item",
                assigned_to="ike",
                confidence=0.9,
                notify_both=True,
            ),
        ]

        def execute_item(parsed_action):
            if parsed_action.title == "Broken item":
                raise RuntimeError("isolated test failure")
            return real_route_and_execute(parsed_action)

        route.side_effect = execute_item
        incoming = IncomingEmail.objects.create(
            gmail_message_id="batch-email-1",
            gmail_thread_id="batch-thread-1",
            outer_sender="ike@example.com",
            subject="Two things",
            body_text="Buy milk and do the broken item",
        )
        gmail = MagicMock()
        gmail.send_reply.return_value = "reply-id"

        process_incoming_email(incoming, gmail_service=gmail, label_map={})

        incoming.refresh_from_db()
        self.assertEqual(incoming.status, IncomingEmail.Status.PENDING_REVIEW)
        self.assertTrue(Task.objects.filter(title="Buy milk").exists())
        self.assertEqual(
            list(incoming.parsed_actions.values_list("status", flat=True)),
            [ParsedAction.Status.FAILED, ParsedAction.Status.EXECUTED],
        )
        gmail.send_reply.assert_called_once()
        body = gmail.send_reply.call_args.kwargs["body_text"]
        self.assertIn("I handled 2 items", body)
        self.assertIn('created a task for Ike: "Buy milk"', body)
        self.assertIn('could not complete "Broken item"', body)
        self.assertEqual(gmail.send_reply.call_args.kwargs["cc_addr"], "wife@example.com")
