from datetime import date
from pathlib import Path

from django.test import SimpleTestCase
from pydantic import ValidationError

from assistant.services.extractor import (
    AssistantAction,
    AssistantActionBatch,
    build_messages,
    normalise_action_inferences,
)

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
        self.assertFalse(extraction.notify_both)  # default: never guess a CC

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

    def test_recurrence_fields_default_to_null(self):
        extraction = AssistantAction(
            action_type="create_calendar_event",
            title="Dental appointment",
            appointment_date="2026-08-18",
            confidence=0.95,
        )
        self.assertIsNone(extraction.recurrence_frequency)
        self.assertIsNone(extraction.recurrence_interval)
        self.assertIsNone(extraction.recurrence_days_of_week)
        self.assertIsNone(extraction.recurrence_until)
        self.assertIsNone(extraction.recurrence_count)

    def test_recurrence_frequency_is_restricted_to_closed_values(self):
        with self.assertRaises(ValidationError):
            AssistantAction(
                action_type="create_calendar_event",
                title="Bin collection",
                appointment_date="2026-08-18",
                recurrence_frequency="fortnightly",
                confidence=0.9,
            )

    def test_recurrence_days_of_week_are_restricted_to_closed_values(self):
        with self.assertRaises(ValidationError):
            AssistantAction(
                action_type="create_calendar_event",
                title="Bin collection",
                appointment_date="2026-08-18",
                recurrence_frequency="weekly",
                recurrence_days_of_week=["Mon"],
                confidence=0.9,
            )

    def test_valid_recurrence_fields_are_accepted(self):
        extraction = AssistantAction(
            action_type="create_calendar_event",
            title="Swimming lessons",
            appointment_date="2026-08-18",
            recurrence_frequency="weekly",
            recurrence_interval=2,
            recurrence_days_of_week=["MO", "WE"],
            recurrence_until="2026-12-15",
            confidence=0.9,
        )
        self.assertEqual(extraction.recurrence_frequency, "weekly")
        self.assertEqual(extraction.recurrence_interval, 2)
        self.assertEqual(extraction.recurrence_days_of_week, ["MO", "WE"])
        self.assertEqual(extraction.recurrence_until, "2026-12-15")

    def test_batch_accepts_multiple_actions_but_is_bounded(self):
        action = AssistantAction(action_type="create_task", title="Buy milk", confidence=0.9)
        self.assertEqual(len(AssistantActionBatch(actions=[action, action]).actions), 2)
        with self.assertRaises(ValidationError):
            AssistantActionBatch(actions=[])
        with self.assertRaises(ValidationError):
            AssistantActionBatch(actions=[action] * 11)

    def test_weekly_recurrence_gets_a_future_anchor_and_default_time(self):
        action = AssistantAction(
            action_type="create_calendar_event",
            title="Swimming",
            recurrence_frequency="weekly",
            recurrence_days_of_week=["TU"],
            confidence=0.8,
            missing_fields=["appointment_date", "start_time"],
        )
        normalised = normalise_action_inferences(action, date(2026, 9, 21))
        self.assertEqual(normalised.appointment_date, "2026-09-22")
        self.assertEqual(normalised.start_time, "09:00")
        self.assertNotIn("appointment_date", normalised.missing_fields)
        self.assertNotIn("start_time", normalised.missing_fields)


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
        self.assertNotIn("exactly one", system_message.lower())
        self.assertIn("every distinct requested action", system_message.lower())
