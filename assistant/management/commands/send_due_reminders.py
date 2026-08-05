"""python manage.py send_due_reminders

Claims a bounded batch of due, pending reminders and sends each through
Gmail. No database transaction is held open while calling Gmail: claiming
(pending -> processing) is a short, separate transaction from sending.

Workflow:
  1. Start a short DB transaction.
  2. select_for_update the due, pending reminders (skip_locked, so a
     concurrent run never blocks on or double-claims the same rows).
  3. Claim a limited batch: status -> processing, set claimed_at, generate
     and persist a fresh unique send_key + RFC Message-ID.
  4. Commit.
  5. Outside any transaction, send each claimed reminder via Gmail.
  6. On success: status -> sent, set sent_at.
  7. On failure: status -> failed, increment delivery_attempts, record a
     safe (truncated, no secrets) error message.
  8. Every claim/send/failure is recorded in AuditLog.

IMPORTANT — exactly-once delivery is not guaranteed. If Gmail accepts a
message and this process crashes before the following DB write commits,
the reminder is left in `processing` with no further automatic retry. A
reminder stuck in `processing` for more than 15 minutes is left alone by
this command (never auto-retried, to avoid a possible double-send) and
must be reset to `pending` manually from Django admin once you've checked
whether the email actually went out.
"""

import uuid
from datetime import datetime
from email.utils import make_msgid
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from core.models import AuditLog, Reminder
from assistant.services.gmail import GmailService
from assistant.services.reminders import resolve_recipient_emails

BATCH_SIZE = 25


def _reminder_datetime(reminder: Reminder):
    naive = datetime.combine(reminder.reminder_date, reminder.reminder_time)
    return naive.replace(tzinfo=ZoneInfo(reminder.timezone or settings.APP_TIMEZONE))


def claim_due_reminders(now=None, batch_size: int = BATCH_SIZE) -> list[Reminder]:
    """Short transaction: lock, filter to actually-due rows, claim them."""
    now = now or timezone.now()
    claimed = []
    with transaction.atomic():
        candidates = list(
            Reminder.objects.select_for_update(skip_locked=True)
            .filter(status=Reminder.Status.PENDING)
            .order_by("reminder_date", "reminder_time")[:batch_size]
        )
        for reminder in candidates:
            if _reminder_datetime(reminder) > now:
                continue
            reminder.status = Reminder.Status.PROCESSING
            reminder.claimed_at = now
            reminder.send_key = uuid.uuid4().hex
            reminder.rfc_message_id = make_msgid(domain="life-assistant.local")
            reminder.save(update_fields=["status", "claimed_at", "send_key", "rfc_message_id", "updated_at"])
            AuditLog.objects.create(
                source_email=reminder.source_email,
                action="reminder_claimed",
                object_type="Reminder",
                object_id=str(reminder.id),
                success=True,
                details={"send_key": reminder.send_key},
            )
            claimed.append(reminder)
    return claimed


def send_claimed_reminder(reminder: Reminder, gmail_service: GmailService) -> bool:
    """No open transaction here — this runs after claim_due_reminders() has
    already committed. Returns True on success."""
    recipients = resolve_recipient_emails(reminder.recipient)
    if not recipients:
        reminder.status = Reminder.Status.FAILED
        reminder.last_error = "No authorised email address configured for this recipient."
        reminder.delivery_attempts += 1
        reminder.save(update_fields=["status", "last_error", "delivery_attempts", "updated_at"])
        AuditLog.objects.create(
            source_email=reminder.source_email,
            action="reminder_failed",
            object_type="Reminder",
            object_id=str(reminder.id),
            success=False,
            error_message=reminder.last_error,
        )
        return False

    body = reminder.message.strip() if reminder.message else reminder.title
    try:
        gmail_service.send_message(
            to_addr=", ".join(recipients),
            subject=f"Reminder: {reminder.title}",
            body_text=body,
            message_id_header=reminder.rfc_message_id,
        )
    except Exception as exc:  # noqa: BLE001 - a genuine send failure, recorded safely
        reminder.status = Reminder.Status.FAILED
        reminder.last_error = str(exc)[:500]
        reminder.delivery_attempts += 1
        reminder.save(update_fields=["status", "last_error", "delivery_attempts", "updated_at"])
        AuditLog.objects.create(
            source_email=reminder.source_email,
            action="reminder_failed",
            object_type="Reminder",
            object_id=str(reminder.id),
            success=False,
            error_message=reminder.last_error,
        )
        return False

    reminder.status = Reminder.Status.SENT
    reminder.sent_at = timezone.now()
    reminder.delivery_attempts += 1
    reminder.save(update_fields=["status", "sent_at", "delivery_attempts", "updated_at"])
    AuditLog.objects.create(
        source_email=reminder.source_email,
        action="reminder_sent",
        object_type="Reminder",
        object_id=str(reminder.id),
        success=True,
        details={"recipients": recipients},
    )
    return True


class Command(BaseCommand):
    help = "Send due, pending email reminders (bounded batch, claim-then-send, no long-held transaction)."

    def handle(self, *args, **options):
        claimed = claim_due_reminders()
        self.stdout.write(f"Claimed {len(claimed)} due reminder(s).")
        if not claimed:
            return

        gmail_service = GmailService()
        sent = failed = 0
        for reminder in claimed:
            if send_claimed_reminder(reminder, gmail_service):
                sent += 1
            else:
                failed += 1

        self.stdout.write(self.style.SUCCESS(f"Sent {sent} reminder(s). {failed} failed."))
