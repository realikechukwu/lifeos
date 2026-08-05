# Family Email Assistant — Phase 1 + 2 + 3

A deliberately scoped Django app. Phase 1: forward an appointment email to
`lifeofchukwudi@gmail.com`, it gets extracted, validated, and either added to
the shared Google Calendar (with a confirmation reply) or parked in Django
admin for manual review. Phase 2 adds direct email commands on top of the
same pipeline: **tasks, notes, email reminders, and marking a task
complete** — still one primary action per email (plus, optionally, one
linked reminder when explicitly requested alongside a calendar event).
Phase 3 adds a small authenticated **family web interface** (Upcoming,
Calendar, Tasks, Notes, Review) on top of the same data, plus a production
settings structure and Oracle deployment files — see
[`PHASE_3_COMPLETE.md`](PHASE_3_COMPLETE.md).

## Project structure

```text
config/                Django project settings (config/settings/{base,development,production}.py) + urls
core/                   All models + Django admin (the staff review UI)
assistant/              Gmail / OpenAI / Calendar services + management commands
web/                     Family web interface (Upcoming/Calendar/Tasks/Notes/Review) + health checks
deploy/                  systemd units, Nginx example config, Gunicorn config, update script
```

`assistant/services/`:

- `extractor.py` — the single Pydantic schema (`AssistantAction`) covering
  every action type, plus deterministic date/time parsing/validation.
- `router.py` — the single place that decides, per email, which one
  primary action (and optional linked reminder) gets executed automatically
  vs. deferred to `pending_review`.
- `tasks.py`, `notes.py`, `reminders.py` — gate + creation/completion logic
  per domain, reused by both automatic processing and Django admin actions.
- `gmail.py`, `google_calendar.py`, `email_parser.py`, `email_sender.py` —
  unchanged Phase 1 Gmail/Calendar/reply plumbing (with one small addition:
  `GmailService.send_message` for non-reply reminder emails).

## Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# fill in .env: DJANGO_SECRET_KEY, APP_ENCRYPTION_KEY, OPENAI_API_KEY,
# OPENAI_MODEL, GOOGLE_CLIENT_ID/SECRET (or GOOGLE_CLIENT_SECRETS_FILE),
# GOOGLE_CALENDAR_ID, AUTHORISED_EMAIL_IKE, AUTHORISED_EMAIL_WIFE.

# Generate an encryption key:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

python manage.py migrate
python manage.py createsuperuser
```

`DATABASE_URL` unset → SQLite (`db.sqlite3`). Set `DATABASE_URL` to point at
Supabase Postgres for production use; do not do this during local automated
test runs.

`AUTHORISED_EMAIL_IKE` / `AUTHORISED_EMAIL_WIFE` are also the *only* source
of reminder recipient addresses — a reminder's `ike`/`wife`/`both` target is
never resolved from anything found in an email body.

## Authenticate the assistant Google account

Run **locally**, where a browser is available:

```bash
python manage.py google_auth
```

This runs the local OAuth consent flow (`InstalledAppFlow.run_local_server`)
for `lifeofchukwudi@gmail.com` with the Gmail modify/send and Calendar
events scopes, and stores the credentials Fernet-encrypted in
`GoogleCredential`. To write straight into a production Supabase database,
point `DATABASE_URL` at it before running this command.

## Poll Gmail

```bash
python manage.py poll_gmail
```

Safe to run repeatedly (e.g. via cron). Processes up to 25 unread, unlabelled
messages per run, applies one of the four `LifeAssistant/*` Gmail labels, and
never reprocesses an already-processed or pending message. Each authorised
email is routed to exactly one action type — calendar event, task, note,
email reminder, or mark-task-complete — plus, only for a calendar event, one
linked reminder if explicitly requested ("...and remind both of us the day
before").

## Send due reminders

```bash
python manage.py send_due_reminders
```

Claims up to 25 due, `pending` reminders (short transaction, `select_for_update`,
`skip_locked`), commits, then sends each through Gmail outside any open
transaction. Success → `sent`; failure → `failed` with a truncated error and
an incremented attempt count. A reminder stuck in `processing` for more than
15 minutes is **not** retried automatically — reset it from Django admin
once you've confirmed whether the email actually went out.

**Exactly-once delivery is not guaranteed.** If Gmail accepts a message and
the process crashes before the following database write commits, the
reminder is left in `processing` and requires a manual decision, not a
silent auto-retry.

Run this on a schedule (e.g. cron every 5 minutes) alongside `poll_gmail`.

## Retry a failed email

```bash
python manage.py retry_email <GMAIL_MESSAGE_ID>
```

Re-runs the same validated pipeline (sender check + idempotency included) —
does not bypass either.

## Django admin

`http://localhost:8000/admin/` — restricted to staff users (Django's
default admin behaviour). Review `ParsedAction` records saved as
`pending_review` and use **Approve pending parsed actions**, which dispatches
to the same creation/completion functions used by automatic processing (no
duplicated business logic). Also:

- **Task** admin: *Mark selected tasks complete*.
- **Reminder** admin: *Cancel selected reminders*, *Reset stale processing
  reminders to pending*, *Retry selected failed reminders*.
- **Note** admin: browse/search saved notes.

Accounts (for both `/admin/` and the family web app below) are created
only through `createsuperuser` or `/admin/` → Users → Add user. There is no
public sign-up page.

## Web interface (Phase 3)

`http://localhost:8000/` — a small authenticated family dashboard built with
Django templates, Bootstrap, and FullCalendar (no separate frontend
framework, no new Django app). Every page requires login; the default
landing page is **Upcoming**.

- **Upcoming** (`/`) — chronological feed combining Google Calendar events
  and task due dates in the next 30 days, overdue tasks (near the top),
  pending reminders, and pending-review actions.
- **Calendar** (`/calendar/`) — FullCalendar month/week/list views, fed by
  the authenticated JSON endpoint at `/calendar/events.json`. Distinguishes
  calendar events, task due dates, and reminders by colour; clicking an
  event opens a details modal with a link into Google Calendar where one
  can be safely constructed.
- **Tasks** (`/tasks/`) — filter by status/assignee/overdue, search, create,
  edit, mark complete, cancel. Uses plain Django `ModelForm`s; task
  completion/cancellation call the same `assistant/services/tasks.py`
  functions used by the email pipeline and Django admin.
- **Notes** (`/notes/`) — list, search, category filter, create, edit,
  detail view.
- **Review** (`/review/`) — the pending-review queue. Non-staff users see
  only a count; staff users can edit extracted fields, approve (via the
  same `admin_approve_and_execute` service Django admin uses), reject, or
  jump to the full record in Django admin.

Raw email bodies are never rendered in the web app for non-staff users —
only subject/sender/date/extracted-field summaries. Full email content
review still happens in Django admin (staff-only).

## Production settings & deployment (Phase 3)

Settings are split into `config/settings/{base,development,production}.py`.
Local `manage.py`/`wsgi.py`/`asgi.py` default to
`config.settings.development`; production sets
`DJANGO_SETTINGS_MODULE=config.settings.production` as a process
environment variable (see `.env.example` and `DEPLOY_ORACLE.md`).
Production refuses to start with a blank/default secret key, missing
`DJANGO_ALLOWED_HOSTS`, or missing `DATABASE_URL`.

See [`DEPLOY_ORACLE.md`](DEPLOY_ORACLE.md) for the full Oracle + Nginx +
Gunicorn + systemd + Supabase deployment guide (nothing in that guide is run
automatically — you run each step yourself), and `deploy/` for the systemd
units, Nginx example config, Gunicorn config, and update script.

`GET /health/` returns a small JSON status (and, optionally, a lightweight
DB check) — never calls Gmail, Calendar, or OpenAI. `GET /healthz/` is the
original Phase 1 liveness check, kept for backwards compatibility.

## Tests

```bash
python manage.py test
```

All Gmail, Calendar, and OpenAI calls are mocked; tests never hit the network
and run against SQLite in memory.

## Manual testing

See [`MANUAL_TEST_PHASE_1.md`](MANUAL_TEST_PHASE_1.md),
[`MANUAL_TEST_PHASE_2.md`](MANUAL_TEST_PHASE_2.md), and
[`MANUAL_TEST_PHASE_3.md`](MANUAL_TEST_PHASE_3.md) for end-to-end steps
using the real Gmail/Calendar/OpenAI accounts and (for Phase 3) a real
Oracle deployment.

## Out of scope so far

Attachment parsing, Celery/Redis/Docker, drag-and-drop calendar editing,
public self-service registration, a mobile app, and any new assistant
capability beyond what Phase 1/2 already built.
