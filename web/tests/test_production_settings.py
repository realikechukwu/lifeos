"""Production settings must refuse to load with an empty/default secret key,
or with DATABASE_URL / DJANGO_ALLOWED_HOSTS missing. Runs config.settings.
production in an isolated subprocess so it never contaminates the already-
loaded test settings module."""

import os
import subprocess
import sys

from django.test import SimpleTestCase

CHECK_CODE = "import django; django.setup()"

# Static files must be served through a *manifest* storage. Without one,
# collectstatic copies files into STATIC_ROOT without rehashing them or
# rewriting staticfiles.json, so {% static %} keeps resolving to whatever the
# previous manifest recorded — the app serves a stale stylesheet and nothing
# errors. Reading manifest_name off the resolved storage (rather than just
# string-matching the setting) proves the backend imports and really is one.
MANIFEST_CHECK_CODE = """
import django
django.setup()
from django.conf import settings
from django.contrib.staticfiles.storage import staticfiles_storage
print(settings.STORAGES["staticfiles"]["BACKEND"])
print(staticfiles_storage.manifest_name)
"""


def _run_with_env(extra_env: dict, code: str = CHECK_CODE) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update({
        "DJANGO_SETTINGS_MODULE": "config.settings.production",
        "DJANGO_SECRET_KEY": "a-real-looking-secret-key-1234567890",
        "DJANGO_ALLOWED_HOSTS": "example.com",
        "DATABASE_URL": "sqlite:///:memory:",
    })
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30,
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

    def test_static_files_use_a_hashing_manifest_storage(self):
        result = _run_with_env({}, code=MANIFEST_CHECK_CODE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "whitenoise.storage.CompressedManifestStaticFilesStorage", result.stdout
        )
        self.assertIn("staticfiles.json", result.stdout)
