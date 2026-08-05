"""Gunicorn configuration for the Family Assistant web app.

Referenced by deploy/systemd/lifeassistant-web.service as:
    gunicorn --config /path/to/deploy/gunicorn.conf.py config.wsgi:application

Binds to a Unix socket by default (matches the example Nginx config in
deploy/nginx/lifeassistant.conf.example) so no application port is exposed
directly on the network. Override with the GUNICORN_BIND env var if you'd
rather bind to localhost:8000.
"""

import multiprocessing
import os

# Unix socket by default — Nginx talks to Gunicorn over this, never over the
# public network. Set GUNICORN_BIND=127.0.0.1:8000 to use a TCP port instead.
bind = os.environ.get("GUNICORN_BIND", "unix:/run/lifeassistant/gunicorn.sock")

# A small family app: 2-4 workers is plenty. (2 x CPU) + 1 is the usual
# Gunicorn rule of thumb, capped here to avoid over-provisioning a small
# Oracle instance.
workers = int(os.environ.get("GUNICORN_WORKERS", min(4, multiprocessing.cpu_count() * 2 + 1)))
worker_class = "sync"
threads = int(os.environ.get("GUNICORN_THREADS", "2"))

timeout = int(os.environ.get("GUNICORN_TIMEOUT", "30"))
graceful_timeout = 30
keepalive = 5

# Restart workers periodically to bound the effect of any slow memory leak.
max_requests = 500
max_requests_jitter = 50

accesslog = "-"   # stdout — captured by systemd/journald, not a raw file.
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")

# Never run as root. The systemd unit already sets User=/Group=; this is a
# defence-in-depth no-op unless gunicorn is somehow started as root.
# (Left unset here — set via the systemd unit's User= directive instead,
# which is the more reliable place to enforce it.)
