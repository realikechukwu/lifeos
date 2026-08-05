# Family Email Assistant — Phase 1 + Phase 2

A deliberately scoped Django app. Phase 1: forward an appointment email to
`lifeofchukwudi@gmail.com`, it gets extracted, validated, and either added to
the shared Google Calendar (with a confirmation reply) or parked in Django
admin for manual review. Phase 2 adds direct email commands on top of the
same pipeline: **tasks, notes, email reminders, and marking a task
complete** — still one primary action per email (plus, optionally, one
linked reminder when explicitly requested alongside a calendar event).

No custom family dashboard and no Oracle/production deployment config yet —
still out of scope.

## Project structure

```text
config/       Django project settings/urls
core/         All models + Django admin (the review UI)
assistant/    Gmail / OpenAI / Calendar services + management commands
web/          Minimal views (health check only)
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

`http://localhost:8000/admin/` — review `ParsedAction` records saved as
`pending_review` and use **Approve pending parsed actions**, which dispatches
to the same creation/completion functions used by automatic processing (no
duplicated business logic). Also:

- **Task** admin: *Mark selected tasks complete*.
- **Reminder** admin: *Cancel selected reminders*, *Reset stale processing
  reminders to pending*, *Retry selected failed reminders*.
- **Note** admin: browse/search saved notes.

## Tests

```bash
python manage.py test
```

All Gmail, Calendar, and OpenAI calls are mocked; tests never hit the network
and run against SQLite in memory.

## Manual testing

See [`MANUAL_TEST_PHASE_1.md`](MANUAL_TEST_PHASE_1.md) and
[`MANUAL_TEST_PHASE_2.md`](MANUAL_TEST_PHASE_2.md) for end-to-end steps using
the real Gmail/Calendar/OpenAI accounts.

## Out of scope so far

Attachment parsing, Celery/Redis/Docker, a custom frontend/dashboard,
deployment to Oracle.
