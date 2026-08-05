from django.db import IntegrityError, transaction
from django.test import TestCase

from core.models import IncomingEmail


class DuplicateGmailMessageIdTests(TestCase):
    def test_duplicate_gmail_message_id_is_prevented(self):
        IncomingEmail.objects.create(
            gmail_message_id="msg-123",
            outer_sender="ike@example.com",
            subject="Dental appointment",
            status=IncomingEmail.Status.PROCESSED,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                IncomingEmail.objects.create(
                    gmail_message_id="msg-123",
                    outer_sender="ike@example.com",
                    subject="Duplicate",
                    status=IncomingEmail.Status.RECEIVED,
                )
