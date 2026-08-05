from datetime import date, time, timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import IncomingEmail, ParsedAction, Reminder
from assistant.services.reminders import evaluate_reminder_fields, resolve_recipient_emails
from assistant.services.router import route_and_execute
from assistant.management.commands.send_due_reminders import claim_due_reminders


def _future_date_time():
    """A date/time comfortably in the future, so gate/claim tests are not
    time-of-day-dependent or close to flaking near a real-clock boundary."""
    when = timezone.now() + timedelta(days=2)
    return when.date(), time(9, 0)


class ReminderFieldValidationTests(TestCase):
    def test_missing_date_or_time_is_never_invented(self):
        ok, reasons = evaluate_reminder_fields(None, time(19, 0), "both", 0.95, False)
        self.assertFalse(ok)
        self.assertTrue(any("date" in r for r in reasons))

        ok, reasons = evaluate_reminder_fields(date(2099, 1, 1), None, "both", 0.95, False)
        self.assertFalse(ok)
        self.assertTrue(any("time" in r for r in reasons))

    def test_past_date_time_fails_the_future_check(self):
        past_date = (timezone.now() - timedelta(days=1)).date()
        ok, reasons = evaluate_reminder_fields(past_date, time(9, 0), "ike", 0.95, False)
        self.assertFalse(ok)
        self.assertTrue(any("future" in r for r in reasons))

    def test_valid_future_reminder_passes(self):
        reminder_date, reminder_time = _future_date_time()
        ok, reasons = evaluate_reminder_fields(reminder_date, reminder_time, "both", 0.95, False)
        self.assertTrue(ok, reasons)


@override_settings(AUTHORISED_EMAIL_IKE="ike@example.com", AUTHORISED_EMAIL_WIFE="wife@example.com")
class RecipientResolutionTests(TestCase):
    def test_ike_resolves_to_the_authorised_ike_address_only(self):
        self.assertEqual(resolve_recipient_emails("ike"), ["ike@example.com"])

    def test_both_resolves_to_both_authorised_addresses(self):
        self.assertEqual(sorted(resolve_recipient_emails("both")), ["ike@example.com", "wife@example.com"])

    def test_unrecognised_target_resolves_to_nothing(self):
        # Never derived from arbitrary email-body text — only ike/wife/both.
        self.assertEqual(resolve_recipient_emails("attacker@evil.example.com"), [])


@override_settings(AUTHORISED_EMAIL_IKE="ike@example.com", AUTHORISED_EMAIL_WIFE="wife@example.com")
class ReminderCommandTests(TestCase):
    def _make_email(self):
        return IncomingEmail.objects.create(
            gmail_message_id="msg-reminder-1",
            outer_sender="ike@example.com",
            subject="Reminder",
            body_text="Remind both of us on Friday at 7pm to put the bins out.",
            status=IncomingEmail.Status.RECEIVED,
        )

    def test_reminder_for_ike_is_created(self):
        reminder_date, reminder_time = _future_date_time()
        email = self._make_email()
        parsed_action = ParsedAction.objects.create(
            incoming_email=email,
            action_type=ParsedAction.ActionType.CREATE_EMAIL_REMINDER,
            title="Put the bins out",
            reminder_date=reminder_date,
            reminder_time=reminder_time,
            reminder_recipient="ike",
            confidence=0.95,
        )

        outcome, extra = route_and_execute(parsed_action)

        self.assertEqual(outcome, "reminder_created")
        reminder = extra["reminder"]
        self.assertEqual(reminder.recipient, "ike")
        self.assertEqual(reminder.status, Reminder.Status.PENDING)

    def test_reminder_with_unclear_time_is_deferred_not_guessed(self):
        email = self._make_email()
        parsed_action = ParsedAction.objects.create(
            incoming_email=email,
            action_type=ParsedAction.ActionType.CREATE_EMAIL_REMINDER,
            title="Put the bins out",
            reminder_date=(timezone.now() + timedelta(days=2)).date(),
            reminder_time=None,  # "Friday evening" - no exact time extracted
            reminder_recipient="both",
            confidence=0.9,
        )

        outcome, extra = route_and_execute(parsed_action)

        self.assertEqual(outcome, "pending_review")
        self.assertEqual(Reminder.objects.count(), 0)


class ReminderClaimingTests(TestCase):
    def _make_reminder(self, status=Reminder.Status.PENDING, when_offset=timedelta(minutes=-5)):
        when = timezone.now() + when_offset
        return Reminder.objects.create(
            title="Put the bins out",
            recipient="both",
            reminder_date=when.date(),
            reminder_time=when.time().replace(microsecond=0),
            status=status,
        )

    def test_claiming_moves_pending_due_reminders_to_processing(self):
        reminder = self._make_reminder()
        claimed = claim_due_reminders()

        self.assertEqual(len(claimed), 1)
        reminder.refresh_from_db()
        self.assertEqual(reminder.status, Reminder.Status.PROCESSING)
        self.assertIsNotNone(reminder.claimed_at)
        self.assertIsNotNone(reminder.send_key)
        self.assertTrue(reminder.rfc_message_id)

    def test_not_yet_due_reminder_is_not_claimed(self):
        self._make_reminder(when_offset=timedelta(days=1))
        claimed = claim_due_reminders()
        self.assertEqual(len(claimed), 0)

    def test_a_sent_reminder_is_never_claimed_again(self):
        self._make_reminder(status=Reminder.Status.SENT)
        claimed = claim_due_reminders()
        self.assertEqual(len(claimed), 0)
