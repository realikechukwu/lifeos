# Phase 2 Complete

Direct email commands, tasks, notes, email reminders, and marking tasks
complete — built on top of the unchanged Phase 1 Gmail/Calendar/OAuth/audit
foundation.

## What was built

- **Extended extraction schema**: `AppointmentExtraction` →
  `AssistantAction` (`assistant/services/extractor.py`), in place, covering
  `create_calendar_event`, `create_task`, `create_note`,
  `create_email_reminder`, `mark_task_complete`, `requires_review`,
  `unsupported`. All dates/times stay separate strings, parsed and validated
  in Python (`parse_iso_date`, `parse_24h_time`, `is_plausible_date`),
  never a naive datetime.
- **New models** (`core/models.py`): `Task`, `Note`, `Reminder`, plus
  matching fields added to `ParsedAction` (due/reminder dates & times,
  `assigned_to`, `reminder_recipient`, `reminder_lead_days`,
  `task_search_text`, `note_category`, `description`).
- **Router** (`assistant/services/router.py`): the single dispatch point
  that builds a `ParsedAction` from an extraction, runs the right
  deterministic auto-create gate, and executes exactly one primary action
  plus, only for `create_calendar_event`, one linked reminder when
  explicitly requested. Everything else is deferred to a `pending_review`
  `ParsedAction` with a reply asking for clarification — never guessed.
- **Domain services**: `tasks.py` (gate, creation, deterministic
  exact/partial-match completion search), `notes.py` (gate, creation, title
  generation from body when none given), `reminders.py` (gate, recipient
  resolution restricted to `AUTHORISED_EMAIL_IKE`/`AUTHORISED_EMAIL_WIFE`,
  creation for both standalone and event-linked reminders).
- **`send_due_reminders` management command**: short claim transaction
  (`select_for_update(skip_locked=True)`, pending → processing, generates a
  fresh unique `send_key` + RFC `Message-ID`), commits, then sends via Gmail
  with no transaction held open. Batches of 25. Documented,
  not-perfectly-avoidable at-least-once risk if the process crashes between
  Gmail accepting a message and the status update committing.
- **Django admin**: `Task`, `Note`, `Reminder` registered with
  `list_display`/`list_filter`/`search_fields`/`date_hierarchy` and
  related-object inlines on `ParsedAction`. Admin actions — *Mark selected
  tasks complete*, *Cancel selected reminders*, *Reset stale processing
  reminders to pending*, *Retry selected failed reminders*, *Approve
  pending parsed actions* — all call the same service functions as
  automatic processing (no duplicated logic).
- **Confirmation replies** (`assistant/services/email_sender.py`) extended
  for every new outcome, still always sent to the incoming email's outer
  sender only.
- **Small additive changes only** to Phase 1 code: one new
  `GmailService.send_message` method (fresh, non-reply messages for
  reminders); `poll_gmail.py`'s extraction/gate logic replaced with calls
  into the new router (behaviour for calendar events is unchanged — same
  gate, same `create_event_for_parsed_action`).

## What was intentionally not built

- Custom family dashboard.
- Oracle / production deployment configuration.
- Any new Django app, Celery, Redis, or Docker.
- Attachment parsing.
- A second/alternate extraction pipeline.
- Broad fuzzy-matching for task completion (deterministic exact/substring
  matching only).

## Commands

```bash
python manage.py migrate
python manage.py check
python manage.py test
python manage.py poll_gmail
python manage.py send_due_reminders
```

## Genuinely incomplete / left for later

- The "day before" reminder default (same time as the event when no time is
  stated) is the only household default implemented; there is no
  configurable per-household default store yet — anything else falls back
  to `pending_review`.
- `send_due_reminders` is meant to be run on a schedule (cron) — no
  in-process scheduler was added, per the no-Celery/no-Docker constraint.
- No automated test exercises a real Gmail/OpenAI/Calendar call — all
  external APIs are mocked, matching the "no live external API tests"
  constraint; the manual test document exists for that coverage.
