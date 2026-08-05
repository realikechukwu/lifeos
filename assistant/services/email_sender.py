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
            return (
                f"I found an appointment for {date_str}, but I could not confidently identify "
                "all the details. I saved it for review and did not add it to the calendar."
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
