"""Only staff users may approve/reject pending ParsedAction records; the
same underlying `admin_approve_and_execute` service Django admin uses is
called — no duplicated creation logic in the view. Uses a create_task
action so no external API (Google Calendar) is touched."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import AssignedTo, IncomingEmail, ParsedAction, Task

User = get_user_model()


def _make_pending_task_action():
    email = IncomingEmail.objects.create(
        gmail_message_id="msg-review-1", outer_sender="ike@example.com", subject="Please add a task",
    )
    return ParsedAction.objects.create(
        incoming_email=email,
        action_type=ParsedAction.ActionType.CREATE_TASK,
        title="Renew passport",
        assigned_to=AssignedTo.IKE,
        status=ParsedAction.Status.PENDING_REVIEW,
        confidence=0.5,
    )


class ReviewListVisibilityTests(TestCase):
    def setUp(self):
        self.non_staff = User.objects.create_user(username="member", password="pw123456!", is_staff=False)
        self.staff = User.objects.create_user(username="staffer", password="pw123456!", is_staff=True)
        self.action = _make_pending_task_action()

    def test_non_staff_sees_only_a_count(self):
        self.client.force_login(self.non_staff)
        response = self.client.get(reverse("web:review_list"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["is_staff"])
        self.assertEqual(response.context["pending_count"], 1)
        self.assertNotContains(response, "Renew passport")

    def test_staff_sees_full_list(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("web:review_list"))
        self.assertContains(response, "Please add a task")  # the source email's subject
        self.assertContains(response, "Create task")

    def test_non_staff_cannot_open_review_detail(self):
        self.client.force_login(self.non_staff)
        response = self.client.get(reverse("web:review_detail", args=[self.action.id]))
        self.assertRedirects(response, reverse("web:review_list"))
        self.action.refresh_from_db()
        self.assertEqual(self.action.status, ParsedAction.Status.PENDING_REVIEW)

    def test_anonymous_user_cannot_open_review_detail(self):
        response = self.client.get(reverse("web:review_detail", args=[self.action.id]))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)


class ReviewApprovalTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(username="staffer", password="pw123456!", is_staff=True)
        self.action = _make_pending_task_action()
        self.client.force_login(self.staff)

    def test_staff_can_approve_and_it_creates_the_task(self):
        response = self.client.post(reverse("web:review_detail", args=[self.action.id]), {
            "intent": "approve",
            "title": "Renew passport",
            "description": "",
            "all_day": "on",
            "assigned_to": AssignedTo.IKE,
        })
        self.assertRedirects(response, reverse("web:review_list"))
        self.action.refresh_from_db()
        self.assertEqual(self.action.status, ParsedAction.Status.EXECUTED)
        self.assertTrue(Task.objects.filter(title="Renew passport").exists())

    def test_staff_can_reject(self):
        response = self.client.post(
            reverse("web:review_detail", args=[self.action.id]), {"intent": "reject"}
        )
        self.assertRedirects(response, reverse("web:review_list"))
        self.action.refresh_from_db()
        self.assertEqual(self.action.status, ParsedAction.Status.REJECTED)
        self.assertEqual(self.action.reviewed_by, self.staff)
        self.assertFalse(Task.objects.exists())

    def test_staff_can_save_edits_without_approving(self):
        response = self.client.post(reverse("web:review_detail", args=[self.action.id]), {
            "intent": "save",
            "title": "Renew passport urgently",
            "description": "",
            "all_day": "on",
            "assigned_to": AssignedTo.IKE,
        })
        self.assertRedirects(response, reverse("web:review_detail", args=[self.action.id]))
        self.action.refresh_from_db()
        self.assertEqual(self.action.title, "Renew passport urgently")
        self.assertEqual(self.action.status, ParsedAction.Status.PENDING_REVIEW)
        self.assertFalse(Task.objects.exists())
