"""Reminder creation and recipient resolution.

Recipients are never derived from email addresses found in message content —
only from the closed `ike` / `wife` / `both` choice, resolved to real
addresses solely via AUTHORISED_EMAIL_IKE / AUTHORISED_EMAIL_WIFE. This is
the single source of truth for both the automatic email pipeline and the
Django admin approval action.
"""

from datetime import date, time

from django.conf import settings
from django.utils import timezone

from core.models import AuditLog, ParsedAction, Reminder, RecipientTarget, normalise_email

from .extractor import combine_date_and_time, is_plausible_date
from .gmail import is_authorised_sender


def resolve_recipient_emails(target: str) -> list[str]:
    """The only place a Reminder recipient choice becomes a real email
    address. Never derived from anything in the email body."""
    ike = normalise_email(settings.AUTHORISED_EMAIL_IKE)
    wife = normalise_email(settings.AUTHORISED_EMAIL_WIFE)
    if target == RecipientTarget.IKE:
        addrs = [ike]
    elif target == RecipientTarget.WIFE:
        addrs = [wife]
    elif target == RecipientTarget.BOTH:
        addrs = [ike, wife]
    else:
        addrs = []
    return [a for a in addrs if a]


def evaluate_reminder_fields(
    reminder_date: date | None,
    reminder_time: time | None,
    recipient: str | None,
    confidence: float,
    has_ambiguity: bool,
) -> tuple[bool, list[str]]:
    """Deterministic checks shared by standalone reminders and reminders
    linked to a calendar event. Confidence is only ever a secondary veto —
    every other check here is a hard gate."""
    reasons = []

    if reminder_date is None:
        reasons.append("Reminder date is missing or could not be parsed.")
    elif not is_plausible_date(reminder_date):
        reasons.append("Reminder date is not plausible.")

    if reminder_time is None:
        reasons.append("Reminder time is missing or could not be parsed.")

    if recipient not in RecipientTarget.values:
        reasons.append("Reminder recipient is not resolved to ike, wife, or both.")

    if reminder_date is not None and reminder_time is not None:
        when = combine_date_and_time(reminder_date, reminder_time)
        if when <= timezone.now():
            reasons.append("Reminder date/time is not in the future.")

    if recipient in RecipientTarget.values and not resolve_recipient_emails(recipient):
        reasons.append("No authorised email address is configured for the requested recipient.")

    return (len(reasons) == 0, reasons)


def passes_reminder_auto_create_gate(parsed_action: ParsedAction) -> tuple[bool, list[str]]:
    reasons = []
    email = parsed_action.incoming_email

    if not is_authorised_sender(email.outer_sender):
        reasons.append("Sender is not authorised.")

    if parsed_action.action_type != ParsedAction.ActionType.CREATE_EMAIL_REMINDER:
        reasons.append("Action type is not create_email_reminder.")

    if not (parsed_action.title or "").strip():
        reasons.append("Title is missing.")

    ok, field_reasons = evaluate_reminder_fields(
        parsed_action.reminder_date,
        parsed_action.reminder_time,
        parsed_action.reminder_recipient,
        parsed_action.confidence,
        bool(parsed_action.ambiguity_notes),
    )
    if not ok:
        reasons.extend(field_reasons)

    return (len(reasons) == 0, reasons)


def create_reminder_for_parsed_action(parsed_action: ParsedAction) -> Reminder:
    """Single execution path for a standalone create_email_reminder action."""
    return _create_reminder(
        title=(parsed_action.title or "").strip() or "Reminder",
        message=parsed_action.description or "",
        recipient=parsed_action.reminder_recipient,
        reminder_date=parsed_action.reminder_date,
        reminder_time=parsed_action.reminder_time,
        source_email=parsed_action.incoming_email,
        source_parsed_action=parsed_action,
        mark_action_executed=True,
    )


def create_linked_event_reminder(
    parsed_action: ParsedAction,
    *,
    reminder_date: date,
    reminder_time: time,
    calendar_event=None,
) -> Reminder:
    """Single execution path for a reminder linked to a create_calendar_event
    action. Does not touch parsed_action.status — the calendar event branch
    already owns that."""
    return _create_reminder(
        title=f"Reminder: {parsed_action.title or 'upcoming event'}",
        message=parsed_action.description or "",
        recipient=parsed_action.reminder_recipient,
        reminder_date=reminder_date,
        reminder_time=reminder_time,
        source_email=parsed_action.incoming_email,
        source_parsed_action=parsed_action,
        related_calendar_event=calendar_event,
        mark_action_executed=False,
    )


def _create_reminder(
    *,
    title: str,
    message: str,
    recipient: str,
    reminder_date: date,
    reminder_time: time,
    source_email,
    source_parsed_action: ParsedAction,
    related_task=None,
    related_calendar_event=None,
    mark_action_executed: bool,
) -> Reminder:
    if reminder_date is None or reminder_time is None:
        raise ValueError("Cannot create a reminder without a valid date and time.")
    if recipient not in RecipientTarget.values:
        raise ValueError("Cannot create a reminder without a valid recipient (ike, wife, or both).")

    reminder = Reminder.objects.create(
        title=title,
        message=message,
        recipient=recipient,
        reminder_date=reminder_date,
        reminder_time=reminder_time,
        timezone=settings.APP_TIMEZONE,
        related_task=related_task,
        related_calendar_event=related_calendar_event,
        source_email=source_email,
        source_parsed_action=source_parsed_action,
        status=Reminder.Status.PENDING,
    )

    if mark_action_executed:
        source_parsed_action.status = ParsedAction.Status.EXECUTED
        source_parsed_action.executed_at = timezone.now()
        source_parsed_action.save(update_fields=["status", "executed_at"])

    AuditLog.objects.create(
        source_email=source_email,
        action="create_reminder",
        object_type="Reminder",
        object_id=str(reminder.id),
        success=True,
        details={
            "title": reminder.title,
            "recipient": reminder.recipient,
            "reminder_date": str(reminder.reminder_date),
            "reminder_time": str(reminder.reminder_time),
        },
    )
    return reminder
