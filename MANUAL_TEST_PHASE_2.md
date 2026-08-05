# Phase 2 — Manual Test Checklist

Prerequisites: same as Phase 1 (`.env` filled in with real
`OPENAI_API_KEY`/`OPENAI_MODEL`, `GOOGLE_CALENDAR_ID`, `AUTHORISED_EMAIL_IKE`,
`AUTHORISED_EMAIL_WIFE`), Google OAuth already done (`python manage.py
google_auth`), a superuser created.

## 1. Create a task by email

- Send (from your authorised address) an email to the assistant account:
  *"Create a task for Ike to renew the home insurance by 30 September."*
- Run `python manage.py poll_gmail`.
- Expect: a `Task` row (`admin/core/task/`) with title "Renew home
  insurance", `assigned_to = ike`, `due_date = 2026-09-30`, `status = open`.
- Expect a reply like: *"I created a task for Ike: 'Renew home insurance',
  due 30 September."*

## 2. Create a task without a due date

- Send: *"Add a task to book the boiler service."*
- Run `poll_gmail`.
- Expect: a `Task` created with `due_date` empty, `assigned_to = unassigned`
  (nobody guessed).

## 3. Create a note by email

- Send: *"Make a note that the electrician still needs to repair the kitchen
  socket."*
- Run `poll_gmail`.
- Expect: a `Note` row with a sensible title and the body text saved
  verbatim. Reply: *"I saved a note: '...'."*

## 4. Create a reminder for Ike

- Send: *"Remind Ike on Friday at 7pm to put the bins out."*
- Run `poll_gmail`.
- Expect: a `Reminder` row, `recipient = ike`, `status = pending`, correct
  `reminder_date`/`reminder_time`. Reply confirms the date/time.

## 5. Create a reminder for both users

- Send: *"Remind both of us on Friday at 7pm to put the bins out."*
- Expect: `recipient = both`, one `Reminder` row (not two).

## 6. Attempt a reminder with an unclear time

- Send: *"Remind me Friday evening to call the plumber."*
- Run `poll_gmail`.
- Expect: **no** `Reminder` created. `ParsedAction.status = pending_review`
  with an ambiguity note about the missing time. Reply says the details
  couldn't be confirmed and nothing was scheduled — never a guessed 7pm.

## 7. Mark a unique task complete

- Ensure exactly one open task matches, e.g. "Renew home insurance" from
  test 1.
- Send: *"Mark the home insurance task as complete."*
- Run `poll_gmail`.
- Expect: that `Task.status = completed`, `completed_at` set. Reply: *"I
  marked 'Renew home insurance' as complete."*

## 8. Attempt to complete an ambiguous task

- Create two open tasks whose titles both contain "insurance" (e.g. "Renew
  home insurance" and "Renew car insurance").
- Send: *"Mark the insurance task as complete."*
- Run `poll_gmail`.
- Expect: **neither** task is completed. `ParsedAction.status =
  pending_review` listing both candidate titles. Reply: *"I found more than
  one open task matching 'insurance'. I saved this for review and did not
  complete a task."*

## 9. Create a calendar event plus a linked reminder

- Send: *"Add the boiler service next Tuesday at 10am and remind both of us
  the day before."*
- Run `poll_gmail`.
- Expect: a `CalendarEventRecord` for the boiler service **and** a
  `Reminder` row dated one day before the event, `recipient = both`,
  `related_calendar_event` set to that event. If no time was stated for the
  reminder, check it defaulted to the event's own start time (10am) — the
  only documented household default; anything else should have gone to
  review instead.
- Reply should mention both the event and the reminder in one message.

## 10. Run `send_due_reminders` twice and check for duplicate sends

- Create (or wait for) a `Reminder` whose `reminder_date`/`reminder_time`
  is now in the past (e.g. edit one in Django admin to 5 minutes ago).
- Run `python manage.py send_due_reminders`.
  - Expect exactly one email sent, `Reminder.status = sent`, `sent_at` set.
- Run `python manage.py send_due_reminders` again immediately.
  - Expect **zero** additional emails — the reminder is `sent`, not
    `pending`, so it is not selected again. Confirm in Gmail's Sent folder
    that only one reminder email exists.

## 11. Inspect failed and stale reminders in Django admin

- Force a failure: temporarily break `GOOGLE_ASSISTANT_EMAIL` credentials
  or similar, run `send_due_reminders` against a due reminder, then restore
  credentials.
- In `/admin/core/reminder/`, confirm the row shows `status = failed`,
  `delivery_attempts` incremented, `last_error` populated with a short,
  secret-free message.
- Use the **Retry selected failed reminders** admin action, then run
  `send_due_reminders` again and confirm it sends.
- To exercise the stale-processing path: manually set a reminder's
  `status = processing` and `claimed_at` to >15 minutes ago in admin. Confirm
  the list view flags it (`Stale?` column = True) and that **Reset stale
  processing reminders to pending** clears it back to `pending` without
  `send_due_reminders` having touched it automatically.
