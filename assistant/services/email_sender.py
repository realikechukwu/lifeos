"""Builds and sends the plain-language confirmation reply. Always replies to
the authorised outer sender of the incoming email — never to an address
found inside the (untrusted) email body or extracted data."""

from core.models import IncomingEmail, ParsedAction

from .gmail import GmailService


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

# Maps ParsedAction.missing_fields entries to a plain-English noun phrase.
# start_time/end_time collapse to the same word so "the time" isn't repeated.
_MISSING_FIELD_WORDS = {
    "title": "title",
    "appointment_date": "date",
    "start_time": "time",
    "end_time": "time",
    "due_date": "due date",
    "reminder_date": "reminder date",
    "reminder_time": "reminder time",
}


def _describe_missing_details(parsed_action: ParsedAction) -> str:
    """Best-effort plain-English description of what was unclear, e.g. 'the
    time' or 'the date and location'. Falls back to a generic phrase when
    the specifics aren't in a field we recognise."""
    words = []
    for field in parsed_action.missing_fields or []:
        word = _MISSING_FIELD_WORDS.get(field)
        if word and word not in words:
            words.append(word)
    if not words:
        return "all the details"
    if len(words) == 1:
        return f"the {words[0]}"
    return "the " + " and ".join(words)


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
        if outcome == "created_with_reminder" and context.get("reminder") is not None:
            reminder = context["reminder"]
            recipient_label = RECIPIENT_LABELS.get(reminder.recipient, reminder.recipient)
            body += (
                f" I'll also email {recipient_label} on {_format_date_human(reminder.reminder_date)} "
                f"at {_format_time_human(reminder.reminder_time)} as a reminder."
            )
        elif outcome == "created_reminder_unclear":
            body += " I could not confirm the reminder details, so I did not schedule a reminder for it."
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
        subject=incoming_email.subject or "Your email",
        body_text=body,
    )
