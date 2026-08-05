"""Production settings must refuse to load with an empty/default secret key,
or with DATABASE_URL / DJANGO_ALLOWED_HOSTS missing. Runs config.settings.
production in an isolated subprocess so it never contaminates the already-
loaded test settings module."""

import os
import subprocess
import sys

from django.test import SimpleTestCase

CHECK_CODE = "import django; django.setup()"


def _run_with_env(extra_env: dict) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update({
        "DJANGO_SETTINGS_MODULE": "config.settings.production",
        "DJANGO_SECRET_KEY": "a-real-looking-secret-key-1234567890",
        "DJANGO_ALLOWED_HOSTS": "example.com",
        "DATABASE_URL": "sqlite:///:memory:",
    })
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", CHECK_CODE], env=env, capture_output=True, text=True, timeout=30,
    )


class ProductionSettingsTests(SimpleTestCase):
    databases = []

    def test_refuses_to_start_with_empty_secret_key(self):
        result = _run_with_env({"DJANGO_SECRET_KEY": ""})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ImproperlyConfigured", result.stderr)
        self.assertIn("DJANGO_SECRET_KEY", result.stderr)

    def test_refuses_to_start_with_the_dev_placeholder_secret_key(self):
        result = _run_with_env({"DJANGO_SECRET_KEY": "django-insecure-dev-only-change-me"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ImproperlyConfigured", result.stderr)

    def test_refuses_to_start_without_allowed_hosts(self):
        result = _run_with_env({"DJANGO_ALLOWED_HOSTS": ""})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DJANGO_ALLOWED_HOSTS", result.stderr)

    def test_refuses_to_start_without_database_url(self):
        result = _run_with_env({"DATABASE_URL": ""})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DATABASE_URL", result.stderr)

    def test_starts_with_all_required_settings_present(self):
        result = _run_with_env({})
        self.assertEqual(result.returncode, 0, result.stderr)
