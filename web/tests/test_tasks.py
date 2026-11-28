"""Task creation and completion through the web interface. No OpenAI/email
pipeline is invoked — plain ModelForm creation, shared completion service."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import AssignedTo, AuditLog, Task

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

    def test_new_task_form_has_no_status_field(self):
        response = self.client.get(reverse("web:task_create"))
        self.assertNotIn("status", response.context["form"].fields)
        task = Task.objects.create(title="Existing")
        response = self.client.get(reverse("web:task_edit", args=[task.id]))
        self.assertIn("status", response.context["form"].fields)


FRAGMENT = {"HTTP_X_FRAGMENT": "1"}


class QuickAddTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="member", password="pw123456!")
        self.client.force_login(self.user)

    def test_quick_add_creates_open_task_and_returns_to_page(self):
        response = self.client.post(reverse("web:task_quick_add"), {
            "title": "  Buy stamps ", "due_date": "", "assigned_to": AssignedTo.WIFE, "next": "/notes/",
        })
        self.assertRedirects(response, "/notes/", fetch_redirect_response=False)
        task = Task.objects.get()
        self.assertEqual(task.title, "Buy stamps")
        self.assertEqual(task.status, Task.Status.OPEN)
        self.assertEqual(task.assigned_to, AssignedTo.WIFE)
        self.assertTrue(AuditLog.objects.filter(action="task_created_via_web", object_id=str(task.id)).exists())

    def test_quick_add_defaults_to_unassigned(self):
        self.client.post(reverse("web:task_quick_add"), {"title": "Anyone can do this"})
        self.assertEqual(Task.objects.get().assigned_to, AssignedTo.UNASSIGNED)

    def test_quick_add_needs_a_title(self):
        response = self.client.post(reverse("web:task_quick_add"), {"title": "   "})
        self.assertRedirects(response, reverse("web:task_list"))
        self.assertFalse(Task.objects.exists())

    def test_quick_add_ignores_an_outside_next_url(self):
        response = self.client.post(reverse("web:task_quick_add"), {
            "title": "Safe", "next": "https://example.com/phish",
        })
        self.assertRedirects(response, reverse("web:task_list"))

    def test_quick_add_fragment_returns_the_list_with_the_new_task(self):
        response = self.client.post(reverse("web:task_quick_add"), {
            "title": "Order prescription", "list_query": "show=todo",
        }, **FRAGMENT)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "web/_task_results.html")
        self.assertTemplateNotUsed(response, "base.html")
        self.assertContains(response, "Order prescription")
        self.assertIn("Order%20prescription", response["X-Toast"])


class TickCircleTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="member", password="pw123456!")
        self.client.force_login(self.user)

    def test_complete_fragment_returns_the_ticked_row(self):
        task = Task.objects.create(title="Water the plants")
        response = self.client.post(reverse("web:task_complete", args=[task.id]), {"next": "/"}, **FRAGMENT)
        self.assertTemplateUsed(response, "web/_task_row.html")
        self.assertContains(response, 'aria-pressed="true"')
        self.assertContains(response, reverse("web:task_reopen", args=[task.id]))
        self.assertIn("done", response["X-Toast"])

    def test_reopen_puts_a_completed_task_back(self):
        task = Task.objects.create(title="Oops", status=Task.Status.COMPLETED, completed_at=timezone.now())
        response = self.client.post(reverse("web:task_reopen", args=[task.id]))
        self.assertRedirects(response, reverse("web:task_list"))
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.OPEN)
        self.assertIsNone(task.completed_at)
        self.assertTrue(AuditLog.objects.filter(action="task_reopened_via_web", object_id=str(task.id)).exists())

    def test_reopen_fragment_returns_an_unticked_row(self):
        task = Task.objects.create(title="Oops", status=Task.Status.COMPLETED, completed_at=timezone.now())
        response = self.client.post(reverse("web:task_reopen", args=[task.id]), **FRAGMENT)
        self.assertContains(response, 'aria-pressed="false"')

    def test_reopen_does_nothing_to_an_open_task(self):
        task = Task.objects.create(title="Already open")
        self.client.post(reverse("web:task_reopen", args=[task.id]))
        self.assertFalse(AuditLog.objects.filter(action="task_reopened_via_web").exists())

    def test_reopen_requires_post(self):
        task = Task.objects.create(title="Done", status=Task.Status.COMPLETED)
        self.assertEqual(self.client.get(reverse("web:task_reopen", args=[task.id])).status_code, 405)


class TaskListFilterTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="member", password="pw123456!")
        self.client.force_login(self.user)
        today = timezone.localdate()
        self.open = Task.objects.create(title="Open soon", due_date=today + timedelta(days=2))
        self.late = Task.objects.create(title="Late one", due_date=today - timedelta(days=3), assigned_to=AssignedTo.IKE)
        self.done = Task.objects.create(title="Finished", status=Task.Status.COMPLETED, completed_at=timezone.now())
        self.cancelled = Task.objects.create(title="Dropped", status=Task.Status.CANCELLED)

    def titles(self, response):
        return [task.title for task in response.context["tasks"]]

    def test_default_shows_open_tasks_soonest_first(self):
        response = self.client.get(reverse("web:task_list"))
        self.assertEqual(self.titles(response), ["Late one", "Open soon"])

    def test_done_view_shows_completed_tasks(self):
        response = self.client.get(reverse("web:task_list"), {"show": "done"})
        self.assertEqual(self.titles(response), ["Finished"])

    def test_all_view_includes_every_status(self):
        response = self.client.get(reverse("web:task_list"), {"show": "all"})
        self.assertEqual(set(self.titles(response)), {"Open soon", "Late one", "Finished", "Dropped"})

    def test_older_overdue_and_status_links_still_work(self):
        response = self.client.get(reverse("web:task_list"), {"overdue": "1"})
        self.assertEqual(self.titles(response), ["Late one"])
        response = self.client.get(reverse("web:task_list"), {"status": Task.Status.CANCELLED})
        self.assertEqual(self.titles(response), ["Dropped"])

    def test_person_filter(self):
        response = self.client.get(reverse("web:task_list"), {"assigned_to": AssignedTo.IKE})
        self.assertEqual(self.titles(response), ["Late one"])

    def test_search_fragment_returns_only_the_results(self):
        response = self.client.get(reverse("web:task_list"), {"q": "late"}, **FRAGMENT)
        self.assertTemplateUsed(response, "web/_task_results.html")
        self.assertTemplateNotUsed(response, "base.html")
        self.assertEqual(self.titles(response), ["Late one"])

    def test_highlighted_task_is_marked(self):
        response = self.client.get(reverse("web:task_list"), {"highlight": self.open.id})
        self.assertContains(response, f'is-highlighted" id="task-{self.open.id}"')
