# Phase 1 — Manual Test Checklist

Prerequisites: `.env` filled in (real `OPENAI_API_KEY`/`OPENAI_MODEL`,
`GOOGLE_CALENDAR_ID`, `AUTHORISED_EMAIL_IKE`, `AUTHORISED_EMAIL_WIFE`), a
superuser created, `lifeofchukwudi@gmail.com` reachable.

## 1. Google OAuth

```bash
python manage.py google_auth
```

- Browser opens, you sign in as `lifeofchukwudi@gmail.com`, consent to the
  three scopes.
- Command prints a success message, no token/secret is printed.
- In `/admin/core/googlecredential/`, one row exists for that account; the
  encrypted value itself is not shown anywhere in admin.

## 2. A clear forwarded appointment

- Forward a real appointment confirmation email (has a clear date/time/title)
  from your authorised address to the assistant account.
- Run `python manage.py poll_gmail`.
- Expect: a `CalendarEventRecord` created, event visible on the shared
  calendar, `IncomingEmail.status = processed`, Gmail message labelled
  `LifeAssistant/Processed`, and a reply in the original thread like:
  *"I added "X" to the shared calendar for \<day\> \<date\> at \<time\>."*

## 3. An appointment with no time

- Forward an email with a clear date but no time (e.g. "parents evening on
  20 August").
- Run `poll_gmail`.
- Expect: no calendar event created, `ParsedAction.status = pending_review`,
  labelled `LifeAssistant/Pending`, reply says it couldn't confidently
  identify the time and saved it for review.

## 4. An email with several unrelated dates

- Forward an email containing multiple dates where only one is the actual
  appointment (e.g. "last visit was X, next appointment is Y").
- Run `poll_gmail`.
- Expect either a correct auto-created event for the real appointment date,
  or `pending_review` with an ambiguity note — never the wrong date silently
  auto-created.

## 5. A forwarded relative date ("next Tuesday")

- Forward an email whose original message says something like "see you next
  Tuesday at 9am", sent a few days before you forward it.
- Run `poll_gmail`.
- Expect the relative date resolved against the *original forwarded email's*
  date, not against today. Check `IncomingEmail.original_forwarded_date` and
  the resulting `ParsedAction.appointment_date` in admin.

## 6. An unauthorised sender

- Send (not forward — send directly) an appointment-shaped email to the
  assistant account from an address that is not `AUTHORISED_EMAIL_IKE`,
  `AUTHORISED_EMAIL_WIFE`, or an authorised `HouseholdMember`.
- Run `poll_gmail`.
- Expect `IncomingEmail.status = unauthorised`, labelled
  `LifeAssistant/Unauthorised`, no calendar event, **no reply sent**.

## 7. A duplicate message

- Run `poll_gmail` again without unread/new mail, or re-run against an email
  already labelled `LifeAssistant/Processed` or `.../Pending`.
- Expect: it is skipped (excluded by the label filter in the Gmail query) and
  nothing changes.

## 8. A possible reschedule

- Forward an appointment with a booking reference (e.g. `BSD-88213`) and let
  it auto-create an event.
- Forward a second email with the *same* booking reference but a *different*
  date/time.
- Run `poll_gmail`.
- Expect: no second event created, no update to the first event,
  `ParsedAction.status = pending_review` with an ambiguity note mentioning a
  possible reschedule.

## 9. An attachment-only appointment

- Forward an email whose body has almost no text (e.g. "see attached") with
  a PDF/image attached.
- Run `poll_gmail`.
- Expect `pending_review`, `attachment_metadata` populated with
  filename/mime type only (no download), and a reply saying the details may
  be in the attached document.

## 10. Manual admin correction and approval

- Open a `pending_review` `ParsedAction` in `/admin/`.
- Correct the title, date, time, or location.
- Save, then select it in the list and run the **Approve and create calendar
  event** admin action.
- Expect a `CalendarEventRecord` created via the same service used by
  automatic processing, and `ParsedAction.status = executed`.
