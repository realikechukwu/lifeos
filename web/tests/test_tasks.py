"""Task creation and completion through the web interface. No OpenAI/email
pipeline is invoked — plain ModelForm creation, shared completion service."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import AssignedTo, Task

User = get_user_model()


class TaskWorkflowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="member", password="pw123456!")
        self.client.force_login(self.user)

    def test_create_task_via_web_form(self):
        response = self.client.post(reverse("web:task_create"), {
            "title": "Book the dentist",
            "description": "",
            "assigned_to": AssignedTo.IKE,
            "status": Task.Status.OPEN,
            "priority": Task.Priority.NORMAL,
            "due_date": "",
            "due_time": "",
        })
        self.assertRedirects(response, reverse("web:task_list"))
        task = Task.objects.get(title="Book the dentist")
        self.assertEqual(task.assigned_to, AssignedTo.IKE)
        self.assertEqual(task.status, Task.Status.OPEN)

    def test_create_task_without_title_is_rejected(self):
        response = self.client.post(reverse("web:task_create"), {
            "title": "",
            "assigned_to": AssignedTo.UNASSIGNED,
            "status": Task.Status.OPEN,
            "priority": Task.Priority.NORMAL,
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Task.objects.exists())
        self.assertFormError(response.context["form"], "title", "This field is required.")

    def test_complete_task(self):
        task = Task.objects.create(title="Water the plants", assigned_to=AssignedTo.BOTH)
        response = self.client.post(reverse("web:task_complete", args=[task.id]))
        self.assertRedirects(response, reverse("web:task_list"))
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.COMPLETED)
        self.assertIsNotNone(task.completed_at)

    def test_cancel_task(self):
        task = Task.objects.create(title="Cancel me", assigned_to=AssignedTo.BOTH)
        response = self.client.post(reverse("web:task_cancel", args=[task.id]))
        self.assertRedirects(response, reverse("web:task_list"))
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.CANCELLED)

    def test_complete_requires_post(self):
        task = Task.objects.create(title="GET should not complete this", assigned_to=AssignedTo.BOTH)
        response = self.client.get(reverse("web:task_complete", args=[task.id]))
        self.assertEqual(response.status_code, 405)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.OPEN)
