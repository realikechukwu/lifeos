"""The Upcoming feed combines calendar events, task due dates (including
overdue), pending reminders, and pending-review actions."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import (
    AssignedTo,
    CalendarEventRecord,
    IncomingEmail,
    ParsedAction,
    RecipientTarget,
    Reminder,
    Task,
)

User = get_user_model()


class UpcomingFeedTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="member", password="pw123456!")
        self.client.force_login(self.user)

        today = timezone.localdate()

        self.email = IncomingEmail.objects.create(
            gmail_message_id="msg-1", outer_sender="ike@example.com", subject="Test",
        )
        self.parsed_action = ParsedAction.objects.create(
            incoming_email=self.email,
            action_type=ParsedAction.ActionType.CREATE_TASK,
            title="Needs a human",
            status=ParsedAction.Status.PENDING_REVIEW,
        )

        self.calendar_event = CalendarEventRecord.objects.create(
            parsed_action=self.parsed_action,
            incoming_email=self.email,
            google_event_id="evt-1",
            calendar_id="cal-1",
            title="Dentist",
            appointment_date=today + timedelta(days=3),
        )

        self.overdue_task = Task.objects.create(
            title="Overdue chore", assigned_to=AssignedTo.IKE, status=Task.Status.OPEN,
            due_date=today - timedelta(days=2),
        )
        self.upcoming_task = Task.objects.create(
            title="Future chore", assigned_to=AssignedTo.WIFE, status=Task.Status.OPEN,
            due_date=today + timedelta(days=5),
        )
        self.far_task = Task.objects.create(
            title="Far future chore", assigned_to=AssignedTo.WIFE, status=Task.Status.OPEN,
            due_date=today + timedelta(days=60),
        )

        self.reminder = Reminder.objects.create(
            title="Pay the bill", recipient=RecipientTarget.IKE,
            reminder_date=today + timedelta(days=1), reminder_time=timezone.now().time(),
        )

    def test_upcoming_combines_expected_item_kinds(self):
        response = self.client.get(reverse("web:upcoming"))
        self.assertEqual(response.status_code, 200)

        kinds = {item.kind for item in response.context["items"]}
        self.assertEqual(
            kinds,
            {"calendar_event", "task_due", "task_overdue", "reminder", "review"},
        )

    def test_far_future_task_is_excluded_from_the_30_day_window(self):
        response = self.client.get(reverse("web:upcoming"))
        titles = [item.title for item in response.context["items"]]
        self.assertNotIn("Far future chore", titles)

    def test_overdue_tasks_are_sorted_to_the_top(self):
        response = self.client.get(reverse("web:upcoming"))
        items = response.context["items"]
        self.assertTrue(items[0].is_overdue)
        self.assertEqual(items[0].title, "Overdue chore")
