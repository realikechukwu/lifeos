"""python manage.py poll_gmail

Bounded, idempotent Gmail poll: fetch up to GMAIL_POLL_MAX_MESSAGES matching
messages, save them, reject unauthorised senders, extract a bounded action
batch, validate and execute each item independently, send one combined reply,
label, and audit. Safe to run repeatedly.
"""

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import AuditLog, IncomingEmail, ParsedAction
from assistant.services.email_parser import ParsedEmail, parse_email_message
from assistant.services.email_sender import send_batch_confirmation
from assistant.services.extractor import extract_actions
from assistant.services.gmail import GmailService, is_authorised_sender
from assistant.services.router import build_parsed_action_from_extraction, route_and_execute

ATTACHMENT_ONLY_TEXT_THRESHOLD = 40  # characters; below this we assume the body has no real content

# Outcomes where the assistant reached a definitive, self-contained decision
# (something was created, completed, or definitively not found) rather than
# needing a human to resolve an ambiguity.
DEFINITIVE_OUTCOMES = {
    "created",
    "created_with_reminder",
    "created_reminder_unclear",
    "task_created",
    "note_created",
    "reminder_created",
    "task_completed",
    "task_not_found",
    "unsupported",
}


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


def _run_extraction_and_gate(incoming_email: IncomingEmail) -> list[dict]:
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
        return [{
            "outcome": "attachment_review",
            "parsed_action": parsed_action,
            "context": {},
            "error": "",
        }]

    current_date = timezone.localdate()
    received_date = incoming_email.received_at.date() if incoming_email.received_at else current_date
    forwarded_date = incoming_email.original_forwarded_date.date() if incoming_email.original_forwarded_date else None
    outer_sender = incoming_email.outer_sender.strip().lower()
    sender_role = None
    if outer_sender == settings.AUTHORISED_EMAIL_IKE.strip().lower():
        sender_role = "ike"
    elif outer_sender == settings.AUTHORISED_EMAIL_WIFE.strip().lower():
        sender_role = "wife"

    extractions = extract_actions(
        text, current_date, received_date, forwarded_date, sender_role
    )
    results = []
    for extraction in extractions:
        parsed_action = None
        try:
            parsed_action = build_parsed_action_from_extraction(incoming_email, extraction)
            outcome, extra = route_and_execute(parsed_action)
            parsed_action.refresh_from_db()
            error = ""
            if parsed_action.status == ParsedAction.Status.FAILED:
                outcome = "failed"
                error = parsed_action.failure_reason[:2000]
            results.append({
                "outcome": outcome,
                "parsed_action": parsed_action,
                "context": extra,
                "error": error,
            })
        except Exception as exc:  # noqa: BLE001 - isolate one item from the rest of the batch
            error = str(exc)[:2000]
            if parsed_action is not None:
                parsed_action.status = ParsedAction.Status.FAILED
                parsed_action.failure_reason = error
                parsed_action.save(update_fields=["status", "failure_reason", "updated_at"])
            results.append({
                "outcome": "failed",
                "parsed_action": parsed_action,
                "context": {},
                "error": error,
            })
    return results


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
        results = _run_extraction_and_gate(incoming_email)
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

    outcomes = [item["outcome"] for item in results]
    is_definitive = bool(outcomes) and all(outcome in DEFINITIVE_OUTCOMES for outcome in outcomes)
    all_failed = bool(outcomes) and all(outcome == "failed" for outcome in outcomes)
    incoming_email.processing_attempts += 1
    if is_definitive:
        incoming_email.status = IncomingEmail.Status.PROCESSED
    elif all_failed:
        incoming_email.status = IncomingEmail.Status.FAILED
    else:
        incoming_email.status = IncomingEmail.Status.PENDING_REVIEW
    incoming_email.save(update_fields=["processing_attempts", "status"])

    reply_ok, reply_error = True, ""
    try:
        send_batch_confirmation(incoming_email, results, gmail_service=gmail_service)
    except Exception as exc:  # noqa: BLE001 - don't let a reply failure hide a successful action
        reply_ok, reply_error = False, str(exc)[:2000]

    if is_definitive:
        label_name = settings.GMAIL_PROCESSED_LABEL
    elif all_failed:
        label_name = settings.GMAIL_FAILED_LABEL
    else:
        label_name = settings.GMAIL_PENDING_LABEL
    label_id = label_map.get(label_name)
    if label_id:
        try:
            gmail_service.apply_label(incoming_email.gmail_message_id, label_id)
        except Exception:
            pass

    for item in results:
        parsed_action = item.get("parsed_action")
        AuditLog.objects.create(
            source_email=incoming_email,
            action="process_incoming_email_item",
            object_type="ParsedAction" if parsed_action is not None else "IncomingEmail",
            object_id=str(parsed_action.id if parsed_action is not None else incoming_email.id),
            success=reply_ok and item["outcome"] != "failed",
            details={"outcome": item["outcome"]},
            error_message=item.get("error") or reply_error,
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
