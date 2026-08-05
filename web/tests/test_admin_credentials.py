"""GoogleCredential's encrypted refresh token must never be rendered in
Django admin, and the admin must not offer a way to add one by hand."""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import GoogleCredential

User = get_user_model()


@override_settings(APP_ENCRYPTION_KEY="zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz=")
class GoogleCredentialAdminTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_superuser(username="admin", password="pw123456!", email="admin@example.com")
        self.client.force_login(self.staff)
        self.credential = GoogleCredential.objects.create(account_email="assistant@example.com")
        self.credential.encrypted_data = "super-secret-cipher-text-should-never-render"
        self.credential.save(update_fields=["encrypted_data"])

    def test_change_list_does_not_expose_encrypted_data(self):
        response = self.client.get(reverse("admin:core_googlecredential_changelist"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "super-secret-cipher-text-should-never-render")

    def test_change_form_does_not_expose_encrypted_data(self):
        response = self.client.get(
            reverse("admin:core_googlecredential_change", args=[self.credential.id])
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "super-secret-cipher-text-should-never-render")

    def test_add_permission_is_disabled(self):
        response = self.client.get(reverse("admin:core_googlecredential_add"))
        self.assertEqual(response.status_code, 403)
