"""python manage.py poll_gmail

Bounded, idempotent Gmail poll: fetch up to GMAIL_POLL_MAX_MESSAGES matching
messages, save them, reject unauthorised senders, extract + validate +
gate-check appointment details, create a calendar event or defer to review,
reply, label, and audit. Safe to run repeatedly.
"""

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import AuditLog, HouseholdMember, IncomingEmail, ParsedAction
from assistant.services.email_parser import ParsedEmail, parse_email_message
from assistant.services.email_sender import send_confirmation
from assistant.services.extractor import extract_appointment, parse_24h_time, parse_iso_date
from assistant.services.gmail import GmailService, is_authorised_sender
from assistant.services.google_calendar import create_event_for_parsed_action, passes_auto_create_gate

ATTACHMENT_ONLY_TEXT_THRESHOLD = 40  # characters; below this we assume the body has no real content


def _all_labels():
    return [
        settings.GMAIL_PROCESSED_LABEL,
        settings.GMAIL_PENDING_LABEL,
        settings.GMAIL_FAILED_LABEL,
        settings.GMAIL_UNAUTHORISED_LABEL,
    ]


def _save_incoming_email(existing: IncomingEmail | None, message_id: str, thread_id: str, parsed: ParsedEmail) -> IncomingEmail:
    fields = dict(
        gmail_thread_id=thread_id,
        outer_sender=parsed.from_addr,
        recipients=", ".join(parsed.to_addrs),
        subject=parsed.subject,
        body_text=parsed.cleaned_text,
        body_html_sanitised=parsed.body_html_sanitised,
        received_at=parsed.date,
        is_forwarded=parsed.is_forwarded,
        original_forwarded_sender=parsed.original_forwarded_sender,
        original_forwarded_date=parsed.original_forwarded_date,
        has_attachments=parsed.has_attachments,
        attachment_metadata=parsed.attachments,
        status=IncomingEmail.Status.RECEIVED,
    )
    if existing:
        for key, value in fields.items():
            setattr(existing, key, value)
        existing.save()
        return existing
    return IncomingEmail.objects.create(gmail_message_id=message_id, **fields)


def _build_parsed_action_from_extraction(incoming_email: IncomingEmail, extraction) -> ParsedAction:
    appt_date = parse_iso_date(extraction.appointment_date)
    start_time = parse_24h_time(extraction.start_time)
    end_time = parse_24h_time(extraction.end_time)

    missing_fields = list(extraction.missing_fields)
    ambiguity_notes = list(extraction.ambiguity_notes)
    if extraction.appointment_date and appt_date is None:
        ambiguity_notes.append("Could not parse the appointment date returned by extraction.")
    if extraction.start_time and start_time is None:
        ambiguity_notes.append("Could not parse the start time returned by extraction.")
    if extraction.end_time and end_time is None:
        ambiguity_notes.append("Could not parse the end time returned by extraction.")

    related_member = None
    if extraction.related_person in ("ike", "wife"):
        related_member = HouseholdMember.objects.filter(role=extraction.related_person, active=True).first()

    return ParsedAction.objects.create(
        incoming_email=incoming_email,
        action_type=extraction.action_type,
        extracted_data=extraction.model_dump(),
        title=extraction.title or "",
        appointment_date=appt_date,
        start_time=start_time,
        end_time=end_time,
        all_day=extraction.all_day,
        location=extraction.location or "",
        meeting_url=extraction.meeting_url or "",
        organiser=extraction.organiser or "",
        booking_reference=extraction.booking_reference or "",
        related_household_member=related_member,
        confidence=extraction.confidence,
        missing_fields=missing_fields,
        ambiguity_notes=ambiguity_notes,
        evidence=list(extraction.evidence),
        status=ParsedAction.Status.PROPOSED,
    )


def _run_extraction_and_gate(incoming_email: IncomingEmail) -> tuple[str, ParsedAction]:
    text = incoming_email.body_text or ""

    if len(text.strip()) < ATTACHMENT_ONLY_TEXT_THRESHOLD and incoming_email.has_attachments:
        parsed_action = ParsedAction.objects.create(
            incoming_email=incoming_email,
            action_type=ParsedAction.ActionType.REQUIRES_REVIEW,
            extracted_data={},
            status=ParsedAction.Status.PENDING_REVIEW,
            confidence=0.0,
            ambiguity_notes=["Appointment details may be contained in an attachment."],
        )
        return "attachment_review", parsed_action

    current_date = timezone.localdate()
    received_date = incoming_email.received_at.date() if incoming_email.received_at else current_date
    forwarded_date = incoming_email.original_forwarded_date.date() if incoming_email.original_forwarded_date else None

    extraction = extract_appointment(text, current_date, received_date, forwarded_date)
    parsed_action = _build_parsed_action_from_extraction(incoming_email, extraction)

    if extraction.action_type != "create_calendar_event":
        parsed_action.status = ParsedAction.Status.PENDING_REVIEW
        parsed_action.save(update_fields=["status"])
        outcome = "unsupported" if extraction.action_type == "unsupported" else "pending_review"
        return outcome, parsed_action

    gate_ok, reasons = passes_auto_create_gate(parsed_action)
    if not gate_ok:
        parsed_action.status = ParsedAction.Status.PENDING_REVIEW
        parsed_action.ambiguity_notes = list(parsed_action.ambiguity_notes) + reasons
        parsed_action.save(update_fields=["status", "ambiguity_notes"])
        return "pending_review", parsed_action

    try:
        create_event_for_parsed_action(parsed_action)
    except (ValueError, RuntimeError) as exc:
        parsed_action.refresh_from_db()
        if parsed_action.status not in (ParsedAction.Status.PENDING_REVIEW, ParsedAction.Status.REJECTED):
            parsed_action.status = ParsedAction.Status.FAILED
            parsed_action.failure_reason = str(exc)
            parsed_action.save(update_fields=["status", "failure_reason"])
        return "pending_review", parsed_action

    parsed_action.refresh_from_db()
    if parsed_action.status == ParsedAction.Status.EXECUTED:
        return "created", parsed_action
    return "pending_review", parsed_action


def process_incoming_email(incoming_email: IncomingEmail, gmail_service: GmailService | None = None, label_map: dict | None = None) -> None:
    """Reusable orchestrator: authorisation -> extraction -> validation ->
    auto-create gate -> calendar event or review -> reply -> label -> audit.
    Used by poll_gmail, retry_email, and the admin 'retry_selected_actions'
    action so idempotency and sender validation are never bypassed."""
    gmail_service = gmail_service or GmailService()
    label_map = label_map or gmail_service.ensure_labels(_all_labels())

    if not is_authorised_sender(incoming_email.outer_sender):
        incoming_email.status = IncomingEmail.Status.UNAUTHORISED
        incoming_email.save(update_fields=["status"])
        label_id = label_map.get(settings.GMAIL_UNAUTHORISED_LABEL)
        if label_id:
            try:
                gmail_service.apply_label(incoming_email.gmail_message_id, label_id)
            except Exception:
                pass
        AuditLog.objects.create(
            source_email=incoming_email,
            action="reject_unauthorised_sender",
            object_type="IncomingEmail",
            object_id=str(incoming_email.id),
            success=True,
            details={"outer_sender": incoming_email.outer_sender},
        )
        return

    incoming_email.status = IncomingEmail.Status.PROCESSING
    incoming_email.save(update_fields=["status"])

    try:
        outcome, parsed_action = _run_extraction_and_gate(incoming_email)
    except Exception as exc:  # noqa: BLE001 - genuine processing failure
        incoming_email.processing_attempts += 1
        incoming_email.status = IncomingEmail.Status.FAILED
        incoming_email.last_error = str(exc)[:2000]
        incoming_email.save(update_fields=["processing_attempts", "status", "last_error"])
        label_id = label_map.get(settings.GMAIL_FAILED_LABEL)
        if label_id:
            try:
                gmail_service.apply_label(incoming_email.gmail_message_id, label_id)
            except Exception:
                pass
        AuditLog.objects.create(
            source_email=incoming_email,
            action="process_incoming_email",
            object_type="IncomingEmail",
            object_id=str(incoming_email.id),
            success=False,
            error_message=str(exc)[:2000],
        )
        return

    incoming_email.processing_attempts += 1
    incoming_email.status = (
        IncomingEmail.Status.PROCESSED if outcome == "created" else IncomingEmail.Status.PENDING_REVIEW
    )
    incoming_email.save(update_fields=["processing_attempts", "status"])

    reply_ok, reply_error = True, ""
    try:
        send_confirmation(incoming_email, outcome, parsed_action, gmail_service=gmail_service)
    except Exception as exc:  # noqa: BLE001 - don't let a reply failure hide a successful action
        reply_ok, reply_error = False, str(exc)[:2000]

    label_name = settings.GMAIL_PROCESSED_LABEL if outcome == "created" else settings.GMAIL_PENDING_LABEL
    label_id = label_map.get(label_name)
    if label_id:
        try:
            gmail_service.apply_label(incoming_email.gmail_message_id, label_id)
        except Exception:
            pass

    AuditLog.objects.create(
        source_email=incoming_email,
        action="process_incoming_email",
        object_type="ParsedAction",
        object_id=str(parsed_action.id),
        success=reply_ok,
        details={"outcome": outcome},
        error_message=reply_error,
    )


class Command(BaseCommand):
    help = "Poll Gmail for new appointment emails and process them (bounded, idempotent)."

    def handle(self, *args, **options):
        gmail_service = GmailService()
        label_map = gmail_service.ensure_labels(_all_labels())

        query = (
            f"is:unread newer_than:{settings.GMAIL_POLL_QUERY_NEWER_THAN} "
            f"-label:{settings.GMAIL_PROCESSED_LABEL} "
            f"-label:{settings.GMAIL_PENDING_LABEL} "
            f"-label:{settings.GMAIL_FAILED_LABEL} "
            f"-label:{settings.GMAIL_UNAUTHORISED_LABEL}"
        )

        message_ids = gmail_service.list_message_ids(query, settings.GMAIL_POLL_MAX_MESSAGES)
        self.stdout.write(f"Found {len(message_ids)} candidate message(s).")

        for message_id in message_ids:
            existing = IncomingEmail.objects.filter(gmail_message_id=message_id).first()
            if existing and existing.status in (
                IncomingEmail.Status.PROCESSED,
                IncomingEmail.Status.PENDING_REVIEW,
                IncomingEmail.Status.UNAUTHORISED,
            ):
                continue  # already handled; never reprocess automatically

            raw_bytes, thread_id = gmail_service.get_message_raw(message_id)
            parsed = parse_email_message(raw_bytes)

            incoming_email = _save_incoming_email(existing, message_id, thread_id, parsed)

            process_incoming_email(incoming_email, gmail_service=gmail_service, label_map=label_map)

        self.stdout.write(self.style.SUCCESS("Done."))
