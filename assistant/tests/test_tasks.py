from datetime import date

from django.test import TestCase, override_settings

from core.models import AssignedTo, IncomingEmail, ParsedAction, Task
from assistant.services.router import route_and_execute
from assistant.services.tasks import find_matching_open_tasks


@override_settings(AUTHORISED_EMAIL_IKE="ike@example.com", AUTHORISED_EMAIL_WIFE="wife@example.com")
class DirectTaskCommandTests(TestCase):
    def _make_email(self, sender="ike@example.com"):
        return IncomingEmail.objects.create(
            gmail_message_id="msg-task-1",
            outer_sender=sender,
            subject="Task request",
            body_text="Create a task for Ike to renew the home insurance by 30 September.",
            status=IncomingEmail.Status.RECEIVED,
        )

    def _make_parsed_action(self, email, **overrides):
        fields = dict(
            incoming_email=email,
            action_type=ParsedAction.ActionType.CREATE_TASK,
            title="Renew home insurance",
            due_date=date(2026, 9, 30),
            assigned_to=AssignedTo.IKE,
            confidence=0.95,
        )
        fields.update(overrides)
        return ParsedAction.objects.create(**fields)

    def test_direct_task_command_creates_a_valid_task(self):
        email = self._make_email()
        parsed_action = self._make_parsed_action(email)

        outcome, extra = route_and_execute(parsed_action)

        self.assertEqual(outcome, "task_created")
        task = extra["task"]
        self.assertEqual(task.title, "Renew home insurance")
        self.assertEqual(task.assigned_to, AssignedTo.IKE)
        self.assertEqual(task.due_date, date(2026, 9, 30))
        self.assertEqual(task.status, Task.Status.OPEN)
        self.assertEqual(task.source_email_id, email.id)
        parsed_action.refresh_from_db()
        self.assertEqual(parsed_action.status, ParsedAction.Status.EXECUTED)

    def test_task_with_no_stated_owner_becomes_unassigned(self):
        email = self._make_email()
        parsed_action = self._make_parsed_action(email, assigned_to="", title="Book the boiler service")

        outcome, extra = route_and_execute(parsed_action)

        self.assertEqual(outcome, "task_created")
        self.assertEqual(extra["task"].assigned_to, AssignedTo.UNASSIGNED)

    def test_task_does_not_require_a_due_date(self):
        email = self._make_email()
        parsed_action = self._make_parsed_action(email, due_date=None, title="Sort out the loft insulation quote")

        outcome, extra = route_and_execute(parsed_action)

        self.assertEqual(outcome, "task_created")
        self.assertIsNone(extra["task"].due_date)


@override_settings(AUTHORISED_EMAIL_IKE="ike@example.com", AUTHORISED_EMAIL_WIFE="wife@example.com")
class AmbiguousTaskCompletionTests(TestCase):
    def _make_email(self):
        return IncomingEmail.objects.create(
            gmail_message_id="msg-task-complete-1",
            outer_sender="ike@example.com",
            subject="Mark complete",
            body_text="Mark the insurance task as complete.",
            status=IncomingEmail.Status.RECEIVED,
        )

    def test_ambiguous_completion_does_not_guess(self):
        Task.objects.create(title="Renew home insurance", assigned_to=AssignedTo.IKE)
        Task.objects.create(title="Renew car insurance", assigned_to=AssignedTo.WIFE)

        email = self._make_email()
        parsed_action = ParsedAction.objects.create(
            incoming_email=email,
            action_type=ParsedAction.ActionType.MARK_TASK_COMPLETE,
            task_search_text="insurance",
            confidence=0.9,
        )

        outcome, extra = route_and_execute(parsed_action)

        self.assertEqual(outcome, "task_ambiguous")
        self.assertEqual(extra, {})
        self.assertTrue(Task.objects.filter(status=Task.Status.OPEN).count() == 2)
        parsed_action.refresh_from_db()
        self.assertEqual(parsed_action.status, ParsedAction.Status.PENDING_REVIEW)

    def test_unique_partial_match_completes_the_task(self):
        task = Task.objects.create(title="Renew home insurance", assigned_to=AssignedTo.IKE)

        email = self._make_email()
        parsed_action = ParsedAction.objects.create(
            incoming_email=email,
            action_type=ParsedAction.ActionType.MARK_TASK_COMPLETE,
            task_search_text="home insurance",
            confidence=0.9,
        )

        outcome, extra = route_and_execute(parsed_action)

        self.assertEqual(outcome, "task_completed")
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.COMPLETED)
        self.assertIsNotNone(task.completed_at)

    def test_no_matching_task_is_reported_not_guessed(self):
        email = self._make_email()
        parsed_action = ParsedAction.objects.create(
            incoming_email=email,
            action_type=ParsedAction.ActionType.MARK_TASK_COMPLETE,
            task_search_text="nonexistent task",
            confidence=0.9,
        )

        outcome, extra = route_and_execute(parsed_action)

        self.assertEqual(outcome, "task_not_found")
        parsed_action.refresh_from_db()
        self.assertEqual(parsed_action.status, ParsedAction.Status.REJECTED)


class FindMatchingOpenTasksTests(TestCase):
    def test_completed_tasks_are_excluded_from_matching(self):
        Task.objects.create(title="Renew home insurance", status=Task.Status.COMPLETED)
        self.assertEqual(find_matching_open_tasks("home insurance"), [])
