# Family LifeOS Assistant — Email, Web, and Telegram

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

Telegram is an optional two-way channel over the same action services. The
two configured household users can talk to LifeOS privately or in one linked
family group. The bot asks follow-up questions, shows a structured summary,
and requires a button confirmation before executing an action. Leaving its
settings empty preserves the original email-only behaviour.

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

## Telegram conversations

Set the Telegram values described in `.env.example`, deploy/migrate, then run:

```bash
DJANGO_SETTINGS_MODULE=config.settings.production python manage.py configure_telegram
```

This installs the HTTPS webhook at `<APP_BASE_URL>/telegram/webhook/` and the
bot command menu. Requests are accepted only when Telegram supplies the
configured webhook-secret header and the immutable sender id matches
`TELEGRAM_IKE_USER_ID` or `TELEGRAM_WIFE_USER_ID`.

Each authorised user can `/start` the bot privately and send either text or a
short voice note (up to 2 minutes and 5 MB). Voice is transcribed in the spoken
language, acknowledged back to the user, and then follows the same clarification
and explicit-confirmation flow as typed text. For example: “Remind both of us on
Friday at 6pm to renew the insurance.” Raw audio is deleted immediately after
transcription and is never stored by LifeOS. For the shared group,
Ike sends `/linkgroup` once inside it; only the configured owner id can enrol a
group. Alternatively, put the group's negative numeric id in
`TELEGRAM_ALLOWED_GROUP_CHAT_IDS`. BotFather's Group Privacy must be off for
ordinary group messages rather than commands only.

Useful commands are `/today`, `/tasks`, `/calendar`, `/notes`, `/reminders`,
`/search <words>`, `/settings`, `/upcoming`, `/cancel`, `/help`, and `/linkgroup`. The inline
menu provides a paginated LifeOS inbox: browse tasks, notes, reminders, and
calendar events; complete/edit tasks; view/edit notes; snooze/cancel
reminders; and reschedule/cancel one-off calendar events with a final
confirmation. Recurring events remain read-only in Telegram so a person can
choose the correct single-occurrence or whole-series operation in Google
Calendar. Scheduled reminders created through Telegram continue to use the
established Gmail reminder delivery.

### Scheduled Telegram briefings

After an authorised user has started the bot privately, LifeOS sends a daily
private briefing at 07:30 local app time by default. It contains that day's
calendar, overdue/due tasks, and pending reminders, with links into the inbox.
Users can switch it on/off or change its time with `/settings`. Run
`send_telegram_briefings` every minute using the supplied systemd timer; a
per-user/type/day record prevents duplicates.

The same runner also sends two planning digests in the app's configured local
timezone:

- Every Sunday at 18:00, a week-ahead digest covering Monday through Sunday.
- On the first Sunday of each month at 09:30, a month-ahead digest covering the
  next day through the day before the same date in the next calendar month.

Planning digests include calendar events and Patchwork shifts, open tasks due
inside the period, and pending reminders. They follow the main briefing on/off
preference; changing the configurable daily time does not move the weekly or
monthly schedules.

```bash
python manage.py send_telegram_briefings
```

Keep the Bot API token and webhook secret only in the server environment. If
a token is ever pasted into chat, logs, or source control, regenerate it in
BotFather after testing.

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

- **Upcoming** (`/`) — dated agenda for the next 30 days, with an always-visible
  Today section and expandable event details. Overdue tasks, overdue pending
  reminders, and pending reviews appear in a separate Needs attention section.
- **Calendar** (`/calendar/`) — FullCalendar month/week/list views, fed by
  the authenticated JSON endpoint at `/calendar/events.json`. Distinguishes
  calendar events, Ike work shifts, task due dates, and reminders by colour; clicking an
  event opens a details modal with a link into Google Calendar where one
  can be safely constructed.
  Type filters narrow the schedule without reloading the page. List view highlights
  today and opens at today's entries (or the next scheduled day); Jump to today
  remains available within the current month. The Today strip stays available on
  empty days. Calendar and details use the app timezone and 24-hour times; sent
  reminders and completed tasks retain their status as history.

### Patchwork work-shift sync

Set `PATCHWORK_CALENDAR_URL` (the private `.ics` subscription URL) in the
same deployment environment file as `GOOGLE_CALENDAR_ID`. The
`sync_patchwork_calendar` command mirrors the source feed into that shared
Google calendar twice daily (07:00 and 19:00 Europe/London) when the included
`lifeassistant-patchwork-calendar.timer` is installed. Mirrored entries keep
their Patchwork label, prefixed with **Ike —** (for example, **Ike — Standard
Day — General Medicine** or **Ike — Annual leave**). Redundant work segments
fully contained by a longer work event on the same day are suppressed, while
adjacent shifts and leave/time-off entries remain visible. Updates and
cancellations must be made in Patchwork, not LifeOS.
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

See [`DEPLOY_ORACLE.md`](DEPLOY_ORACLE.md) for the full Oracle + Gunicorn +
systemd + Supabase deployment guide (nothing in that guide is run
automatically — you run each step yourself), and `deploy/` for the systemd
units, Gunicorn config, and update script. The reverse proxy step has two
documented paths: a dedicated Nginx + Certbot (`deploy/nginx/`) if this app
owns ports 80/443 outright, or — if another app on the same box already
runs a reverse proxy (e.g. Caddy in Docker) that owns those ports — adding
one new site entry to that existing proxy instead
(`deploy/caddy/lifeassistant-site.Caddyfile.example`), with Gunicorn bound
to a host-only TCP port. Either way this app keeps its own dedicated Linux
user, venv, `.env`, systemd units, and database.

`GET /health/` returns a small JSON status (and, optionally, a lightweight
DB check) — never calls Gmail, Calendar, or OpenAI. `GET /healthz/` is the
original Phase 1 liveness check, kept for backwards compatibility.

## Tests

The automated suite is intentionally made of isolated unit and integration
tests rather than live-account browser end-to-end tests. The full suite takes
only a few seconds locally, so keep it as the default confidence check:

```bash
python manage.py test
```

Calendar controller interaction checks (Node.js built-in test runner):

```bash
node --test web/tests/calendar_ui.test.cjs
```

All Gmail, Calendar, and OpenAI calls are mocked; tests never hit the network
and run against SQLite in memory.

For fast feedback while changing one area, run its test class first, then run
the full suite before merging. For example, the Telegram briefing tests are:

```bash
python manage.py test assistant.tests.test_telegram_inbox.TelegramBriefingTests
```

## Manual testing

See [`MANUAL_TEST_PHASE_1.md`](MANUAL_TEST_PHASE_1.md),
[`MANUAL_TEST_PHASE_2.md`](MANUAL_TEST_PHASE_2.md), and
[`MANUAL_TEST_PHASE_3.md`](MANUAL_TEST_PHASE_3.md) for end-to-end steps
using the real Gmail/Calendar/OpenAI accounts and (for Phase 3) a real
Oracle deployment. These checks are not automated and should be used when a
change touches the corresponding external integration or deployment path.

## Out of scope so far

Attachment parsing, Celery/Redis/Docker, drag-and-drop calendar editing,
public self-service registration, a mobile app, and any new assistant
capability beyond what Phase 1/2 already built.
