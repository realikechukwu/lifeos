"""Note deletion through the web interface."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import AuditLog, Note

User = get_user_model()


class NoteDeleteTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="member", password="pw123456!")
        self.client.force_login(self.user)

    def test_delete_note(self):
        note = Note.objects.create(title="Wifi password", body="on the router", category="home")
        response = self.client.post(reverse("web:note_delete", args=[note.id]))
        self.assertRedirects(response, reverse("web:note_list"))
        self.assertFalse(Note.objects.filter(pk=note.pk).exists())

    def test_delete_keeps_text_in_audit_log(self):
        note = Note.objects.create(title="Gate code", body="4821")
        self.client.post(reverse("web:note_delete", args=[note.id]))
        log = AuditLog.objects.get(action="note_deleted_via_web")
        self.assertEqual(log.object_id, str(note.id))
        self.assertEqual(log.details["title"], "Gate code")
        self.assertEqual(log.details["body"], "4821")
        self.assertEqual(log.details["deleted_by"], "member")

    def test_delete_requires_post(self):
        note = Note.objects.create(title="GET should not delete this")
        response = self.client.get(reverse("web:note_delete", args=[note.id]))
        self.assertEqual(response.status_code, 405)
        self.assertTrue(Note.objects.filter(pk=note.pk).exists())

    def test_delete_requires_login(self):
        note = Note.objects.create(title="Anonymous can't delete")
        self.client.logout()
        response = self.client.post(reverse("web:note_delete", args=[note.id]))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])
        self.assertTrue(Note.objects.filter(pk=note.pk).exists())

    def test_delete_missing_note_is_404(self):
        response = self.client.post(reverse("web:note_delete", args=[999999]))
        self.assertEqual(response.status_code, 404)

    def test_detail_and_edit_pages_show_delete_button(self):
        note = Note.objects.create(title="Has a delete button")
        delete_url = reverse("web:note_delete", args=[note.id])
        for page in ("web:note_detail", "web:note_edit"):
            response = self.client.get(reverse(page, args=[note.id]))
            self.assertContains(response, f'action="{delete_url}"')

    def test_create_page_has_no_delete_button(self):
        response = self.client.get(reverse("web:note_create"))
        self.assertNotContains(response, "Delete note")
