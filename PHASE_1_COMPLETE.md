# Phase 1 — Complete

The full vertical slice is built: **forwarded email → extraction →
validation → calendar event or admin review → confirmation email**.

## What was built

- 3 Django apps exactly: `core` (models + admin), `assistant` (Gmail/OpenAI/
  Calendar services + management commands), `web` (health check only).
- 6 models in `core`: `HouseholdMember`, `IncomingEmail`, `ParsedAction`,
  `CalendarEventRecord`, `AuditLog`, `GoogleCredential` (Fernet-encrypted,
  never exposed in admin).
- `assistant/services/`: `email_parser.py` (MIME parsing, HTML sanitisation,
  forwarded-message/date detection, attachment metadata only),
  `extractor.py` (Pydantic `AppointmentExtraction` schema, prompt-injection-
  resistant system prompt, deterministic date/time/timezone handling),
  `gmail.py` (Gmail API + sender authorisation), `google_calendar.py`
  (deterministic duplicate/reschedule detection, the auto-create gate, and
  the single `create_event_for_parsed_action` used by both automatic
  processing and the admin approval action), `email_sender.py` (confirmation
  replies to the authorised outer sender only).
- Management commands: `google_auth` (local browser OAuth flow),
  `poll_gmail` (bounded, idempotent, labels + audits everything), and
  `retry_email` (manual retry without bypassing sender validation or
  idempotency).
- Django admin is the full Phase 1 UI, including `approve_and_create_event`,
  `reject_selected_actions`, `retry_selected_actions` actions on
  `ParsedAction`.
- 28 focused tests, all mocked/pure, covering the 8 required behaviours plus
  a couple of parser tests. `python manage.py test` passes.
- `python manage.py check` passes; `python manage.py migrate` runs cleanly
  on a fresh SQLite database.

## What you must configure

- Create the Google Cloud project and OAuth client; set `GOOGLE_CLIENT_ID` /
  `GOOGLE_CLIENT_SECRET` (or `GOOGLE_CLIENT_SECRETS_FILE`) in `.env`.
- Set `GOOGLE_CALENDAR_ID` to the shared calendar's ID.
- Set `OPENAI_API_KEY` and `OPENAI_MODEL`.
- Set `AUTHORISED_EMAIL_IKE` / `AUTHORISED_EMAIL_WIFE`.
- Generate `APP_ENCRYPTION_KEY` (see README) and a real `DJANGO_SECRET_KEY`.
- Create the Supabase project and set `DATABASE_URL` when you're ready to
  point at it (leave unset for local SQLite).

## Exact commands to run

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in the values above
python manage.py migrate
python manage.py createsuperuser
python manage.py google_auth      # run locally, browser required
python manage.py poll_gmail       # run manually or via cron
python manage.py test
python manage.py runserver        # admin at /admin/, health check at /healthz/
```

## Manual tests to perform

See `MANUAL_TEST_PHASE_1.md` for the 10-step checklist: OAuth, a clear
appointment, a missing time, several dates, a forwarded relative date, an
unauthorised sender, a duplicate message, a possible reschedule, an
attachment-only email, and manual admin correction + approval.

## Genuine incomplete items

- No live Gmail/Calendar/OpenAI calls have been made — this environment has
  no credentials and automated tests intentionally never call the real
  APIs. First real-world verification happens when you run the manual tests
  above with your own `.env` values.
- Google Cloud OAuth consent screen, Supabase project, and Oracle deployment
  are explicitly out of scope for this phase and were not touched.
- Forwarded-message detection covers Gmail's own forward marker, Outlook's
  "Original Message"/Apple Mail's "Begin forwarded message", and "On ...
  wrote:" quoting — unusual mail clients with different quoting conventions
  may need a small regex addition later if you hit one in practice.
