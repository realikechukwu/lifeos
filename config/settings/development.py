"""Local development settings. `python manage.py runserver` uses this by
default (see manage.py). Never used in production."""

from .base import *  # noqa: F401,F403
from .base import _bool_env, _list_env, os

DEBUG = _bool_env("DJANGO_DEBUG", True)

ALLOWED_HOSTS = _list_env("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")

# Convenient for local HTTP (no HTTPS) development.
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SECURE_SSL_REDIRECT = False
