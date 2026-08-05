"""Production settings — used on Oracle via
`DJANGO_SETTINGS_MODULE=config.settings.production` (set in the systemd
unit's environment, not in `.env`; see deploy/systemd/*). Deliberately
fails loudly on missing/insecure configuration rather than silently
falling back to an unsafe default.
"""

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F401,F403
from .base import BASE_DIR, SECRET_KEY, _bool_env, _list_env, os

DEBUG = False

# --- Secret key -------------------------------------------------------------
# Refuse to start with no secret key, or with the development placeholder —
# both would silently disable Django's cryptographic signing protections.
if not SECRET_KEY or SECRET_KEY == "django-insecure-dev-only-change-me":
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY must be set to a real secret value in production. "
        "Generate one with: "
        "python -c \"from django.core.management.utils import get_random_secret_key; "
        "print(get_random_secret_key())\""
    )

# --- Hosts / CSRF -------------------------------------------------------------
ALLOWED_HOSTS = _list_env("DJANGO_ALLOWED_HOSTS")
if not ALLOWED_HOSTS:
    raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS must be set in production.")

CSRF_TRUSTED_ORIGINS = _list_env("DJANGO_CSRF_TRUSTED_ORIGINS")

# --- HTTPS / proxy -------------------------------------------------------------
# Nginx terminates TLS and forwards to Gunicorn over a Unix socket / localhost;
# tell Django to trust the X-Forwarded-Proto header it sets.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

SECURE_SSL_REDIRECT = _bool_env("DJANGO_SECURE_SSL_REDIRECT", True)

SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_HSTS_SECONDS", "3600"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = _bool_env("DJANGO_HSTS_INCLUDE_SUBDOMAINS", True)
SECURE_HSTS_PRELOAD = _bool_env("DJANGO_HSTS_PRELOAD", False)

# --- Cookies -------------------------------------------------------------
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = False  # Django's CSRF cookie must stay readable by the JS that sets the header.
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# --- Static files (WhiteNoise) -------------------------------------------------------------
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    *[m for m in MIDDLEWARE if m != "django.middleware.security.SecurityMiddleware"],  # noqa: F405
]

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# --- Database -------------------------------------------------------------
if not os.environ.get("DATABASE_URL"):
    raise ImproperlyConfigured(
        "DATABASE_URL must point at the Supabase Postgres connection string in production."
    )
