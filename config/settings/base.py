"""
Django settings for the Family Email Assistant — settings shared by every
environment. `development.py` and `production.py` both start with
`from .base import *` and only override what genuinely differs.
"""

import os
import sys
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent.parent

load_dotenv(BASE_DIR / ".env")


def _bool_env(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _list_env(name, default=""):
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "django-insecure-dev-only-change-me")


# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core",
    "assistant",
    "web",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "web.context_processors.pending_review_count",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


# Database
# Use SQLite locally when DATABASE_URL is unset; PostgreSQL (Supabase) when it is set.

DATABASE_URL = os.environ.get("DATABASE_URL")

if DATABASE_URL:
    DATABASES = {
        "default": dj_database_url.parse(DATABASE_URL, conn_max_age=600)
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

# Automated tests always run against in-memory SQLite, even if DATABASE_URL
# points at Supabase — keeps the suite fast, hermetic, and never touching
# the live project (matches "never make live calls during automated tests").
if "test" in sys.argv:
    DATABASES["default"] = {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }


# Password validation

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# Internationalization
# LANGUAGE_CODE stays en-gb; TIME_ZONE is the *display* timezone (Europe/London
# per household default) — all datetimes are stored timezone-aware (UTC) and
# converted for display, per USE_TZ = True below.

LANGUAGE_CODE = "en-gb"

APP_TIMEZONE = os.environ.get("APP_TIMEZONE", "Europe/London")
TIME_ZONE = APP_TIMEZONE

USE_I18N = True

USE_TZ = True


# Static files

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"] if (BASE_DIR / "static").exists() else []
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# Auth / login flow — no public signup; accounts are created via Django
# admin only. Every family page requires authentication (see web/views.py).

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "web:upcoming"
LOGOUT_REDIRECT_URL = "login"

# Map Django's "error" message level to Bootstrap's "danger" alert class.
from django.contrib.messages import constants as _message_constants  # noqa: E402

MESSAGE_TAGS = {_message_constants.ERROR: "danger"}


# Reasonable request body limits (this app never accepts large uploads).

DATA_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024  # 5 MB
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024  # 5 MB
DATA_UPLOAD_MAX_NUMBER_FIELDS = 200


# ---------------------------------------------------------------------------
# Application-specific settings
# ---------------------------------------------------------------------------

APP_ENCRYPTION_KEY = os.environ.get("APP_ENCRYPTION_KEY", "")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "")

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_CLIENT_SECRETS_FILE = os.environ.get("GOOGLE_CLIENT_SECRETS_FILE", "")
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "")
GOOGLE_ASSISTANT_EMAIL = os.environ.get("GOOGLE_ASSISTANT_EMAIL", "lifeofchukwudi@gmail.com")

AUTHORISED_EMAIL_IKE = os.environ.get("AUTHORISED_EMAIL_IKE", "")
AUTHORISED_EMAIL_WIFE = os.environ.get("AUTHORISED_EMAIL_WIFE", "")

GMAIL_PROCESSED_LABEL = os.environ.get("GMAIL_PROCESSED_LABEL", "LifeAssistant/Processed")
GMAIL_PENDING_LABEL = os.environ.get("GMAIL_PENDING_LABEL", "LifeAssistant/Pending")
GMAIL_FAILED_LABEL = os.environ.get("GMAIL_FAILED_LABEL", "LifeAssistant/Failed")
GMAIL_UNAUTHORISED_LABEL = os.environ.get("GMAIL_UNAUTHORISED_LABEL", "LifeAssistant/Unauthorised")

AUTOMATIC_ACTION_CONFIDENCE_THRESHOLD = float(
    os.environ.get("AUTOMATIC_ACTION_CONFIDENCE_THRESHOLD", "0.85")
)

GMAIL_POLL_MAX_MESSAGES = 25
GMAIL_POLL_QUERY_NEWER_THAN = "7d"

# Shown in the web UI footer / used to build absolute links where needed
# (e.g. in emails generated from the web app in a future phase). Never
# required for the app to function locally.
APP_BASE_URL = os.environ.get("APP_BASE_URL", "")


# ---------------------------------------------------------------------------
# Logging — safe by default: never logs email bodies, OAuth tokens, API
# keys, extracted sensitive appointment content, or session cookies.
# Environments override the handlers/level, not what gets logged.
# ---------------------------------------------------------------------------

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "structured": {
            "format": "%(asctime)s level=%(levelname)s logger=%(name)s %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "structured",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": LOG_LEVEL,
    },
    "loggers": {
        "django": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "django.security": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        # Deliberately do NOT set a low level for django.db.backends /
        # django.request bodies here — leave at default WARNING to avoid
        # accidentally logging SQL parameters or request payloads.
        "assistant": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "web": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
    },
}
