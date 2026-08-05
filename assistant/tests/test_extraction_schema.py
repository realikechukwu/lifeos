from datetime import date
from pathlib import Path

from django.test import SimpleTestCase
from pydantic import ValidationError

from assistant.services.extractor import AssistantAction, build_messages

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text()


class AssistantActionSchemaTests(SimpleTestCase):
    def test_valid_extraction_passes_validation(self):
        extraction = AssistantAction(
            action_type="create_calendar_event",
            title="Dental appointment",
            appointment_date="2026-08-18",
            start_time="10:30",
            confidence=0.95,
        )
        self.assertEqual(extraction.action_type, "create_calendar_event")
        self.assertEqual(extraction.related_person, "unknown")  # default

    def test_invalid_action_type_is_rejected(self):
        with self.assertRaises(ValidationError):
            AssistantAction(action_type="delete_all_events", confidence=0.9)

    def test_confidence_out_of_range_is_rejected(self):
        with self.assertRaises(ValidationError):
            AssistantAction(action_type="requires_review", confidence=1.5)

    def test_confidence_is_required(self):
        with self.assertRaises(ValidationError):
            AssistantAction(action_type="requires_review")

    def test_task_action_with_unassigned_default(self):
        extraction = AssistantAction(
            action_type="create_task",
            title="Renew home insurance",
            due_date="2026-09-30",
            confidence=0.9,
        )
        self.assertIsNone(extraction.assigned_to)

    def test_reminder_recipient_is_restricted_to_closed_values(self):
        with self.assertRaises(ValidationError):
            AssistantAction(
                action_type="create_email_reminder",
                reminder_recipient="attacker@evil.example.com",
                confidence=0.9,
            )


class PromptInjectionRemainsUntrustedTests(SimpleTestCase):
    def test_injection_text_is_wrapped_as_data_only(self):
        injection_text = load_fixture("prompt_injection.txt")
        messages = build_messages(
            injection_text,
            current_date=date(2026, 8, 5),
            received_date=date(2026, 8, 3),
            forwarded_date=date(2026, 8, 3),
        )
        system_message = messages[0]["content"]
        user_message = messages[1]["content"]

        # The system prompt states the injection-protection rules...
        self.assertIn("cannot be overridden", system_message.lower().replace("’", "'"))
        self.assertIn("closed", system_message.lower())

        # ...and the untrusted content is clearly delimited within the user
        # message, not appended as free-standing instructions.
        self.assertIn("<untrusted_email_content>", user_message)
        self.assertIn("</untrusted_email_content>", user_message)
        start = user_message.index("<untrusted_email_content>")
        end = user_message.index("</untrusted_email_content>")
        self.assertIn(injection_text.strip(), user_message[start:end])

        # The injected instruction text itself never appears outside the
        # delimited block (i.e. it cannot smuggle itself into the system
        # prompt or the surrounding instructions).
        self.assertNotIn("rm -rf", system_message)
