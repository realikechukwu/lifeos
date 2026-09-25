"""Builds and sends the plain-language confirmation reply. Always replies to
the authorised outer sender of the incoming email — never to an address
found inside the (untrusted) email body or extracted data. May additionally
CC the other household member, but only when explicitly requested
(ParsedAction.notify_both) and only with one of the two closed, configured
addresses — same trust boundary as resolve_recipient_emails()."""

from core.models import IncomingEmail, ParsedAction, RecipientTarget, normalise_email

from .gmail import GmailService
from .google_calendar import DEFAULTED_END_TIME_NOTE, DEFAULTED_START_TIME_NOTE
from .reminders import resolve_recipient_emails


def _format_time_human(t) -> str:
    hour = t.hour % 12 or 12
    period = "am" if t.hour < 12 else "pm"
    if t.minute == 0:
        return f"{hour}{period}"
    return f"{hour}:{t.minute:02d}{period}"


def _format_date_human(d) -> str:
    return d.strftime("%A %d %B %Y")


def _format_date_short(d) -> str:
    return f"{d.day} {d.strftime('%B')}"


ASSIGNEE_LABELS = {"ike": "Ike", "wife": "wife", "both": "both of you", "unassigned": ""}
RECIPIENT_LABELS = {"ike": "Ike", "wife": "wife", "both": "both of you"}

# Maps ParsedAction.missing_fields entries to a full plain-English phrase
# (each already includes its own article/wording, since not every field
# reads naturally as "the X" — e.g. "who the reminder should go to").
# start_time and end_time are kept distinct so a missing end time is never
# described as simply "the time", which reads as if the stated start time
# was the problem.
_MISSING_FIELD_PHRASES = {
    "title": "the title",
    "appointment_date": "the date",
    "start_time": "the start time",
    "end_time": "the end time",
    "due_date": "the due date",
    "reminder_date": "the reminder date",
    "reminder_time": "the reminder time",
    "reminder_recipient": "who the reminder should go to",
}


def _describe_missing_details(parsed_action: ParsedAction) -> str:
    """Best-effort plain-English description of what was unclear, e.g. 'the
    end time' or 'the end time and who the reminder should go to'. Falls
    back to a generic phrase when the specifics aren't in a field we
    recognise."""
    phrases = []
    for field in parsed_action.missing_fields or []:
        phrase = _MISSING_FIELD_PHRASES.get(field)
        if phrase and phrase not in phrases:
            phrases.append(phrase)
    if not phrases:
        return "all the details"
    if len(phrases) == 1:
        return phrases[0]
    return " and ".join(phrases)


def _describe_other_gaps(parsed_action: ParsedAction, exclude: set[str]) -> str:
    """For calendar events that were still created despite a self-reported
    gap (per the 'a title + date is enough, never block, say what's left'
    policy — assistant/services/google_calendar.py), mentions anything not
    already covered by a more specific sentence above it (defaulted start/
    end time, or the reminder-unclear sentence). Empty string if nothing
    is left to mention."""
    phrases = []
    for field in parsed_action.missing_fields or []:
        if field in exclude:
            continue
        phrase = _MISSING_FIELD_PHRASES.get(field)
        if phrase and phrase not in phrases:
            phrases.append(phrase)
    if not phrases:
        return ""
    if len(phrases) == 1:
        return f"I also wasn't sure about {phrases[0]}."
    return "I also wasn't sure about " + " and ".join(phrases) + "."


def _event_when_phrase(parsed_action: ParsedAction) -> str:
    if not parsed_action.appointment_date:
        return ""
    date_str = _format_date_human(parsed_action.appointment_date)
    if parsed_action.all_day:
        return f" for {date_str}"
    if parsed_action.start_time:
        return f" for {date_str} at {_format_time_human(parsed_action.start_time)}"
    return f" for {date_str}"


def build_confirmation_body(outcome: str, parsed_action: ParsedAction | None = None, context: dict | None = None) -> str:
    context = context or {}

    if outcome in ("created", "created_with_reminder", "created_reminder_unclear") and parsed_action is not None:
        title = parsed_action.title or "the appointment"
        when = _event_when_phrase(parsed_action)
        body = f'I added "{title}" to the shared calendar{when}.'

        notes = parsed_action.ambiguity_notes or []
        if DEFAULTED_START_TIME_NOTE in notes:
            body += " I wasn't given a start time, so I used 9:00am — please adjust if that's wrong."
        if DEFAULTED_END_TIME_NOTE in notes:
            body += " No end time was given, so I scheduled it for 1 hour."

        calendar_event = context.get("calendar_event")
        if calendar_event is not None and getattr(calendar_event, "recurrence_description", ""):
            body += f" {calendar_event.recurrence_description}."

        exclude_from_gaps = {"title", "appointment_date", "start_time", "end_time"}
        if outcome == "created_with_reminder" and context.get("reminder") is not None:
            reminder = context["reminder"]
            recipient_label = RECIPIENT_LABELS.get(reminder.recipient, reminder.recipient)
            body += (
                f" I'll also email {recipient_label} on {_format_date_human(reminder.reminder_date)} "
                f"at {_format_time_human(reminder.reminder_time)} as a reminder."
            )
            exclude_from_gaps.add("reminder_recipient")
        elif outcome == "created_reminder_unclear":
            body += " I could not confirm the reminder details, so I did not schedule a reminder for it."
            exclude_from_gaps.add("reminder_recipient")

        gap_note = _describe_other_gaps(parsed_action, exclude_from_gaps)
        if gap_note:
            body += " " + gap_note
        return body

    if outcome == "task_created" and context.get("task") is not None:
        task = context["task"]
        assignee = ASSIGNEE_LABELS.get(task.assigned_to, "")
        who = f" for {assignee}" if assignee else ""
        when = f", due {_format_date_short(task.due_date)}" if task.due_date else ""
        return f'I created a task{who}: "{task.title}"{when}.'

    if outcome == "note_created" and context.get("note") is not None:
        note = context["note"]
        return f'I saved a note: "{note.title}".'

    if outcome == "reminder_created" and context.get("reminder") is not None:
        reminder = context["reminder"]
        recipient_label = RECIPIENT_LABELS.get(reminder.recipient, reminder.recipient)
        return (
            f"I'll email {recipient_label} on {_format_date_human(reminder.reminder_date)} at "
            f"{_format_time_human(reminder.reminder_time)} to {reminder.title.lower() if reminder.title else 'follow up'}."
        )

    if outcome == "task_completed" and context.get("task") is not None:
        return f'I marked "{context["task"].title}" as complete.'

    if outcome == "task_not_found":
        return "I could not find a matching open task, so I did not mark anything complete."

    if outcome == "task_ambiguous":
        return (
            "I found more than one open task matching that description. I saved this for review "
            "and did not complete a task."
        )

    if outcome == "pending_review":
        if parsed_action is not None and parsed_action.action_type == ParsedAction.ActionType.CREATE_CALENDAR_EVENT:
            if parsed_action.appointment_date:
                date_str = _format_date_short(parsed_action.appointment_date)
                missing_desc = _describe_missing_details(parsed_action)
                return (
                    f"I found an appointment for {date_str}, but I could not confidently identify "
                    f"{missing_desc}. I saved it for review and did not add it to the calendar."
                )
            return (
                "I found a possible appointment in this email, but I could not confidently identify "
                "the details. I saved it for review and did not add it to the calendar."
            )
        return (
            "I found a request in this email, but I could not confidently confirm all the details. "
            "I saved it for review and have not taken any action yet."
        )

    if outcome == "attachment_review":
        return (
            "I could not find enough appointment information in the email body. The details may "
            "be in the attached document, so I saved this for review."
        )

    # outcome == "unsupported" (or anything else)
    return (
        "I can currently add calendar events, tasks, notes and email reminders. "
        "I have not taken any action on this request."
    )


def build_batch_confirmation_body(results: list[dict]) -> str:
    """Build one reply covering every independently handled request item."""
    if len(results) == 1 and results[0].get("outcome") != "failed":
        item = results[0]
        return build_confirmation_body(
            item["outcome"], item.get("parsed_action"), item.get("context")
        )

    lines = [f"I handled {len(results)} items from your message:"]
    for index, item in enumerate(results, start=1):
        parsed_action = item.get("parsed_action")
        if item.get("outcome") == "failed":
            title = (getattr(parsed_action, "title", "") or "this item").strip()
            detail = f'I could not complete "{title}". It was recorded as failed; the other items were unaffected.'
        else:
            detail = build_confirmation_body(
                item["outcome"], parsed_action, item.get("context")
            )
        lines.append(f"{index}. {detail}")
    return "\n\n".join(lines)


def _resolve_confirmation_cc(incoming_email: IncomingEmail, parsed_action: ParsedAction | None) -> str | None:
    """Only ever the *other* configured household address, and only when
    explicitly requested. `to_addr` (the outer sender) is never duplicated
    into the CC line."""
    if parsed_action is None or not parsed_action.notify_both:
        return None
    outer = normalise_email(incoming_email.outer_sender)
    others = [a for a in resolve_recipient_emails(RecipientTarget.BOTH) if normalise_email(a) != outer]
    return ", ".join(others) if others else None


def send_confirmation(
    incoming_email: IncomingEmail,
    outcome: str,
    parsed_action: ParsedAction | None = None,
    gmail_service: GmailService | None = None,
    context: dict | None = None,
) -> str:
    body = build_confirmation_body(outcome, parsed_action, context)
    service = gmail_service or GmailService()
    return service.send_reply(
        thread_id=incoming_email.gmail_thread_id,
        to_addr=incoming_email.outer_sender,
        cc_addr=_resolve_confirmation_cc(incoming_email, parsed_action),
        subject=incoming_email.subject or "Your email",
        body_text=body,
    )


def send_batch_confirmation(
    incoming_email: IncomingEmail,
    results: list[dict],
    gmail_service: GmailService | None = None,
) -> str:
    """Send exactly one reply for a batch; any explicit notify_both applies."""
    body = build_batch_confirmation_body(results)
    parsed_actions = [item.get("parsed_action") for item in results]
    notify_action = next(
        (action for action in parsed_actions if action is not None and action.notify_both),
        None,
    )
    service = gmail_service or GmailService()
    return service.send_reply(
        thread_id=incoming_email.gmail_thread_id,
        to_addr=incoming_email.outer_sender,
        cc_addr=_resolve_confirmation_cc(incoming_email, notify_action),
        subject=incoming_email.subject or "Your email",
        body_text=body,
    )
