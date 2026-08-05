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


# Maps ParsedAction.missing_fields entries to a plain-English noun phrase.
# start_time/end_time collapse to the same word so "the time" isn't repeated.
_MISSING_FIELD_WORDS = {
    "title": "title",
    "appointment_date": "date",
    "start_time": "time",
    "end_time": "time",
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


def build_confirmation_body(outcome: str, parsed_action: ParsedAction | None = None) -> str:
    """outcome is one of: 'created', 'pending_review', 'attachment_review', 'unsupported'."""

    if outcome == "created" and parsed_action is not None:
        title = parsed_action.title or "the appointment"
        when = ""
        if parsed_action.appointment_date:
            date_str = _format_date_human(parsed_action.appointment_date)
            if parsed_action.all_day:
                when = f" for {date_str}"
            elif parsed_action.start_time:
                when = f" for {date_str} at {_format_time_human(parsed_action.start_time)}"
            else:
                when = f" for {date_str}"
        return f'I added "{title}" to the shared calendar{when}.'

    if outcome == "pending_review":
        if parsed_action is not None and parsed_action.appointment_date:
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

    if outcome == "attachment_review":
        return (
            "I could not find enough appointment information in the email body. The details may "
            "be in the attached document, so I saved this for review."
        )

    # outcome == "unsupported" (or anything else)
    return "I could not find a clear appointment in this email, so I did not take any action."


def send_confirmation(
    incoming_email: IncomingEmail,
    outcome: str,
    parsed_action: ParsedAction | None = None,
    gmail_service: GmailService | None = None,
) -> str:
    body = build_confirmation_body(outcome, parsed_action)
    service = gmail_service or GmailService()
    return service.send_reply(
        thread_id=incoming_email.gmail_thread_id,
        to_addr=incoming_email.outer_sender,
        subject=incoming_email.subject or "Your appointment email",
        body_text=body,
    )
