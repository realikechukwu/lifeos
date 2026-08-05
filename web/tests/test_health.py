"""The health endpoint is unauthenticated, fast, and never calls Gmail,
Google Calendar, or OpenAI."""

from unittest.mock import patch

from django.db import DatabaseError
from django.test import TestCase
from django.urls import reverse


class HealthEndpointTests(TestCase):
    def test_health_ok(self):
        response = self.client.get(reverse("web:health"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "database": "ok"})

    def test_health_reports_503_when_database_is_unreachable(self):
        with patch("web.views.connection") as mock_connection:
            mock_connection.cursor.side_effect = DatabaseError("boom")
            response = self.client.get(reverse("web:health"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "error")

    def test_healthz_still_works(self):
        response = self.client.get(reverse("web:healthz"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_health_does_not_require_login(self):
        response = self.client.get("/health/")
        self.assertEqual(response.status_code, 200)
