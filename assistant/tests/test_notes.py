from django.test import TestCase, override_settings

from core.models import IncomingEmail, Note, ParsedAction
from assistant.services.router import route_and_execute


@override_settings(AUTHORISED_EMAIL_IKE="ike@example.com", AUTHORISED_EMAIL_WIFE="wife@example.com")
class NoteCommandTests(TestCase):
    def _make_email(self):
        return IncomingEmail.objects.create(
            gmail_message_id="msg-note-1",
            outer_sender="wife@example.com",
            subject="Note",
            body_text="Make a note that the electrician still needs to repair the kitchen socket.",
            status=IncomingEmail.Status.RECEIVED,
        )

    def test_note_command_creates_a_note(self):
        email = self._make_email()
        parsed_action = ParsedAction.objects.create(
            incoming_email=email,
            action_type=ParsedAction.ActionType.CREATE_NOTE,
            title="Kitchen socket repair",
            description="The electrician still needs to repair the kitchen socket.",
            note_category="household",
            confidence=0.95,
        )

        outcome, extra = route_and_execute(parsed_action)

        self.assertEqual(outcome, "note_created")
        note = extra["note"]
        self.assertIsInstance(note, Note)
        self.assertEqual(note.title, "Kitchen socket repair")
        self.assertEqual(note.category, "household")
        self.assertEqual(note.source_email_id, email.id)
        parsed_action.refresh_from_db()
        self.assertEqual(parsed_action.status, ParsedAction.Status.EXECUTED)

    def test_note_without_title_gets_a_generated_title(self):
        email = self._make_email()
        parsed_action = ParsedAction.objects.create(
            incoming_email=email,
            action_type=ParsedAction.ActionType.CREATE_NOTE,
            title="",
            description="The electrician still needs to repair the kitchen socket.",
            confidence=0.9,
        )

        outcome, extra = route_and_execute(parsed_action)

        self.assertEqual(outcome, "note_created")
        self.assertTrue(extra["note"].title)

    def test_note_with_no_body_or_title_is_deferred_for_review(self):
        email = self._make_email()
        parsed_action = ParsedAction.objects.create(
            incoming_email=email,
            action_type=ParsedAction.ActionType.CREATE_NOTE,
            title="",
            description="",
            confidence=0.95,
        )

        outcome, extra = route_and_execute(parsed_action)

        self.assertEqual(outcome, "pending_review")
        self.assertEqual(Note.objects.count(), 0)
