"""Every family page (and the calendar JSON endpoint) requires
authentication; anonymous visitors are redirected to login."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

User = get_user_model()

FAMILY_PAGE_URL_NAMES = [
    "web:upcoming",
    "web:calendar",
    "web:task_list",
    "web:task_create",
    "web:note_list",
    "web:note_create",
    "web:review_list",
]


class AnonymousAccessTests(TestCase):
    def test_family_pages_redirect_anonymous_users_to_login(self):
        for url_name in FAMILY_PAGE_URL_NAMES:
            with self.subTest(url_name=url_name):
                response = self.client.get(reverse(url_name))
                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse("login"), response.url)

    def test_calendar_json_endpoint_requires_authentication(self):
        response = self.client.get(reverse("web:calendar_events_json"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)


class AuthenticatedAccessTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="member", password="pw123456!", is_active=True)

    def test_authenticated_user_can_reach_upcoming(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("web:upcoming"))
        self.assertEqual(response.status_code, 200)

    def test_authenticated_user_can_reach_calendar_json(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("web:calendar_events_json"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")

    def test_root_url_is_the_upcoming_page(self):
        self.client.force_login(self.user)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "web/upcoming.html")

    def test_admin_is_refused_for_non_staff_user(self):
        self.client.force_login(self.user)
        response = self.client.get("/admin/")
        # Django admin redirects non-staff users to its own login page.
        self.assertEqual(response.status_code, 302)
