# Family Email Assistant — Phase 1

A deliberately scoped Django app: forward an appointment email to
`lifeofchukwudi@gmail.com`, it gets extracted, validated, and either added to
the shared Google Calendar (with a confirmation reply) or parked in Django
admin for manual review.

Vertical slice: **forwarded email → extraction → validation → calendar event
or admin review → confirmation email**. No tasks, notes, reminders, custom
dashboard, or general agent in this phase.

## Project structure

```text
config/       Django project settings/urls
core/         All models + Django admin (the review UI)
assistant/    Gmail / OpenAI / Calendar services + management commands
web/          Minimal views (health check only)
```

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
never reprocesses an already-processed or pending message.

## Retry a failed email

```bash
python manage.py retry_email <GMAIL_MESSAGE_ID>
```

Re-runs the same validated pipeline (sender check + idempotency included) —
does not bypass either.

## Django admin

`http://localhost:8000/admin/` — the complete Phase 1 interface. Review
`ParsedAction` records saved as `pending_review`, correct fields inline, and
use the **Approve and create calendar event** admin action (calls the same
`create_event_for_parsed_action` service used by automatic processing — no
duplicated business logic).

## Tests

```bash
python manage.py test
```

All Gmail, Calendar, and OpenAI calls are mocked; tests never hit the network
and run against SQLite in memory.

## Manual testing

See [`MANUAL_TEST_PHASE_1.md`](MANUAL_TEST_PHASE_1.md) for end-to-end steps
using the real Gmail/Calendar/OpenAI accounts.

## Out of scope for Phase 1

Attachment parsing, Celery/Redis/Docker, a custom frontend, tasks/notes/
reminders, deployment to Oracle.
