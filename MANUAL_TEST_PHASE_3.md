# Manual testing — Phase 3

Follow after `python manage.py test` passes. Covers the family web
interface and production deployment. Uses the real Gmail/Calendar/OpenAI
accounts and (for the production section) a real Oracle server — nothing
here is exercised by the automated test suite.

## Local: web interface

### Login and logout

1. `python manage.py runserver`
2. Visit `http://localhost:8000/` while logged out → redirected to
   `/accounts/login/`.
3. Log in with a superuser created via `createsuperuser` (or a regular user
   created via `/admin/`) → redirected to **Upcoming**.
4. Click **Logout** → redirected to the login page; visiting `/` again
   redirects back to login.
5. Create a second, non-staff `User` via `/admin/` (staff unchecked). Log in
   as them → confirm no **Admin** link appears in the nav, and visiting
   `/admin/` directly is refused.

### Upcoming dashboard

6. In Django admin, create: one `CalendarEventRecord` dated a few days out,
   one open `Task` with a due date in the past (overdue), one open `Task`
   due within 30 days, one pending `Reminder`, and approve/leave one
   `ParsedAction` as `pending_review` (or forward a test email — see below).
7. Visit **Upcoming** → confirm all five appear, each labelled with its
   type, the overdue task appears near the top, and each row shows date/
   time, assignee/related person where relevant, status, and a link.

### Calendar rendering

8. Visit **Calendar** → month view loads with the `CalendarEventRecord`
   from step 6 visible.
9. Switch to week view and list view using the top-right buttons.
10. Click the calendar event → a modal shows its type/status/assignee/
    location; if a Google Calendar ID exists, an **Open in Google Calendar**
    link appears and opens the real event in a new tab.
11. Confirm a task's due date and a reminder's date/time also appear, in a
    different colour, per the legend above the calendar.

### Task creation and completion

12. **Tasks** → **+ New task** → fill in title, assignee, due date → Save →
    confirm it appears in the list and a success message shows.
13. Filter by status, by assignee, by "Overdue only", and search by title —
    confirm each narrows the list correctly.
14. Click **Complete** on a task → status flips to Completed, a success
    message shows, and it now shows under the "completed" filter.
15. Click **Cancel** on another open task → status flips to Cancelled.
16. Submit the new-task form with an empty title → confirm a clear
    validation error is shown (no crash, no stack trace).

### Note creation

17. **Notes** → **+ New note** → fill in title/body/category → Save →
    confirm the note detail page renders the body correctly.
18. Edit the note, change the category → Save → confirm the change persists.
19. Search notes by a word from the body, and filter by category → confirm
    both narrow the list.
20. Submit the new-note form with both title and body empty → confirm a
    validation error, not a crash.

### Staff review approval

21. As a **non-staff** user, visit **Review** → confirm only a count is
    shown, with no approve/reject controls, no raw email body, and no
    Django-admin link.
22. As a **staff** user, visit **Review** → open a `pending_review` action →
    confirm subject, outer sender, received date, proposed action,
    extracted fields (editable), missing fields, ambiguity notes, and
    supporting evidence are all shown. If the source email had an
    attachment, confirm the attachment-present warning appears.
23. Edit an extracted field (e.g. fix a mistyped title) → **Save changes** →
    confirm the change persists and the form reloads with it.
24. Click **Approve & execute** on a valid calendar-event action → confirm
    it redirects to the review list with a success message, and the event
    now appears on **Upcoming**/**Calendar** and in real Google Calendar.
25. Create a second `pending_review` action that will duplicate an existing
    calendar event (same title/date/time) and approve it → confirm a clear
    "not created — duplicate" message, not a stack trace.
26. Reject a `pending_review` action → confirm it disappears from the
    review list and the nav badge count decreases.
27. Click **Open in Django admin** from a review detail page → confirms it
    opens the same `ParsedAction` in `/admin/`.

### Static files

28. With `DEBUG=True` (development settings), confirm Bootstrap styling and
    the FullCalendar widget render correctly without touching
    `collectstatic`.
29. Run `python manage.py collectstatic --noinput` locally → confirm it
    completes without error and `staticfiles/css/app.css` exists.

## Production settings (local dry run before deploying)

30. Confirm production settings refuse to start without a secret key:
    ```bash
    DJANGO_SETTINGS_MODULE=config.settings.production DJANGO_SECRET_KEY= \
      DJANGO_ALLOWED_HOSTS=example.com DATABASE_URL=sqlite:///db.sqlite3 \
      python -c "import django; django.setup()"
    ```
    → expect an `ImproperlyConfigured` error, not a silent start.
31. Confirm it also refuses to start without `DATABASE_URL` or
    `DJANGO_ALLOWED_HOSTS` set, the same way.

## Production (on the Oracle server — see DEPLOY_ORACLE.md)

### Gunicorn startup

32. `sudo systemctl start lifeassistant-web` → `sudo systemctl status
    lifeassistant-web --no-pager` shows `active (running)`.
33. `curl -s --unix-socket /run/lifeassistant/gunicorn.sock http://localhost/health/`
    returns `{"status": "ok", ...}`.

### Nginx proxy

34. `sudo nginx -t` reports no errors after installing
    `lifeassistant.conf`.
35. `curl -s http://<YOUR_DOMAIN>/health/` (before HTTPS) returns the same
    JSON via the reverse proxy.
36. Visit `http://<YOUR_DOMAIN>/static/css/app.css` in a browser → served
    directly by Nginx (check response headers, not proxied to Gunicorn).

### HTTPS

37. After `certbot --nginx`, visit `https://<YOUR_DOMAIN>/` → valid
    certificate, no browser warning.
38. Visit `http://<YOUR_DOMAIN>/` → redirected to `https://`.
39. Log in over HTTPS → confirm the session cookie is marked `Secure` (check
    in browser dev tools → Application → Cookies).

### Gmail timer

40. `sudo systemctl status lifeassistant-gmail.timer --no-pager` shows it
    active and lists the next scheduled run.
41. `sudo journalctl -u lifeassistant-gmail.service -n 20 --no-pager` shows
    recent successful runs, with counts of messages processed — no raw
    email bodies or tokens in the log lines.

### Reminder timer

42. Same checks as above for `lifeassistant-reminders.timer` /
    `.service`. Create a `Reminder` due one minute in the future via
    `/admin/`, wait for the timer to fire, and confirm the email arrives
    and the reminder's status becomes `sent`.

### Server restart

43. `sudo reboot` (or `sudo systemctl daemon-reexec` if you'd rather not
    reboot) → after the server comes back, confirm
    `lifeassistant-web`, `lifeassistant-gmail.timer`, and
    `lifeassistant-reminders.timer` are all `active` without manual
    intervention (`enable`d in step 11/12 of DEPLOY_ORACLE.md).

### Health endpoint

44. `curl -s https://<YOUR_DOMAIN>/health/` → `{"status": "ok", "database": "ok"}`.
45. Temporarily point `DATABASE_URL` at an unreachable host and restart the
    service → `/health/` now returns HTTP 503 with `"database":
    "unreachable"` — confirm this does **not** crash the process or expose
    a stack trace, then restore the correct `DATABASE_URL` and restart
    again.

### Database connection

46. `python manage.py dbshell` (or `psql "<DATABASE_URL>"`) connects
    successfully to Supabase from the server.
47. `python manage.py showmigrations` shows every migration applied.

### Google credential availability

48. `python manage.py shell -c "from core.models import GoogleCredential;
    print(GoogleCredential.objects.filter(account_email='<assistant
    email>').exists())"` → `True`.
49. Confirm `/admin/core/googlecredential/` shows the row's email and
    timestamps only — never a decrypted token or the raw `encrypted_data`
    field.

### End-to-end forwarded appointment on production

50. Forward a real appointment email to the assistant's Gmail address from
    an authorised sender.
51. Within ~1 minute, confirm (in order): the Gmail timer log shows it was
    processed; the event appears in real Google Calendar; a confirmation
    reply arrives in the authorised sender's inbox; the event appears on
    `/` (Upcoming) and `/calendar/` in the web app.
52. Forward a deliberately ambiguous email (e.g. no clear date) → confirm it
    lands in `/review/` instead, and a staff user can resolve it there.
