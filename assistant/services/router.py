"""Routes one validated AssistantAction extraction to the correct side
effect. Each email produces exactly one ParsedAction plus, at most, one
primary object (calendar event / task / note / reminder) and, only for
create_calendar_event, one linked reminder when explicitly requested.

This is the single place that decides what an incoming email is allowed to
do — the management commands and the admin actions both call into it rather
than re-implementing any of this dispatch logic.
"""

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from core.models import AuditLog, HouseholdMember, ParsedAction

from .extractor import parse_24h_time, parse_iso_date
from .gmail import is_authorised_sender
from .google_calendar import create_event_for_parsed_action, passes_auto_create_gate
from .notes import create_note_for_parsed_action, passes_note_auto_create_gate
from .reminders import (
    create_linked_event_reminder,
    create_reminder_for_parsed_action,
    evaluate_reminder_fields,
    passes_reminder_auto_create_gate,
)
from .tasks import complete_task, create_task_for_parsed_action, find_matching_open_tasks, passes_task_auto_create_gate

ACTION_TYPE = ParsedAction.ActionType


def build_parsed_action_from_extraction(incoming_email, extraction) -> ParsedAction:
    """Build the ParsedAction row from an AssistantAction, parsing every
    date/time string deterministically in Python and recording any that
    failed to parse as an ambiguity note (never silently dropped)."""
    appt_date = parse_iso_date(extraction.appointment_date)
    start_time = parse_24h_time(extraction.start_time)
    end_time = parse_24h_time(extraction.end_time)
    due_date = parse_iso_date(extraction.due_date)
    due_time = parse_24h_time(extraction.due_time)
    reminder_date = parse_iso_date(extraction.reminder_date)
    reminder_time = parse_24h_time(extraction.reminder_time)

    missing_fields = list(extraction.missing_fields)
    ambiguity_notes = list(extraction.ambiguity_notes)

    def _note_unparsable(raw, parsed, label):
        if raw and parsed is None:
            ambiguity_notes.append(f"Could not parse the {label} returned by extraction.")

    _note_unparsable(extraction.appointment_date, appt_date, "appointment date")
    _note_unparsable(extraction.start_time, start_time, "start time")
    _note_unparsable(extraction.end_time, end_time, "end time")
    _note_unparsable(extraction.due_date, due_date, "due date")
    _note_unparsable(extraction.due_time, due_time, "due time")
    _note_unparsable(extraction.reminder_date, reminder_date, "reminder date")
    _note_unparsable(extraction.reminder_time, reminder_time, "reminder time")

    related_member = None
    if extraction.related_person in ("ike", "wife"):
        related_member = HouseholdMember.objects.filter(role=extraction.related_person, active=True).first()

    return ParsedAction.objects.create(
        incoming_email=incoming_email,
        action_type=extraction.action_type,
        extracted_data=extraction.model_dump(),
        title=extraction.title or "",
        description=extraction.description or "",
        appointment_date=appt_date,
        start_time=start_time,
        end_time=end_time,
        all_day=extraction.all_day,
        location=extraction.location or "",
        meeting_url=extraction.meeting_url or "",
        organiser=extraction.organiser or "",
        booking_reference=extraction.booking_reference or "",
        due_date=due_date,
        due_time=due_time,
        assigned_to=extraction.assigned_to or "",
        task_search_text=extraction.task_search_text or "",
        note_category=extraction.note_category or "",
        reminder_date=reminder_date,
        reminder_time=reminder_time,
        reminder_recipient=extraction.reminder_recipient or "",
        reminder_lead_days=extraction.reminder_lead_days,
        related_household_member=related_member,
        confidence=extraction.confidence,
        missing_fields=missing_fields,
        ambiguity_notes=ambiguity_notes,
        evidence=list(extraction.evidence),
        status=ParsedAction.Status.PROPOSED,
    )


def _defer_to_review(parsed_action: ParsedAction, reasons: list[str], *, unsupported: bool) -> tuple[str, dict]:
    parsed_action.status = ParsedAction.Status.PENDING_REVIEW
    if reasons:
        parsed_action.ambiguity_notes = list(parsed_action.ambiguity_notes) + reasons
        parsed_action.save(update_fields=["status", "ambiguity_notes"])
    else:
        parsed_action.save(update_fields=["status"])
    return ("unsupported" if unsupported else "pending_review"), {}


def _maybe_create_linked_reminder(parsed_action: ParsedAction, calendar_event) -> tuple[str | None, dict]:
    """Only for create_calendar_event. Returns (note_for_reply, extra) where
    note_for_reply is None if no reminder was requested at all."""
    wants_reminder = any([
        parsed_action.reminder_date,
        parsed_action.reminder_time,
        parsed_action.reminder_recipient,
        parsed_action.reminder_lead_days,
    ])
    if not wants_reminder or calendar_event is None:
        return None, {}

    reminder_date = parsed_action.reminder_date
    reminder_time = parsed_action.reminder_time
    conflict = False

    if parsed_action.reminder_lead_days is not None and calendar_event.appointment_date:
        computed_date = calendar_event.appointment_date - timedelta(days=parsed_action.reminder_lead_days)
        if reminder_date and reminder_date != computed_date:
            conflict = True
        reminder_date = computed_date
        if reminder_time is None and not calendar_event.all_day and calendar_event.start_time:
            # Documented household default: a "day before" reminder with no
            # explicit time uses the event's own start time. There is no
            # other configured household default at this time.
            reminder_time = calendar_event.start_time

    ok, reasons = evaluate_reminder_fields(
        reminder_date,
        reminder_time,
        parsed_action.reminder_recipient,
        parsed_action.confidence,
        conflict,
    )
    if not ok:
        AuditLog.objects.create(
            source_email=parsed_action.incoming_email,
            action="reminder_requires_review",
            object_type="ParsedAction",
            object_id=str(parsed_action.id),
            success=False,
            details={"reasons": reasons},
        )
        return "unclear", {}

    reminder = create_linked_event_reminder(
        parsed_action,
        reminder_date=reminder_date,
        reminder_time=reminder_time,
        calendar_event=calendar_event,
    )
    return "created", {"reminder": reminder}


def _execute_mark_task_complete(parsed_action: ParsedAction) -> tuple[str, dict]:
    search_text = (parsed_action.task_search_text or parsed_action.title or "").strip()
    if not search_text:
        parsed_action.status = ParsedAction.Status.PENDING_REVIEW
        parsed_action.ambiguity_notes = list(parsed_action.ambiguity_notes) + [
            "No task title/search text was given to match against."
        ]
        parsed_action.save(update_fields=["status", "ambiguity_notes"])
        return "pending_review", {}

    matches = find_matching_open_tasks(search_text)

    if len(matches) == 0:
        parsed_action.status = ParsedAction.Status.REJECTED
        parsed_action.failure_reason = f"No matching open task found for '{search_text}'."
        parsed_action.save(update_fields=["status", "failure_reason"])
        AuditLog.objects.create(
            source_email=parsed_action.incoming_email,
            action="task_completion_not_found",
            object_type="ParsedAction",
            object_id=str(parsed_action.id),
            success=False,
            details={"search_text": search_text},
        )
        return "task_not_found", {}

    if len(matches) > 1:
        parsed_action.status = ParsedAction.Status.PENDING_REVIEW
        parsed_action.ambiguity_notes = list(parsed_action.ambiguity_notes) + [
            f"Multiple open tasks match '{search_text}': " + ", ".join(t.title for t in matches)
        ]
        parsed_action.save(update_fields=["status", "ambiguity_notes"])
        AuditLog.objects.create(
            source_email=parsed_action.incoming_email,
            action="task_completion_ambiguous",
            object_type="ParsedAction",
            object_id=str(parsed_action.id),
            success=False,
            details={"search_text": search_text, "candidate_ids": [t.id for t in matches]},
        )
        return "task_ambiguous", {}

    task = complete_task(matches[0], source_email=parsed_action.incoming_email)
    parsed_action.status = ParsedAction.Status.EXECUTED
    parsed_action.executed_at = timezone.now()
    parsed_action.save(update_fields=["status", "executed_at"])
    return "task_completed", {"task": task}


def route_and_execute(parsed_action: ParsedAction) -> tuple[str, dict]:
    """Runs the deterministic auto-create gate for the action's type, then
    executes it. Returns (outcome, extra) where extra carries the created
    object(s) for building the confirmation reply."""
    action_type = parsed_action.action_type

    if action_type == ACTION_TYPE.UNSUPPORTED:
        return _defer_to_review(parsed_action, [], unsupported=True)

    if action_type == ACTION_TYPE.REQUIRES_REVIEW:
        return _defer_to_review(parsed_action, ["Extraction flagged this as requiring review."], unsupported=False)

    if action_type == ACTION_TYPE.MARK_TASK_COMPLETE:
        if not is_authorised_sender(parsed_action.incoming_email.outer_sender):
            return _defer_to_review(parsed_action, ["Sender is not authorised."], unsupported=False)
        return _execute_mark_task_complete(parsed_action)

    gate_fn = {
        ACTION_TYPE.CREATE_CALENDAR_EVENT: passes_auto_create_gate,
        ACTION_TYPE.CREATE_TASK: passes_task_auto_create_gate,
        ACTION_TYPE.CREATE_NOTE: passes_note_auto_create_gate,
        ACTION_TYPE.CREATE_EMAIL_REMINDER: passes_reminder_auto_create_gate,
    }.get(action_type)

    if gate_fn is None:
        return _defer_to_review(parsed_action, ["Action type does not support automatic execution."], unsupported=False)

    gate_ok, reasons = gate_fn(parsed_action)
    if not gate_ok:
        return _defer_to_review(parsed_action, reasons, unsupported=False)

    try:
        if action_type == ACTION_TYPE.CREATE_CALENDAR_EVENT:
            calendar_event = create_event_for_parsed_action(parsed_action)
            parsed_action.refresh_from_db()
            if parsed_action.status != ParsedAction.Status.EXECUTED:
                return "pending_review", {}
            reminder_result, extra = _maybe_create_linked_reminder(parsed_action, calendar_event)
            extra["calendar_event"] = calendar_event
            if reminder_result == "created":
                return "created_with_reminder", extra
            if reminder_result == "unclear":
                return "created_reminder_unclear", extra
            return "created", extra

        if action_type == ACTION_TYPE.CREATE_TASK:
            task = create_task_for_parsed_action(parsed_action)
            return "task_created", {"task": task}

        if action_type == ACTION_TYPE.CREATE_NOTE:
            note = create_note_for_parsed_action(parsed_action)
            return "note_created", {"note": note}

        if action_type == ACTION_TYPE.CREATE_EMAIL_REMINDER:
            reminder = create_reminder_for_parsed_action(parsed_action)
            return "reminder_created", {"reminder": reminder}

    except (ValueError, RuntimeError) as exc:
        parsed_action.refresh_from_db()
        if parsed_action.status not in (ParsedAction.Status.PENDING_REVIEW, ParsedAction.Status.REJECTED):
            parsed_action.status = ParsedAction.Status.FAILED
            parsed_action.failure_reason = str(exc)
            parsed_action.save(update_fields=["status", "failure_reason"])
        return "pending_review", {}

    return _defer_to_review(parsed_action, ["Action type does not support automatic execution."], unsupported=False)


def admin_approve_and_execute(parsed_action: ParsedAction) -> tuple[str, dict]:
    """Used by the Django admin 'Approve pending parsed actions' action,
    after a human has reviewed (and possibly corrected) the fields. Skips
    the confidence/ambiguity gate — a human already vouched for this — but
    still calls the exact same creation functions as automatic processing,
    which still enforce hard invariants (title present, valid date, etc.)."""
    action_type = parsed_action.action_type

    if action_type == ACTION_TYPE.CREATE_CALENDAR_EVENT:
        calendar_event = create_event_for_parsed_action(parsed_action)
        parsed_action.refresh_from_db()
        outcome = "created" if parsed_action.status == ParsedAction.Status.EXECUTED else "pending_review"
        return outcome, {"calendar_event": calendar_event}

    if action_type == ACTION_TYPE.CREATE_TASK:
        return "task_created", {"task": create_task_for_parsed_action(parsed_action)}

    if action_type == ACTION_TYPE.CREATE_NOTE:
        return "note_created", {"note": create_note_for_parsed_action(parsed_action)}

    if action_type == ACTION_TYPE.CREATE_EMAIL_REMINDER:
        return "reminder_created", {"reminder": create_reminder_for_parsed_action(parsed_action)}

    if action_type == ACTION_TYPE.MARK_TASK_COMPLETE:
        return _execute_mark_task_complete(parsed_action)

    raise ValueError(f"Action type '{action_type}' cannot be manually approved/executed.")
