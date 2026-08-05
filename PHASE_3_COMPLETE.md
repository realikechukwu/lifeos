# Phase 3 Complete

A small authenticated family web interface, production settings, and
Oracle deployment files/instructions — built on top of the unchanged
Phase 1/2 Gmail/OpenAI/Calendar pipeline. No new Django app, no new
assistant capability, no Celery/Redis/React.

## What was built

### Family web interface (`web/`)

- **Auth**: Django's built-in `LoginView`/`LogoutView`
  (`/accounts/login/`, `/accounts/logout/`), `LOGIN_URL`/
  `LOGIN_REDIRECT_URL`/`LOGOUT_REDIRECT_URL` in settings. Every family page
  is `@login_required`; anonymous visitors are redirected to login.
  Accounts are created only via `createsuperuser` or `/admin/` — no
  public sign-up view exists anywhere in the URLconf. `/admin/` enforces
  staff-only access via Django's own default `AdminSite.has_permission`
  (unchanged, no code needed).
- **Upcoming** (`/`, default landing page) — `web/services.py:
  build_upcoming_feed()` combines `CalendarEventRecord`s in the next 30
  days, open `Task`s due in that window, overdue open tasks (sorted to the
  top), pending `Reminder`s, and `pending_review` `ParsedAction`s into one
  labelled, chronological list. Read-only aggregation only — no new
  business logic.
- **Calendar** (`/calendar/`) — FullCalendar (CDN, pinned version) month/
  week/list views, fed by the authenticated JSON endpoint
  `/calendar/events.json` (`web/services.py: build_calendar_feed()`).
  Distinguishes calendar events / task due dates / reminders by colour and
  an `extendedProps.type` label; clicking an event shows a details modal;
  a "best-effort" Google Calendar deep link
  (`assistant/services/google_calendar.py:
  build_google_calendar_event_url()`) is offered where an event id exists.
- **Tasks** (`/tasks/`) — filter by status/assignee/overdue + search;
  create/edit via a plain `TaskForm` (`ModelForm`, no OpenAI call);
  complete/cancel call the *existing* `assistant/services/tasks.py`
  `complete_task()` (unchanged) and the newly-added `cancel_task()`
  (same shape/audit pattern) — the same functions Django admin's "Mark
  selected tasks complete" action uses.
- **Notes** (`/notes/`) — list/search/category filter, create/edit/detail
  via a plain `NoteForm`.
- **Review** (`/review/`) — pending-review queue. Non-staff users see only
  a count (no subject/sender/extracted fields, no admin link, no
  approve/reject controls). Staff users see the full list and a detail
  page showing subject, outer sender, received date, proposed action,
  editable extracted fields, missing fields, ambiguity notes, supporting
  evidence, and an attachment-present warning. **Save** persists field
  edits; **Approve** calls the *existing*
  `assistant/services/router.py: admin_approve_and_execute()` — the exact
  function Django admin's "Approve pending parsed actions" action already
  used — so no event/task/note/reminder creation logic is duplicated in
  the view; **Reject** sets status directly (same as the admin action);
  a link opens the same record in Django admin.
- **Error handling**: Django messages framework for every success/failure
  path (form validation, approval failure, duplicate detection, permission
  denied). Exceptions from `admin_approve_and_execute` (e.g. missing
  Google credentials, an unclear reminder) are caught and shown as a short
  message — never a stack trace or raw API response.
- **Templates**: Django templates + Bootstrap 5 (CDN) + a small
  `static/css/app.css`. No React/Next.js/other frontend framework, no HTMX
  was actually needed once plain forms + full-page navigation covered
  every interaction cleanly.

### Production settings (`config/settings/`)

Split into `base.py` / `development.py` / `production.py` (old
`config/settings.py` removed; `manage.py`/`wsgi.py`/`asgi.py` now default
to `config.settings.development`). `production.py`:

- Refuses to start (raises `ImproperlyConfigured`) with a blank/default
  secret key, missing `DJANGO_ALLOWED_HOSTS`, or missing `DATABASE_URL`.
- `DEBUG=False`; `SECURE_SSL_REDIRECT`, `SECURE_HSTS_SECONDS` (+
  subdomains/preload) configurable via env vars; `SECURE_PROXY_SSL_HEADER`
  set for Nginx; secure session/CSRF cookies; `X_FRAME_OPTIONS=DENY`;
  `SECURE_CONTENT_TYPE_NOSNIFF`.
- WhiteNoise (`whitenoise.middleware.WhiteNoiseMiddleware` +
  `CompressedManifestStaticFilesStorage`) for static files — verified
  working with `collectstatic` under production settings.
- Reasonable request body limits (`DATA_UPLOAD_MAX_MEMORY_SIZE`,
  `FILE_UPLOAD_MAX_MEMORY_SIZE` = 5 MB) in `base.py`, inherited everywhere.
- `TIME_ZONE = Europe/London` (unchanged from Phase 1/2) is the display
  timezone; storage stays UTC (`USE_TZ = True`, unchanged).
- Structured, safe logging (`base.py: LOGGING`) — never logs email
  bodies, OAuth tokens, API keys, or session cookies; existing
  `poll_gmail`/`send_due_reminders` logging (message counts, failed ids)
  is unchanged from Phase 1/2 and already met this bar.

### Health endpoint

`GET /health/` — Django-only liveness + optional lightweight `SELECT 1`
DB check, JSON response, no auth required, never calls Gmail/Calendar/
OpenAI. `GET /healthz/` (Phase 1) is kept unchanged for backwards
compatibility.

### Oracle deployment files (`deploy/`)

- `deploy/gunicorn.conf.py` — production Gunicorn config (Unix socket by
  default, bounded workers/threads, periodic worker recycling, logs to
  stdout for journald).
- `deploy/systemd/lifeassistant-web.service` — Gunicorn under systemd,
  non-root, `RuntimeDirectory` for the socket, restarts on failure.
- `deploy/systemd/lifeassistant-gmail.{service,timer}` — runs
  `poll_gmail` every ~60s; overlap is prevented by systemd's normal
  single-instance-per-unit behaviour plus `poll_gmail`'s existing
  idempotency (unchanged).
- `deploy/systemd/lifeassistant-reminders.{service,timer}` — runs
  `send_due_reminders` every ~60s; same overlap reasoning, plus the
  existing `select_for_update(skip_locked=True)` claim step (unchanged).
- `deploy/nginx/lifeassistant.conf.example` — reverse proxy to the
  Gunicorn socket, static file serving, forwarding headers, a documented
  (not executed) Certbot workflow, and a commented HTTPS server block
  preview.
- `deploy/update.sh.example` — pull → venv → install → migrate →
  collectstatic → restart → `/health/` check, `set -euo pipefail`, never
  touches `.env`, never runs a destructive DB command, guarded so it
  cannot run unedited.
- `DEPLOY_ORACLE.md` — 20 numbered steps from creating the Linux user
  through updating later, all placeholder-based; includes the Supabase
  transaction-pooler-vs-direct-connection guidance and a Backups section.

## What was intentionally not built

- Any new Django app, model, or migration (Tasks/Notes reuse Phase 2
  models exactly as they were).
- Any new assistant capability, extraction field, or OpenAI call from the
  web app.
- React/Next.js/other frontend framework; Celery/Redis; a mobile app;
  drag-and-drop calendar editing; public self-service registration.
- Actual deployment to Oracle, DNS configuration, or Certbot execution —
  `DEPLOY_ORACLE.md` documents every step but none were run.
- A connection to the real Supabase project during this session.

## Commands

```bash
python manage.py migrate
python manage.py check
python manage.py test
python manage.py collectstatic --noinput
```

## Local verification performed this session

- `python manage.py check` — no issues.
- `python manage.py test` — **90 passed** (55 pre-existing Phase 1/2 tests
  unchanged + 35 new Phase 3 tests covering: login-required on every
  family page + the calendar JSON endpoint, the Upcoming feed's item
  kinds/30-day window/overdue-sort, task create/complete/cancel + form
  validation, staff-only review approve/reject/save (mocked, no external
  API touched), the health endpoint's ok/503 paths, production settings
  refusing to start with a blank secret key / missing `DJANGO_ALLOWED_HOSTS`
  / missing `DATABASE_URL`, and `GoogleCredential`'s encrypted data never
  rendering in Django admin).
- `python manage.py collectstatic --noinput` — verified under **both**
  `config.settings.development` (128 files copied) and
  `config.settings.production` (128 files copied, 384 post-processed by
  WhiteNoise's manifest storage).
- Manual `Client()` smoke test (with `testserver` allowed) confirmed `/`,
  `/calendar/`, `/tasks/`, `/notes/`, `/review/`, `/health/`, `/healthz/`
  all return 200 for an authenticated user.

## Genuinely incomplete / left for later

- No automated test drives FullCalendar itself in a browser (it's a CDN
  script) — only the JSON endpoint it consumes is tested. See
  `MANUAL_TEST_PHASE_3.md` for the manual FullCalendar checks.
- The Google Calendar deep link (`eid=` query param) uses a
  widely-observed but undocumented URL scheme; if Google changes it, the
  link would silently stop working (display-only, not a functional
  dependency of anything else).
- No automated test exercises the real Oracle/Nginx/systemd stack, a real
  Supabase connection, or a real Gmail/Calendar/OpenAI call from the web
  app — `MANUAL_TEST_PHASE_3.md` and `DEPLOY_ORACLE.md` cover that by hand,
  as instructed.
- `HTMX` was evaluated but not used — every Phase 3 interaction (filter,
  create, complete, approve) worked cleanly as a normal form submit +
  redirect, and adding it would not have simplified anything.
- Nothing was deployed to Oracle, and no DNS/Certbot/SSH step was
  performed, per the working constraints for this session.
