"""
Django settings for the Family Email Assistant (Phase 1).
"""

import os
import sys
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

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

DEBUG = _bool_env("DJANGO_DEBUG", True)

ALLOWED_HOSTS = _list_env("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")


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

LANGUAGE_CODE = "en-gb"

APP_TIMEZONE = os.environ.get("APP_TIMEZONE", "Europe/London")
TIME_ZONE = APP_TIMEZONE

USE_I18N = True

USE_TZ = True


# Static files

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"] if (BASE_DIR / "static").exists() else []

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


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
