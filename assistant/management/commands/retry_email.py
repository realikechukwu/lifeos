"""python manage.py retry_email GMAIL_MESSAGE_ID

Manually re-run processing for a previously saved IncomingEmail without
bypassing idempotency (already-processed emails are not silently redone) or
sender validation (still re-checked by process_incoming_email)."""

from django.core.management.base import BaseCommand, CommandError

from core.models import IncomingEmail
from assistant.management.commands.poll_gmail import process_incoming_email


class Command(BaseCommand):
    help = "Manually retry processing of a saved IncomingEmail by its Gmail message id."

    def add_arguments(self, parser):
        parser.add_argument("gmail_message_id", type=str)

    def handle(self, *args, **options):
        message_id = options["gmail_message_id"]
        try:
            incoming_email = IncomingEmail.objects.get(gmail_message_id=message_id)
        except IncomingEmail.DoesNotExist as exc:
            raise CommandError(f"No IncomingEmail found with gmail_message_id={message_id}") from exc

        if incoming_email.status == IncomingEmail.Status.PROCESSED:
            self.stdout.write(self.style.WARNING(
                "This email was already processed successfully. Not retrying automatically. "
                "Use Django admin to review or correct the resulting ParsedAction instead."
            ))
            return

        incoming_email.processing_attempts += 1
        incoming_email.status = IncomingEmail.Status.PROCESSING
        incoming_email.save(update_fields=["processing_attempts", "status"])

        process_incoming_email(incoming_email)
        self.stdout.write(self.style.SUCCESS(f"Retried processing for {message_id}."))
