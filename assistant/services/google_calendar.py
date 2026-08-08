"""Google Calendar integration: deterministic duplicate detection, the
auto-create gate, and the single event-creation code path shared by
automatic processing and the Django admin 'approve and create event' action.
"""

import base64
import hashlib
from datetime import time, timedelta

from django.conf import settings
from django.utils import timezone
from googleapiclient.discovery import build

from core.models import CalendarEventRecord, ParsedAction

from .extractor import build_recurrence, combine_date_and_time, times_are_valid
from .gmail import get_google_credentials, is_authorised_sender

# For calendar events specifically, a title + date is enough to post — a
# missing start time defaults to this rather than blocking (see
# passes_auto_create_gate / create_event_for_parsed_action below), and the
# confirmation reply says so explicitly (assistant/services/email_sender.py)
# so it is never a silent guess. Tasks/notes/reminders are unaffected.
DEFAULT_START_TIME = time(9, 0)
DEFAULTED_START_TIME_NOTE = f"No usable start time was extracted; defaulted to {DEFAULT_START_TIME.strftime('%H:%M')}."
DEFAULTED_END_TIME_NOTE = "No end time was given; the event was scheduled for 1 hour."


def _start_time_or_all_day_str(parsed_action: ParsedAction) -> str:
    if parsed_action.all_day:
        return "ALL_DAY"
    if parsed_action.start_time:
        return parsed_action.start_time.strftime("%H:%M")
    return "NONE"


def compute_duplicate_key(
    title: str,
    appointment_date,
    start_time_or_all_day: str,
    calendar_id: str,
    recurrence_frequency: str | None = None,
) -> str:
    """SHA-256 of normalised-lowercase-title | date | start_time-or-ALL_DAY |
    calendar_id | recurrence-frequency-or-ONE_OFF. The recurrence component
    keeps a recurring series from colliding with a coincidentally identical
    one-off event that shares the same title/first-date/time/calendar."""
    normalised_title = " ".join((title or "").strip().lower().split())
    date_str = appointment_date.isoformat() if hasattr(appointment_date, "isoformat") else str(appointment_date or "")
    recurrence_component = recurrence_frequency or "ONE_OFF"
    raw = f"{normalised_title}|{date_str}|{start_time_or_all_day}|{calendar_id}|{recurrence_component}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def check_for_duplicate(parsed_action: ParsedAction, calendar_id: str):
    """Deterministic duplicate check, in the required order. Returns
    ("duplicate" | "reschedule" | "none", CalendarEventRecord | None)."""

    # 1. Same source Gmail message id already produced an event.
    existing = CalendarEventRecord.objects.filter(incoming_email=parsed_action.incoming_email).first()
    if existing:
        return "duplicate", existing

    # 2. Same non-empty booking reference.
    booking_ref = (parsed_action.booking_reference or "").strip()
    if booking_ref:
        candidate = (
            CalendarEventRecord.objects.filter(booking_reference=booking_ref, calendar_id=calendar_id)
            .order_by("-created_at")
            .first()
        )
        if candidate:
            same_date = candidate.appointment_date == parsed_action.appointment_date
            same_time = (candidate.all_day and parsed_action.all_day) or (
                candidate.start_time == parsed_action.start_time
            )
            if same_date and same_time:
                return "duplicate", candidate
            return "reschedule", candidate

    # 3. Deterministic duplicate key.
    key = compute_duplicate_key(
        parsed_action.title,
        parsed_action.appointment_date,
        _start_time_or_all_day_str(parsed_action),
        calendar_id,
        parsed_action.recurrence_frequency,
    )
    candidate = CalendarEventRecord.objects.filter(duplicate_key=key).first()
    if candidate:
        return "duplicate", candidate

    return "none", None


def build_google_calendar_event_url(google_event_id: str, calendar_id: str) -> str | None:
    """Best-effort deep link to view an existing event in the Google Calendar
    web UI, for display in the family web app (Phase 3). Uses the
    widely-observed `eid` query parameter: URL-safe base64 of
    "<event_id> <calendar_id>". Returns None if either id is missing so
    callers never render a broken link — this is read-only display, never
    used to authorise or perform any action."""
    if not google_event_id or not calendar_id:
        return None
    raw = f"{google_event_id} {calendar_id}".encode("utf-8")
    eid = base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")
    return f"https://www.google.com/calendar/event?eid={eid}"


def passes_auto_create_gate(parsed_action: ParsedAction) -> tuple[bool, list[str]]:
    """Re-validate every condition required before an event may be created
    automatically. Confidence alone never authorises creation, and sender/
    type/title/date/duplicate checks below are still hard blocks.

    Everything else is deliberately permissive: a title + date is enough to
    post. A missing start time defaults to DEFAULT_START_TIME rather than
    blocking; other self-reported missing/ambiguous fields (no end time, no
    reminder recipient, etc.) no longer block automatic posting either — the
    confirmation reply reports them instead (assistant/services/
    email_sender.py), and a human can still fix them afterwards via the
    calendar or Review."""
    reasons = []
    email = parsed_action.incoming_email

    if not is_authorised_sender(email.outer_sender):
        reasons.append("Sender is not authorised.")

    if parsed_action.action_type != ParsedAction.ActionType.CREATE_CALENDAR_EVENT:
        reasons.append("Action type is not create_calendar_event.")

    if not (parsed_action.title or "").strip():
        reasons.append("Title is empty.")

    appt_date = parsed_action.appointment_date
    if appt_date is None:
        reasons.append("Appointment date is missing or invalid.")
    else:
        today = timezone.localdate()
        if not (today <= appt_date <= today + timedelta(days=365)):
            reasons.append("Appointment date is not today or within the next 365 days.")

    # Persisted here (not just in create_event_for_parsed_action) so the
    # duplicate check just below and the eventual creation both see the
    # same resolved time, and so it's recorded even if this turns out to
    # be a duplicate/reschedule that isn't actually (re-)created.
    if not parsed_action.all_day and parsed_action.start_time is None:
        parsed_action.start_time = DEFAULT_START_TIME
        if DEFAULTED_START_TIME_NOTE not in (parsed_action.ambiguity_notes or []):
            parsed_action.ambiguity_notes = list(parsed_action.ambiguity_notes or []) + [DEFAULTED_START_TIME_NOTE]
        parsed_action.save(update_fields=["start_time", "ambiguity_notes"])

    if appt_date and parsed_action.start_time and parsed_action.end_time:
        if not times_are_valid(appt_date, parsed_action.start_time, parsed_action.end_time):
            reasons.append("End time is not after start time.")

    if parsed_action.confidence < settings.AUTOMATIC_ACTION_CONFIDENCE_THRESHOLD:
        reasons.append("Confidence is below the automatic action threshold.")

    calendar_id = settings.GOOGLE_CALENDAR_ID
    if not reasons and appt_date is not None:
        kind, _existing = check_for_duplicate(parsed_action, calendar_id)
        if kind == "duplicate":
            reasons.append("A matching calendar event already exists (duplicate).")
        elif kind == "reschedule":
            reasons.append("Booking reference matches an existing event with a different date/time.")

    return (len(reasons) == 0, reasons)


def create_event_for_parsed_action(parsed_action: ParsedAction) -> CalendarEventRecord | None:
    """Single source of truth for turning a ParsedAction into a Google
    Calendar event. Used by both the automatic pipeline (after
    passes_auto_create_gate, via assistant.services.router) and the admin
    'Approve pending parsed actions' action (after human review/correction)
    — no event-creation logic is duplicated elsewhere.

    Returns the created (or pre-existing, for an exact duplicate)
    CalendarEventRecord, or None if this was a possible-reschedule case
    (parsed_action is updated to pending_review / requires_review instead)."""
    calendar_id = settings.GOOGLE_CALENDAR_ID
    if not calendar_id:
        raise RuntimeError("GOOGLE_CALENDAR_ID is not configured.")

    title = (parsed_action.title or "").strip()
    if not title:
        raise ValueError("Cannot create an event without a title.")

    appt_date = parsed_action.appointment_date
    if appt_date is None:
        raise ValueError("Cannot create an event without a valid appointment date.")

    # Same default as passes_auto_create_gate, repeated here (idempotent —
    # a no-op if the gate already set it) so admin/Review approval, which
    # calls this function directly without going through the gate, gets
    # the same "title + date is enough" behaviour.
    if not parsed_action.all_day and parsed_action.start_time is None:
        parsed_action.start_time = DEFAULT_START_TIME
        if DEFAULTED_START_TIME_NOTE not in (parsed_action.ambiguity_notes or []):
            parsed_action.ambiguity_notes = list(parsed_action.ambiguity_notes or []) + [DEFAULTED_START_TIME_NOTE]
        parsed_action.save(update_fields=["start_time", "ambiguity_notes"])

    if parsed_action.start_time and parsed_action.end_time:
        if not times_are_valid(appt_date, parsed_action.start_time, parsed_action.end_time):
            raise ValueError("End time must be after start time.")

    kind, existing = check_for_duplicate(parsed_action, calendar_id)
    if kind == "duplicate":
        parsed_action.status = ParsedAction.Status.REJECTED
        parsed_action.failure_reason = "Duplicate of an existing calendar event; not created again."
        parsed_action.save(update_fields=["status", "failure_reason"])
        return existing
    if kind == "reschedule":
        # Not an error: a deliberate business outcome. Do not create a second
        # event automatically, and do not update the existing one.
        parsed_action.status = ParsedAction.Status.PENDING_REVIEW
        parsed_action.action_type = ParsedAction.ActionType.REQUIRES_REVIEW
        parsed_action.ambiguity_notes = list(parsed_action.ambiguity_notes or []) + [
            "Booking reference matches an existing event with a different date/time (possible reschedule)."
        ]
        parsed_action.save(update_fields=["status", "action_type", "ambiguity_notes"])
        return None

    start_dt = end_dt = None
    if not parsed_action.all_day:
        start_dt = combine_date_and_time(appt_date, parsed_action.start_time)
        if parsed_action.end_time:
            end_dt = combine_date_and_time(appt_date, parsed_action.end_time)
        else:
            end_dt = start_dt + timedelta(hours=1)
            # Persist the resolved end time (not just used for the Google
            # event body) so CalendarEventRecord.end_time, the web app's
            # calendar/upcoming views, and the confirmation reply all agree
            # on what was actually scheduled.
            parsed_action.end_time = end_dt.time()
            if DEFAULTED_END_TIME_NOTE not in (parsed_action.ambiguity_notes or []):
                parsed_action.ambiguity_notes = list(parsed_action.ambiguity_notes or []) + [DEFAULTED_END_TIME_NOTE]
            parsed_action.save(update_fields=["end_time", "ambiguity_notes"])

    description_lines = []
    if parsed_action.meeting_url:
        description_lines.append(f"Meeting link: {parsed_action.meeting_url}")
    if parsed_action.booking_reference:
        description_lines.append(f"Booking reference: {parsed_action.booking_reference}")
    description = "\n".join(description_lines)

    recurrence = build_recurrence(
        parsed_action.recurrence_frequency or None,
        parsed_action.recurrence_interval,
        parsed_action.recurrence_days_of_week,
        parsed_action.recurrence_until,
        parsed_action.recurrence_count,
        appt_date,
        parsed_action.all_day,
    )
    if recurrence.note and recurrence.note not in (parsed_action.ambiguity_notes or []):
        parsed_action.ambiguity_notes = list(parsed_action.ambiguity_notes or []) + [recurrence.note]
        parsed_action.save(update_fields=["ambiguity_notes"])

    event_body = {
        "summary": title,
        "location": parsed_action.location or "",
        "description": description,
        "extendedProperties": {
            "private": {"source_gmail_message_id": parsed_action.incoming_email.gmail_message_id}
        },
    }
    if parsed_action.all_day:
        event_body["start"] = {"date": appt_date.isoformat()}
        event_body["end"] = {"date": (appt_date + timedelta(days=1)).isoformat()}
    else:
        event_body["start"] = {"dateTime": start_dt.isoformat(), "timeZone": settings.APP_TIMEZONE}
        event_body["end"] = {"dateTime": end_dt.isoformat(), "timeZone": settings.APP_TIMEZONE}
    if recurrence.rrule:
        event_body["recurrence"] = [recurrence.rrule]

    service = build("calendar", "v3", credentials=get_google_credentials(), cache_discovery=False)
    created_event = service.events().insert(calendarId=calendar_id, body=event_body).execute()

    key = compute_duplicate_key(
        title, appt_date, _start_time_or_all_day_str(parsed_action), calendar_id, parsed_action.recurrence_frequency
    )

    record = CalendarEventRecord.objects.create(
        parsed_action=parsed_action,
        incoming_email=parsed_action.incoming_email,
        google_event_id=created_event.get("id", ""),
        calendar_id=calendar_id,
        title=title,
        appointment_date=appt_date,
        start_time=parsed_action.start_time,
        end_time=parsed_action.end_time,
        timezone=settings.APP_TIMEZONE,
        all_day=parsed_action.all_day,
        location=parsed_action.location or "",
        booking_reference=parsed_action.booking_reference or "",
        recurrence_rule=recurrence.rrule or "",
        recurrence_description=recurrence.description or "",
        duplicate_key=key,
    )

    parsed_action.duplicate_key = key
    parsed_action.status = ParsedAction.Status.EXECUTED
    parsed_action.executed_at = timezone.now()
    parsed_action.save(update_fields=["duplicate_key", "status", "executed_at"])

    return record
